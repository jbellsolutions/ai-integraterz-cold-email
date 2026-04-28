"""Single-task subprocess executor.

Reads one task from the ledger, executes its tool_call with progress
callbacks that update the ledger, writes the result. A worker crash is
isolated — supervisor sees stale heartbeat and decides retry/escalate.

Usage:
  python -m orchestrator.worker --task-id task-XYZ
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
import traceback
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from tools.task_ledger import TaskLedger


def _say(msg: str) -> None:
    print(f"[worker] {msg}", flush=True)


async def execute(task_id: str) -> int:
    ledger = TaskLedger()
    t = ledger.get(task_id)
    if not t:
        _say(f"unknown task {task_id}")
        return 1
    if t["status"] in ("completed", "permanently_failed", "cancelled"):
        _say(f"task {task_id} already finalized: {t['status']}")
        return 0

    tool_call = t.get("tool_call") or {}
    tool_name = tool_call.get("name")
    args = dict(tool_call.get("args") or {})
    if not tool_name:
        ledger.mark_failed(task_id, "missing tool_call.name", permanent=True)
        return 2

    ledger.mark_running(task_id)
    ledger.append_event(task_id, "worker_start",
                          {"pid": os.getpid(), "tool": tool_name})

    # Cancellation polling: if status flips to 'cancelled' during execution,
    # raise CancelledError on the next progress callback so cooperative tools
    # exit cleanly.
    cancel_flag = {"cancelled": False}

    def check_cancellation():
        cur = ledger.get(task_id)
        if cur and cur.get("status") == "cancelled":
            cancel_flag["cancelled"] = True
            raise asyncio.CancelledError("task cancelled via ledger")

    def on_progress(stage: str, done: int, total: int, detail: str = ""):
        check_cancellation()
        progress = (done / total) if total else None
        ledger.update_progress(task_id, stage=stage, progress=progress)
        ledger.append_event(task_id, "progress",
                              {"stage": stage, "done": done, "total": total,
                               "detail": str(detail)[:200]})

    args["progress_cb"] = on_progress
    if t.get("thread_ts"):
        args["_thread_ts"] = t["thread_ts"]
    if t.get("channel"):
        args["_channel"] = t["channel"]
    args["_task_id"] = task_id  # workers may use this for sub-step recording

    # Resolve the tool. Workers can dispatch to:
    # 1. Concierge tool registry (TOOL_FUNCS) for high-level tools like
    #    personalize_to_csv, launch_pilot. These are async.
    # 2. Inline subagent runners (LLM tool-loop). For now, only #1.
    try:
        from orchestrator import slack_agent
        if tool_name not in slack_agent.TOOL_FUNCS:
            ledger.mark_failed(task_id, f"unknown tool {tool_name!r}",
                                  permanent=True)
            return 3
        func = slack_agent.TOOL_FUNCS[tool_name]
    except Exception as e:
        ledger.mark_failed(task_id, f"tool resolution failed: {e}",
                              permanent=True)
        return 4

    # Many concierge tools spawn their own asyncio tasks (legacy path);
    # for ledger-routed runs we want SYNCHRONOUS-style execution where the
    # worker blocks on the actual work and reports back. We accomplish that
    # by calling tools that ALREADY accept progress_cb (run_pilot path) via
    # a thin direct-execution wrapper for personalize_to_csv / bulk paths.
    #
    # The cleanest version is to dispatch on tool_name and call the squad
    # APIs directly. This is what we do here for personalize_to_csv;
    # other tools fall through to the registered concierge function.
    try:
        if tool_name == "personalize_to_csv":
            result = await _run_personalize_direct(args, on_progress, ledger,
                                                       task_id)
        elif tool_name == "spawn_subagent":
            result = await _run_spawn_subagent_direct(args, on_progress,
                                                          ledger, task_id)
        elif tool_name == "council_review":
            result = await _run_council_direct(args, on_progress, ledger,
                                                   task_id)
        else:
            # Fallback: let the registered concierge tool function run.
            # If it returns a "started=True" stub, we wait on its promise
            # via a polling loop (the legacy tools use create_task internally).
            r = func(args)
            result = await _await_legacy_tool(r, ledger, task_id)
        ledger.mark_completed(task_id, result if isinstance(result, dict)
                                  else {"value": str(result)})
        ledger.append_event(task_id, "worker_done", {"ok": True})
        return 0
    except asyncio.CancelledError:
        ledger.append_event(task_id, "worker_cancelled",
                              {"pid": os.getpid()})
        return 0
    except Exception as e:
        err = traceback.format_exc()
        ledger.append_event(task_id, "worker_exception",
                              {"error": err[:2000]})
        ledger.mark_failed(task_id, f"{type(e).__name__}: {e}",
                              permanent=False)
        # Print to stderr so watchdog log captures it
        print(err, file=sys.stderr, flush=True)
        return 5


# ---------------------------------------------------------------------------
# Direct execution for personalize_to_csv (the deliverable path)
# ---------------------------------------------------------------------------

async def _run_personalize_direct(args: dict, progress_cb, ledger: TaskLedger,
                                       task_id: str) -> dict:
    """Run Strategy(cached) → Research → Copy → write CSV → upload to Slack.

    This bypasses the slack_agent's create_task wrapper because the worker
    IS the task — it should block on the work and report a real result, not
    spawn another async task.
    """
    import csv
    import datetime as dt
    from squads.copy import CopySquad
    from squads.research import ResearchSquad
    from squads.research.squad import Lead
    from squads.strategy import StrategySquad
    from squads.smartlead.squad import make_campaign_name
    from tools.lead_loader import lead_summary
    from tools.slack_notify import SlackClient
    from orchestrator import slack_agent

    handle = args["lead_handle"]
    niche = args.get("niche")
    offer = args["offer"]
    variant = args.get("variant", "A")
    if handle not in slack_agent.LEAD_HANDLES:
        # Lead handles live in the concierge process memory; if the worker
        # is a separate process, they're not here. The concierge must serialize
        # leads to disk before creating the task.
        # Fallback: load from data/slack/handles/<handle>.json if present.
        handle_file = REPO_ROOT / "data" / "slack" / "handles" / f"{handle}.json"
        if handle_file.exists():
            data = json.loads(handle_file.read_text())
            leads_raw = data.get("leads", [])
            source = data.get("source", "?")
        else:
            raise RuntimeError(f"unknown lead_handle {handle!r} "
                                  f"(not in memory; no file at {handle_file})")
    else:
        meta = slack_agent.LEAD_HANDLES[handle]
        leads_raw = meta["leads"]
        source = meta.get("source", "?")

    # Reconstitute Lead dataclass instances from dicts (for cross-process)
    leads = []
    for raw in leads_raw:
        if isinstance(raw, Lead):
            leads.append(raw)
        else:
            leads.append(Lead(
                lead_id=raw.get("lead_id") or raw.get("email", ""),
                email=raw.get("email", ""),
                name=raw.get("name", ""),
                company=raw.get("company", ""),
                title=raw.get("title", ""),
                linkedin_url=raw.get("linkedin_url", ""),
            ))
    n = len(leads)
    campaign_name = make_campaign_name(niche, offer, variant)
    progress_cb("init", 0, n, f"campaign={campaign_name}")

    # 1. Strategy
    brief_dir = REPO_ROOT / "data" / "campaigns" / campaign_name
    brief_dir.mkdir(parents=True, exist_ok=True)
    brief_path = brief_dir / "brief.md"
    if brief_path.exists():
        brief = brief_path.read_text()
        progress_cb("strategy", 1, 1, "cached")
    else:
        progress_cb("strategy", 0, 1, "generating")
        strategy = StrategySquad()
        brief = await strategy.build_brief(offer, lead_summary(leads),
                                              niche=niche, variant=variant)
        brief_path.write_text(brief)
        progress_cb("strategy", 1, 1, f"{len(brief)} chars")

    # 2. Research
    rp = int(os.environ.get("CE2_RESEARCH_PARALLEL", "30"))
    progress_cb("research", 0, n, f"max_parallel={rp}")
    research = ResearchSquad(brief=brief)
    rt0 = dt.datetime.now(dt.UTC)
    signals = await research.research_batch(leads, max_parallel=rp)
    rdt = (dt.datetime.now(dt.UTC) - rt0).total_seconds()
    progress_cb("research", n, n, f"{rdt:.0f}s")

    # 3. Copy
    cp = int(os.environ.get("CE2_COPY_PARALLEL", "20"))
    progress_cb("copy", 0, n, f"max_parallel={cp}")
    copy = CopySquad(brief=brief)
    sem = asyncio.Semaphore(cp)
    done = {"n": 0}
    last = {"t": dt.datetime.now(dt.UTC)}

    async def write_one(lead, signal):
        async with sem:
            try:
                return await copy.write_one(lead, signal)
            except Exception as e:
                return {"sequence": [], "slop_pass": False, "error": str(e)}
            finally:
                done["n"] += 1
                now = dt.datetime.now(dt.UTC)
                if (done["n"] % 25 == 0 or
                        (now - last["t"]).total_seconds() > 30):
                    progress_cb("copy", done["n"], n, "")
                    last["t"] = now

    ct0 = dt.datetime.now(dt.UTC)
    emails = await asyncio.gather(*(write_one(l, s)
                                       for l, s in zip(leads, signals)))
    cdt = (dt.datetime.now(dt.UTC) - ct0).total_seconds()
    slop = sum(1 for e in emails if e.get("slop_pass"))
    progress_cb("copy", n, n, f"{cdt:.0f}s · {slop}/{n} clean")

    # 4. CSV
    progress_cb("writing_csv", 0, 1, "")
    cols = ["email", "first_name", "last_name", "company_name", "title",
             "linkedin_url",
             "email_1_subject", "email_1_body",
             "email_2_subject", "email_2_body",
             "email_3_subject", "email_3_body",
             "slop_pass", "signal_tier"]
    ts = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%S")
    out = REPO_ROOT / "data" / "exports" / f"{campaign_name}-{ts}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    skipped = 0
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, quoting=csv.QUOTE_ALL)
        w.writeheader()
        for lead, em, sig in zip(leads, emails, signals):
            seq = em.get("sequence") or []
            s1 = next((s for s in seq if s.get("step") == 1), None)
            s2 = next((s for s in seq if s.get("step") == 2), None)
            s3 = next((s for s in seq if s.get("step") == 3), None)
            if not s1 or not (s1.get("body") or "").strip():
                skipped += 1
                continue
            full = lead.name or ""
            first, _, last = full.partition(" ")
            w.writerow({
                "email": lead.email,
                "first_name": first,
                "last_name": last,
                "company_name": lead.company or "",
                "title": lead.title or "",
                "linkedin_url": getattr(lead, "linkedin_url", "") or "",
                "email_1_subject": s1.get("subject", ""),
                "email_1_body": s1.get("body", ""),
                "email_2_subject": (s2 or {}).get("subject", ""),
                "email_2_body": (s2 or {}).get("body", ""),
                "email_3_subject": (s3 or {}).get("subject", ""),
                "email_3_body": (s3 or {}).get("body", ""),
                "slop_pass": "1" if em.get("slop_pass") else "0",
                "signal_tier": (sig or {}).get("tier", ""),
            })
            written += 1
    progress_cb("writing_csv", 1, 1, f"{written} rows")

    # 5. Upload to Slack (worker DOES post for top-level tasks; child
    # tasks return result up the tree without posting).
    t = ledger.get(task_id)
    if not t.get("parent_task"):
        try:
            sc = SlackClient()
            channel = t.get("channel") or os.environ.get(
                "SLACK_CONTROL_CHANNEL", "#cold-email-control")
            comment = (
                f":white_check_mark: *Personalized CSV ready* — `{campaign_name}`\n"
                f"  • {written} rows ({skipped} dropped empty / {n} attempted)\n"
                f"  • Slop-clean: {slop}/{n}\n"
                f"  • Research {rdt:.0f}s · Copy {cdt:.0f}s · "
                f"*total {(rdt + cdt):.0f}s*\n"
                f"  • Task: `{task_id}`"
            )
            sc.upload_file(channel, out, title=out.name,
                              initial_comment=comment,
                              thread_ts=t.get("thread_ts"))
            progress_cb("posted", 1, 1, "uploaded to Slack")
            ledger.append_event(task_id, "slack_uploaded",
                                  {"file": str(out)})
        except Exception as e:
            ledger.append_event(task_id, "slack_upload_failed",
                                  {"error": str(e)[:300]})
            # Don't fail the task — file is on disk, we just couldn't upload

    return {
        "campaign_name": campaign_name,
        "leads_attempted": n,
        "rows_written": written,
        "rows_skipped_empty": skipped,
        "slop_pass": slop,
        "research_seconds": round(rdt, 1),
        "copy_seconds": round(cdt, 1),
        "csv_path": str(out.relative_to(REPO_ROOT)),
        "csv_size_bytes": out.stat().st_size,
    }


# ---------------------------------------------------------------------------
# spawn_subagent — delegate work down the tree
# ---------------------------------------------------------------------------

async def _run_spawn_subagent_direct(args: dict, progress_cb, ledger: TaskLedger,
                                          task_id: str) -> dict:
    """Spawn one or more child tasks. The PARENT worker waits until they
    all complete (or fail/cancel), then aggregates results and returns.

    args:
      role:    string label for the children
      intent:  one-line goal
      tool_call: {name, args} for each child (deterministic), OR
      children: list of {role, intent, tool_call} for batch fan-out
      max_depth: depth ceiling (default 3)
      timeout_seconds: how long to wait for children before giving up
    """
    from orchestrator.spawner import spawn_and_wait

    children_specs = args.get("children")
    if not children_specs:
        children_specs = [{
            "role": args["role"],
            "intent": args["intent"],
            "tool_call": args["tool_call"],
        }]
    timeout_seconds = int(args.get("timeout_seconds", 1800))
    max_depth = int(args.get("max_depth", 3))

    progress_cb("spawn_children", 0, len(children_specs), "")
    results = await spawn_and_wait(
        parent_id=task_id,
        children_specs=children_specs,
        max_depth=max_depth,
        timeout_seconds=timeout_seconds,
        progress_cb=progress_cb,
    )
    progress_cb("spawn_children", len(children_specs), len(children_specs),
                  "all returned")
    return {"children": results, "count": len(children_specs)}


async def _run_council_direct(args: dict, progress_cb, ledger: TaskLedger,
                                  task_id: str) -> dict:
    """Spawn N parallel critic subagents, aggregate verdicts."""
    from orchestrator.council import run_council
    return await run_council(
        parent_id=task_id,
        criteria=args["criteria"],
        target=args["target"],
        progress_cb=progress_cb,
        max_depth=int(args.get("max_depth", 3)),
    )


async def _await_legacy_tool(initial_result: dict | Any,
                                  ledger: TaskLedger, task_id: str) -> dict:
    """For legacy tools that return {started: True, task_id: <legacy>}, just
    pass through the initial result. The worker is structurally sync-style;
    if a tool needs async monitoring, it should be wrapped in one of the
    direct paths above instead."""
    if isinstance(initial_result, dict):
        return initial_result
    return {"value": str(initial_result)}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--task-id", required=True)
    a = p.parse_args()

    # Soft cancellation: SIGTERM → mark task cancelled in ledger then exit
    def _on_term(signum, frame):
        try:
            TaskLedger().mark_cancelled(a.task_id)
        except Exception:
            pass
        sys.exit(0)
    signal.signal(signal.SIGTERM, _on_term)
    signal.signal(signal.SIGINT, _on_term)

    return asyncio.run(execute(a.task_id))


if __name__ == "__main__":
    sys.exit(main())
