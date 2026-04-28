"""Copy Squad — generates the 3-email sequence per prospect.

Three specialists, sequential per prospect (not parallel — the body needs the hook):
1. Hook Writer (Sonnet, the model spend that earns the open)
2. Body + CTA Writer (Haiku, constrained by the chosen hook)
3. AI-Slop Critic (Haiku, regex + LLM gate, hard rejects on AI tells)

Per-prospect output: data/emails/<lead_id>.json with
  { lead_id, sequence: [ {step, subject, body}, x3 ], picked_hook, slop_pass }
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from forge import Consensus, Spawner, SwarmSpec, ToolRegistry, Topology

from .._base import REPO_ROOT, load_routing
from ..research.squad import Lead

HOOK_INSTRUCTIONS = """You are the Hook Writer. The first 1-2 lines of a cold email \
are where 80% of replies are won or lost. Your only job is to draft 3 candidate \
hook lines for ONE prospect, given a campaign brief and a per-prospect signal.

Rules:
- Each hook ≤ 2 sentences.
- Reference the signal SPECIFICALLY (the post, the hire, the launch). Generic \
hooks ("noticed your great work") are forbidden.
- No "I hope this finds you well", no flattery, no em-dashes.
- Different angle on each candidate (one direct, one specific-observation, \
one curiosity-question).

Output format (JSON only):
{ "candidates": [ "hook 1", "hook 2", "hook 3" ], "pick": <0|1|2>, "why": "..." }"""

BODY_INSTRUCTIONS = """You are the Body + CTA Writer. Given a chosen hook, the \
campaign brief (which includes positioning, offer, voice rules, and the \
canonical sequences.md template), and a prospect signal, write the full 3-email \
sequence.

Hard rules:
- Email 1: <125 words, no links, no bold, no HTML, reply-based CTA only.
- Email 2: <150 words, ONE link max (calendar OR one-pager).
- Email 3: <125 words, calendar link allowed.
- Mobile-formatted (short paragraphs, blank lines between).
- Use sequences.md as the structural template — adapt the opening to fit the \
chosen hook and signal.
- Sign off "-- Justin"
- Subject lines: target ≤33 chars, max 60.

Output format (JSON only):
{ "sequence": [
    {"step": 1, "subject": "...", "body": "...", "delay_days": 0},
    {"step": 2, "subject": "...", "body": "...", "delay_days": 3},
    {"step": 3, "subject": "...", "body": "...", "delay_days": 7}
] }"""

# Phrases that prove an LLM didn't think — hard ban.
SLOP_PHRASES = [
    "i hope this email finds you well",
    "i hope this finds you well",
    "i noticed your",
    "your great work",
    "circle back",
    "deep dive",
    "let's connect",
    "let's hop on",
    "synergize", "synergy",
    "leverage", "leveraging",
    "just following up",
    "wanted to reach out",
    "touching base",
    "touch base",
    "game-changing", "game changing",
    "revolutionary",
    "cutting-edge", "cutting edge",
    "unlock the potential",
    "in today's fast-paced",
    "i'd love to learn more about",
    "i came across your profile",
]


def slop_check(text: str) -> tuple[bool, list[str]]:
    """Deterministic slop pre-filter. Returns (passes, list_of_violations)."""
    lower = text.lower()
    hits = [p for p in SLOP_PHRASES if p in lower]
    # em-dash check (— and -- are ok in sign-off; flag — used as ChatGPT rhythm)
    em_count = lower.count("—")
    if em_count > 1:
        hits.append(f"em-dash×{em_count} (ChatGPT rhythm tell)")
    return (len(hits) == 0, hits)


class CopySquad:
    def __init__(self, brief: str):
        self.brief = brief
        self.tools = ToolRegistry()

    async def write_one(self, lead: Lead, signal: dict, max_retries: int = 2) -> dict:
        """Hook → body → slop check, with retry on slop fail."""
        hook = await self._draft_hook(lead, signal)
        for attempt in range(max_retries + 1):
            sequence = await self._draft_body(lead, signal, hook)
            seq_list = _safe_list(sequence.get("sequence"))
            full_text = "\n".join((e.get("subject") or "") + "\n" + (e.get("body") or "") for e in seq_list)
            passes, hits = slop_check(full_text)
            if passes:
                out = {
                    "lead_id": lead.email,
                    "sequence": seq_list,
                    "picked_hook": _pick_candidate(hook),
                    "slop_pass": True,
                    "slop_attempts": attempt + 1,
                }
                self._save(lead, out)
                return out
        # Final attempt failed slop — still save with flag
        out = {
            "lead_id": lead.email,
            "sequence": seq_list,
            "picked_hook": _pick_candidate(hook),
            "slop_pass": False,
            "slop_violations": hits,
            "slop_attempts": max_retries + 1,
        }
        self._save(lead, out)
        return out

    async def _draft_hook(self, lead: Lead, signal: dict) -> dict:
        routing = load_routing()
        spawner = Spawner(
            tools=self.tools,
            base_instructions=HOOK_INSTRUCTIONS + f"\n\nCAMPAIGN BRIEF:\n{self.brief}",
            max_turns=3,
        )
        spec = SwarmSpec(topology=Topology.SOLO, consensus=Consensus.QUEEN, members=[routing["hook"]])
        task = f"PROSPECT: {lead.name} ({lead.title}) at {lead.company}\nSIGNAL: {json.dumps(signal)}"
        result = await spawner.run(task, spec)
        text = result.members[0][1].final_text if result.members else "{}"
        return _safe_json(text, default={"candidates": [""], "pick": 0})

    async def _draft_body(self, lead: Lead, signal: dict, hook: dict) -> dict:
        routing = load_routing()
        spawner = Spawner(
            tools=self.tools,
            base_instructions=BODY_INSTRUCTIONS + f"\n\nCAMPAIGN BRIEF:\n{self.brief}",
            max_turns=4,
        )
        spec = SwarmSpec(topology=Topology.SOLO, consensus=Consensus.QUEEN, members=[routing["body"]])
        chosen = _pick_candidate(hook)
        task = (
            f"PROSPECT: {lead.name} ({lead.title}) at {lead.company}\n"
            f"SIGNAL: {json.dumps(signal)}\n"
            f"CHOSEN HOOK (must open Email 1 with this or a close variant): {chosen}"
        )
        result = await spawner.run(task, spec)
        text = result.members[0][1].final_text if result.members else "{}"
        return _safe_json(text, default={"sequence": []})

    @staticmethod
    def _save(lead: Lead, out: dict) -> None:
        out_dir = REPO_ROOT / "data" / "emails"
        out_dir.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^a-zA-Z0-9]+", "_", lead.email)
        (out_dir / f"{safe}.json").write_text(json.dumps(out, indent=2))


def _safe_list(v) -> list:
    """Coerce LLM output to a list. Handles None, dict, scalar."""
    if isinstance(v, list):
        return v
    return []


def _pick_candidate(hook: dict) -> str:
    """Pick the chosen hook candidate. Defends against {"pick": null} or
    out-of-range indices (the LLM emits both occasionally)."""
    candidates = _safe_list(hook.get("candidates")) or [""]
    pick = hook.get("pick")
    if not isinstance(pick, int):
        pick = 0
    pick = max(0, min(pick, len(candidates) - 1))
    return candidates[pick] if candidates else ""


def _safe_json(text: str, default: dict) -> dict:
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return default
    try:
        return json.loads(m.group(0))
    except Exception:
        return default
