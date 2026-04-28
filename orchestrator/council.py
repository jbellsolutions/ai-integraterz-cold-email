"""Council pattern: spawn N parallel critics, aggregate verdicts.

Built on top of spawner.spawn_and_wait. The council pattern is:
  1. Concierge calls council_review(criteria=[...], target=...).
  2. Each criterion → one critic subagent (LLM with a scoped prompt).
  3. Critics run in parallel, return verdict {pass: bool, score, reasoning}.
  4. Council returns aggregate: pass = all critics pass, fail = any block.

For now critics are LLM-driven via Claude Sonnet (read-only tools). Future
versions can use forge's PARALLEL_COUNCIL+MAJORITY for vote-based decisions.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import anthropic

from orchestrator.spawner import spawn_and_wait
from tools.task_ledger import TaskLedger


CRITIC_MODEL = os.environ.get("CE2_CRITIC_MODEL", "claude-haiku-4-5")
CRITIC_PROMPT = """You are a single-criterion critic on a multi-agent council.

Your criterion: {criterion}

You will be given a target (a CSV file path, a brief, a piece of copy, etc.).
Read it carefully. Apply ONLY your single criterion — leave other dimensions
to the other critics. Return strict JSON:

{{
    "pass": true | false,
    "score": 0-10,
    "reasoning": "1-3 sentences",
    "violations": ["..."],          // empty list if pass
    "recommendation": "..."         // optional
}}

Be fair but firm. Block (pass=false) when the criterion is materially
violated. Note borderline cases in reasoning even when passing.
"""


async def _critic_verdict(criterion: str, target_summary: str) -> dict:
    """Single LLM call. Returns the parsed verdict dict."""
    client = anthropic.Anthropic()
    msg = client.messages.create(
        model=CRITIC_MODEL,
        max_tokens=512,
        system=CRITIC_PROMPT.format(criterion=criterion),
        messages=[{
            "role": "user",
            "content": (f"Target to evaluate:\n\n{target_summary}\n\n"
                          "Return strict JSON only — no preamble."),
        }],
    )
    text = "".join(getattr(b, "text", "") for b in msg.content).strip()
    # Strip code-fence if present
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:].strip()
    try:
        return json.loads(text)
    except Exception as e:
        return {"pass": False, "score": 0,
                  "reasoning": f"verdict parse failed: {e}",
                  "violations": [text[:300]]}


def _summarize_target(target: str) -> str:
    """If target is a path, read first ~3KB. Otherwise return as-is."""
    p = Path(target)
    if p.is_file():
        try:
            data = p.read_text(encoding="utf-8", errors="ignore")
            if len(data) > 4000:
                return data[:4000] + "\n\n[... truncated ...]"
            return data
        except Exception as e:
            return f"(could not read {target}: {e})"
    return target


async def run_council(parent_id: str, criteria: list[str], target: str,
                          progress_cb=None, max_depth: int = 3) -> dict:
    """Synchronous-style: runs N critics in parallel via asyncio.gather and
    writes one ledger record per critic so we have an audit trail.

    Returns aggregated verdict {pass, blocking, verdicts: [...]}.
    """
    ledger = TaskLedger()
    target_summary = _summarize_target(target)

    if progress_cb:
        progress_cb("council_start", 0, len(criteria), f"target={target[:60]}")

    # Record each critic as a child task in the ledger (LLM call instead of
    # subprocess — critics are short and cheap).
    child_ids: list[str] = []
    for crit in criteria:
        try:
            cid = ledger.spawn_child(
                parent_id=parent_id,
                role=f"critic:{crit[:30]}",
                intent=f"evaluate target against criterion: {crit}",
                tool_call={"name": "__llm_critic__",
                            "args": {"criterion": crit}},
                deadline_seconds=120,
                max_depth=max_depth,
            )
        except RuntimeError as e:
            return {"pass": False, "blocking": [str(e)], "verdicts": [],
                      "error": "depth_exceeded"}
        child_ids.append(cid)
        ledger.mark_running(cid)

    # Run critics in parallel
    async def _critic_wrapped(crit, cid):
        try:
            v = await _critic_verdict(crit, target_summary)
            ledger.mark_completed(cid, v)
            if progress_cb:
                progress_cb("critic_done", 1, 1,
                              f"{crit[:30]}→{'pass' if v.get('pass') else 'block'}")
            return v
        except Exception as e:
            ledger.mark_failed(cid, str(e))
            return {"pass": False, "score": 0,
                      "reasoning": f"critic error: {e}",
                      "violations": [], "criterion": crit}

    verdicts = await asyncio.gather(*(_critic_wrapped(c, cid)
                                          for c, cid in zip(criteria, child_ids)))

    # Annotate verdicts with the criterion (for caller convenience)
    for v, c in zip(verdicts, criteria):
        v.setdefault("criterion", c)

    blocking = [v for v in verdicts if not v.get("pass")]
    overall_pass = len(blocking) == 0

    result = {
        "pass": overall_pass,
        "verdicts": verdicts,
        "blocking_count": len(blocking),
        "blocking_criteria": [v.get("criterion") for v in blocking],
        "child_task_ids": child_ids,
        "target": target,
    }
    if progress_cb:
        progress_cb("council_done", len(criteria), len(criteria),
                      f"pass={overall_pass}  blocking={len(blocking)}")
    return result
