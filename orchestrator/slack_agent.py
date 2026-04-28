"""Slack-driven always-on orchestrator daemon.

Polls a single Slack channel (default `#cold-email-control`) for new user
messages and routes each one through Anthropic tool-use. Tools cover the
operational surface: ingest CSV uploads, pull leads from Smartlead Prospector
via natural language, launch pilots into pre-created campaigns, archive
campaigns, run stats.

Persistent per-thread conversation memory (data/slack/threads/<thread_ts>.json)
so multi-turn requests like "find me 50 recruiting agency owners" → "ok now
launch them into recruiters-power-partner-A" hold context.

Destructive / credit-spending tools (`launch_pilot`, `prospect_fetch_confirmed`,
`archive_campaign`) require the user to say "yes" explicitly first — the system
prompt instructs the agent to ask before calling those tools.

Run:
  python -m orchestrator.slack_agent              # daemon, polls forever
  python -m orchestrator.slack_agent --once       # single poll then exit
  python -m orchestrator.slack_agent --interval=10
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import sys
import traceback
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import anthropic

from tools.slack_notify import SlackClient

DATA_DIR = REPO_ROOT / "data" / "slack"
CURSOR_PATH = DATA_DIR / "cursor.json"
ACTIVE_THREADS_PATH = DATA_DIR / "active_threads.json"
THREADS_DIR = DATA_DIR / "threads"
UPLOADS_DIR = DATA_DIR / "uploads"

# Threads we keep watching for new replies after the parent message is gone
# from the conversations.history window. Pruned after 7 days of inactivity.
ACTIVE_THREAD_TTL_SECONDS = 7 * 24 * 3600

# In-memory handles to ephemeral lead lists the user is workshopping mid-thread.
# {handle_id -> {"leads": [...], "source": str, "created_at": iso}}
LEAD_HANDLES: dict[str, dict] = {}
RUNNING_PILOTS: dict[str, asyncio.Task] = {}
THREAD_LOCKS: dict[str, asyncio.Lock] = {}


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

def _load_cursor() -> dict:
    if CURSOR_PATH.exists():
        return json.loads(CURSOR_PATH.read_text())
    return {}


def _save_cursor(c: dict) -> None:
    CURSOR_PATH.parent.mkdir(parents=True, exist_ok=True)
    CURSOR_PATH.write_text(json.dumps(c, indent=2))


def _load_thread(thread_ts: str) -> list[dict]:
    p = THREADS_DIR / f"{thread_ts}.json"
    if p.exists():
        return json.loads(p.read_text())
    return []


def _save_thread(thread_ts: str, messages: list[dict]) -> None:
    THREADS_DIR.mkdir(parents=True, exist_ok=True)
    (THREADS_DIR / f"{thread_ts}.json").write_text(json.dumps(messages, indent=2))


def _load_active_threads() -> dict:
    """Map of {thread_ts: {"last_reply_ts": str, "updated_at": iso}}."""
    if ACTIVE_THREADS_PATH.exists():
        try:
            return json.loads(ACTIVE_THREADS_PATH.read_text())
        except Exception:
            return {}
    return {}


def _save_active_threads(d: dict) -> None:
    ACTIVE_THREADS_PATH.parent.mkdir(parents=True, exist_ok=True)
    ACTIVE_THREADS_PATH.write_text(json.dumps(d, indent=2))


def _register_active_thread(thread_ts: str, last_reply_ts: str) -> None:
    """Mark a thread as live so subsequent polls watch it for replies."""
    d = _load_active_threads()
    d[thread_ts] = {
        "last_reply_ts": last_reply_ts,
        "updated_at": dt.datetime.utcnow().isoformat() + "Z",
    }
    _save_active_threads(d)


def _prune_active_threads() -> dict:
    """Drop threads idle longer than ACTIVE_THREAD_TTL_SECONDS. Returns the
    pruned dict so the caller can iterate without re-loading."""
    d = _load_active_threads()
    cutoff = dt.datetime.utcnow() - dt.timedelta(seconds=ACTIVE_THREAD_TTL_SECONDS)
    keep = {}
    for tts, meta in d.items():
        try:
            updated = dt.datetime.fromisoformat(meta.get("updated_at", "").rstrip("Z"))
        except Exception:
            updated = dt.datetime.utcnow()  # keep on parse error
        if updated >= cutoff:
            keep[tts] = meta
    if len(keep) != len(d):
        _save_active_threads(keep)
    return keep


def _new_handle(prefix: str = "leads") -> str:
    return f"{prefix}-{dt.datetime.utcnow().strftime('%H%M%S%f')}"


# ---------------------------------------------------------------------------
# Tool implementations — every callable returns a JSON-serializable dict.
# ---------------------------------------------------------------------------

def tool_list_active_campaigns(_: dict) -> dict:
    from tools.smartlead import SmartleadCLI
    cli = SmartleadCLI()
    cs = cli.list_campaigns()
    keep = [c for c in cs if (c.get("status") or "").upper() in ("DRAFTED", "ACTIVE", "STARTED", "PAUSED")]
    return {"campaigns": [
        {"id": c.get("id"), "name": c.get("name"), "status": c.get("status"),
          "leads": c.get("lead_count") or c.get("leads_count") or 0}
        for c in keep
    ]}


def tool_list_briefs(_: dict) -> dict:
    out = []
    for d in sorted((REPO_ROOT / "data" / "campaigns").glob("*/")):
        bp = d / "brief.md"
        if bp.exists():
            txt = bp.read_text()
            out.append({"campaign_name": d.name,
                          "preview": txt[:200] + ("…" if len(txt) > 200 else ""),
                          "chars": len(txt)})
    return {"briefs": out}


def tool_read_brief(args: dict) -> dict:
    name = args["campaign_name"]
    bp = REPO_ROOT / "data" / "campaigns" / name / "brief.md"
    if not bp.exists():
        return {"error": f"no brief at data/campaigns/{name}/brief.md"}
    return {"campaign_name": name, "brief": bp.read_text()}


def tool_stats(_: dict) -> dict:
    from io import StringIO
    from contextlib import redirect_stdout
    from orchestrator.stats import print_stats
    buf = StringIO()
    with redirect_stdout(buf):
        print_stats()
    return {"text": buf.getvalue() or "(no output)"}


def tool_ingest_csv_from_slack(args: dict) -> dict:
    """Download a Slack-uploaded CSV by file_id, validate, return a lead handle."""
    from tools.lead_loader import load_leads, lead_summary
    file_id = args["file_id"]
    sc = SlackClient()
    info = sc.file_info(file_id)
    url = info.get("url_private")
    if not url:
        return {"error": f"no url_private on file {file_id}"}
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    dest = UPLOADS_DIR / f"{file_id}.csv"
    sc.download_file(url, dest)
    try:
        leads = load_leads(dest)
    except Exception as e:
        return {"error": f"CSV failed validation: {e}",
                  "saved_to": str(dest.relative_to(REPO_ROOT))}
    handle = _new_handle("csv")
    LEAD_HANDLES[handle] = {
        "leads": leads, "source": f"slack_csv:{info.get('name', file_id)}",
        "created_at": dt.datetime.utcnow().isoformat(),
    }
    return {"lead_handle": handle, "count": len(leads),
            "summary": lead_summary(leads),
            "saved_to": str(dest.relative_to(REPO_ROOT))}


def tool_pull_prospector_nl(args: dict) -> dict:
    """Translate NL → filters, run prospect search (FREE — no fetch yet).
    Returns count + filter_id; user must call prospect_fetch_confirmed to fetch."""
    from tools.prospect_filters import nl_to_filters
    from tools.smartlead import SmartleadCLI
    description = args["description"]
    filters = nl_to_filters(description)
    search = SmartleadCLI().prospect_search(filters)
    return {
        "filters_used": filters,
        "filter_id": search.get("filter_id") or search.get("id"),
        "count": search.get("count") or search.get("total") or 0,
        "note": "Search is FREE. Call prospect_fetch_confirmed with filter_id + max_fetch to actually pull leads (consumes credits).",
    }


def tool_pull_prospector_saved(args: dict) -> dict:
    from tools.smartlead import SmartleadCLI
    sid = args["saved_search_id"]
    search = SmartleadCLI().prospect_search_saved(sid)
    return {
        "saved_search_id": sid,
        "filter_id": search.get("filter_id") or search.get("id"),
        "count": search.get("count") or search.get("total") or 0,
        "note": "Search is FREE. Call prospect_fetch_confirmed with filter_id + max_fetch to actually pull leads (consumes credits).",
    }


def tool_prospect_fetch_confirmed(args: dict) -> dict:
    """User confirmed credit cost. Fetch + find-emails + return lead handle."""
    from tools.lead_loader import load_leads_from_prospect, lead_summary
    filters = args.get("filters")
    saved_id = args.get("saved_search_id")
    max_fetch = int(args.get("max_fetch", 20))
    if not (filters or saved_id):
        return {"error": "supply filters or saved_search_id"}
    leads = load_leads_from_prospect(saved_search_id=saved_id, filters=filters,
                                       max_n=max_fetch, max_fetch=max_fetch)
    handle = _new_handle("prospect")
    LEAD_HANDLES[handle] = {
        "leads": leads, "source": "prospect_saved" if saved_id else "prospect_filters",
        "created_at": dt.datetime.utcnow().isoformat(),
    }
    return {"lead_handle": handle, "count": len(leads),
            "summary": lead_summary(leads)}


def tool_launch_pilot(args: dict) -> dict:
    """Spawn run_pilot as a background task. Returns immediately with task id.

    Three reliability fixes from 2026-04-28:
    1. `done_callback` reads `task.exception()` so silent crashes get posted.
       Previously a TypeError or import error before the inner try inside the
       coroutine would simply vanish.
    2. Throttled progress posts to Slack ("research 250/698 done · 12s/100")
       so a 30-min run isn't a black hole.
    3. Hard-fail in run_pilot raises if Copy produced empty sequences;
       this catches it and posts ❌ with the diagnostic instead of pretending
       the upload succeeded.
    """
    from orchestrator.main import run_pilot
    handle = args["lead_handle"]
    niche = args.get("niche")
    offer = args["offer"]
    variant = args.get("variant", "A")
    if handle not in LEAD_HANDLES:
        return {"error": f"unknown lead_handle '{handle}' — call ingest or fetch first"}
    leads = LEAD_HANDLES[handle]["leads"]
    source = LEAD_HANDLES[handle]["source"]
    campaign_label = f"{niche}-{offer}-{variant.upper()}"

    sc = SlackClient()
    channel = os.environ.get("SLACK_CONTROL_CHANNEL", "#cold-email-control")
    thread_ts = args.get("_thread_ts")  # injected by run_tools

    # Throttled progress callback — only posts at meaningful milestones so we
    # don't spam Slack but the user can see it's alive. Stage transitions and
    # "every 100 leads" within copy.
    last_msg = {"key": ""}
    def progress_cb(stage: str, done: int, total: int, detail: str):
        # Only post on stage transitions or every 100 within copy. The first
        # call for each stage (done=0) marks the transition.
        key = f"{stage}:{done // 100}" if stage == "copy" else f"{stage}:{done}/{total}"
        if key == last_msg["key"]:
            return
        last_msg["key"] = key
        msg = (f":hourglass_flowing_sand: `{campaign_label}` · "
                f"*{stage}* {done}/{total}" +
                (f" · {detail}" if detail else ""))
        try:
            sc.post(channel, msg, thread_ts=thread_ts)
        except Exception as e:
            print(f"[launch_pilot] progress post failed: {e}", flush=True)

    async def _runner():
        result = await run_pilot(offer, leads, niche=niche, variant=variant,
                                    source_label=source, dry_run=False,
                                    progress_cb=progress_cb)
        # Post full success summary with real numbers — not the old vague
        # "pilot complete" line.
        total = result.get("total_seconds", 0)
        camp_id = result.get("campaign_id", "?")
        leads_in = result.get("leads_in_this_run", len(leads))
        slop = result.get("slop_pass", "?")
        sc.post(channel,
                  f":white_check_mark: *pilot complete* — `{campaign_label}` "
                  f"(id `{camp_id}`)\n"
                  f"  • {leads_in} leads from {source}\n"
                  f"  • Slop-clean: {slop}/{leads_in}\n"
                  f"  • Research {result.get('research_seconds', 0):.0f}s · "
                  f"Copy {result.get('copy_seconds', 0):.0f}s · "
                  f"Smartlead {result.get('smartlead_seconds', 0):.0f}s · "
                  f"*total {total:.0f}s*\n"
                  f"  • Log: `{result.get('pilot_log', '?')}`",
                  thread_ts=thread_ts)
        return result

    def _on_done(task: asyncio.Task):
        """Critical: post traceback to Slack on ANY exception, including the
        ones that the inner try-except in _runner would have missed (cancellation,
        import errors, etc.). This is the bug Justin hit — three pilots launched,
        zero completion messages, zero errors anywhere."""
        if task.cancelled():
            sc.post(channel, f":x: `{campaign_label}` pilot was *cancelled*",
                     thread_ts=thread_ts)
            return
        exc = task.exception()
        if exc is None:
            return  # success path already posted
        import traceback as tb
        err = "".join(tb.format_exception(type(exc), exc, exc.__traceback__))
        # Trim to last ~1500 chars so it fits in a Slack message
        tail = err[-1500:] if len(err) > 1500 else err
        try:
            sc.post(channel,
                      f":x: *pilot FAILED* — `{campaign_label}`\n"
                      f"```\n{tail}\n```",
                      thread_ts=thread_ts)
        except Exception as post_e:
            print(f"[launch_pilot] failed to post error: {post_e}\n"
                   f"original error: {err}", flush=True)
        # also dump to disk so we have it forever
        try:
            crash_dir = REPO_ROOT / "data" / "pilots"
            crash_dir.mkdir(parents=True, exist_ok=True)
            (crash_dir / f"CRASH-{campaign_label}-"
                f"{dt.datetime.utcnow().strftime('%Y%m%dT%H%M%S')}.txt"
            ).write_text(err)
        except Exception:
            pass

    task_id = _new_handle("pilot")
    task = asyncio.create_task(_runner())
    task.add_done_callback(_on_done)
    RUNNING_PILOTS[task_id] = task
    return {"started": True, "task_id": task_id, "leads": len(leads),
            "campaign": campaign_label,
            "note": ("Running in background. Progress will post to this thread "
                       "(stage transitions + every 100 leads). I will post a final "
                       "✅ or ❌ when done.")}


def tool_load_leads_from_smartlead(args: dict) -> dict:
    """Pull leads OUT of an existing Smartlead campaign by ID. Returns a lead
    handle the user can then route into one of our new (niche, offer, variant)
    campaigns. FREE — no credits consumed."""
    from tools.lead_loader import load_leads_from_campaign, lead_summary
    cid = args["campaign_id"]
    max_n = args.get("max_n")
    leads = load_leads_from_campaign(cid, max_n=max_n)
    if not leads:
        return {"lead_handle": None, "count": 0,
                "warning": f"campaign {cid} has no leads (or all are missing emails)"}
    handle = _new_handle("from-campaign")
    LEAD_HANDLES[handle] = {
        "leads": leads, "source": f"smartlead_campaign:{cid}",
        "created_at": dt.datetime.utcnow().isoformat(),
    }
    return {"lead_handle": handle, "count": len(leads),
            "summary": lead_summary(leads)}


def _serialize_handle_to_disk(handle: str) -> bool:
    """Persist a LEAD_HANDLES entry to data/slack/handles/<handle>.json so a
    worker subprocess (different process, different memory) can load it.
    Returns True if written, False if handle is unknown."""
    if handle not in LEAD_HANDLES:
        return False
    meta = LEAD_HANDLES[handle]
    d = REPO_ROOT / "data" / "slack" / "handles"
    d.mkdir(parents=True, exist_ok=True)
    leads_serialized = []
    for lead in meta.get("leads", []):
        if hasattr(lead, "__dict__"):
            leads_serialized.append(dict(lead.__dict__))
        elif isinstance(lead, dict):
            leads_serialized.append(dict(lead))
        else:
            leads_serialized.append({"raw": str(lead)})
    payload = {
        "handle": handle,
        "source": meta.get("source", ""),
        "created_at": meta.get("created_at", ""),
        "leads": leads_serialized,
    }
    (d / f"{handle}.json").write_text(json.dumps(payload, indent=2,
                                                       default=str))
    return True


def _route_to_ledger(intent: str, tool_name: str, args: dict,
                       *, deadline_seconds: int = 1800,
                       channel: str | None = None,
                       thread_ts: str | None = None) -> dict:
    """Common path for long-running tools. Creates a ledger task; supervisor
    spawns a worker. Returns immediately to the concierge with the task id.

    Side effects: if `args` carries a `lead_handle`, the in-memory handle is
    serialized to disk so the worker process can load it.
    """
    from tools.task_ledger import get_ledger
    args_clean = {k: v for k, v in args.items() if not k.startswith("_")}
    if "lead_handle" in args_clean:
        ok = _serialize_handle_to_disk(args_clean["lead_handle"])
        if not ok:
            return {"error": f"unknown lead_handle "
                                f"{args_clean['lead_handle']!r}"}
    ledger = get_ledger()
    task_id = ledger.create_task(
        intent=intent,
        channel=channel,
        thread_ts=thread_ts,
        created_by="slack",
        tool_call={"name": tool_name, "args": args_clean},
        deadline_seconds=deadline_seconds,
    )
    return {
        "queued": True,
        "task_id": task_id,
        "tool": tool_name,
        "deadline_seconds": deadline_seconds,
        "note": (f"Task `{task_id}` queued. The supervisor will spawn a "
                   "worker within 15s. Progress posts to this thread; final "
                   "result delivered as a Slack file or message when done. "
                   "If you want to know status, ask `list my tasks`."),
    }


def tool_list_my_tasks(args: dict) -> dict:
    """Return all tasks in the current Slack thread (or any thread the user
    names). Read-only. No confirmation needed."""
    from tools.task_ledger import get_ledger
    ledger = get_ledger()
    thread_ts = args.get("thread_ts") or args.get("_thread_ts")
    if not thread_ts:
        return {"error": "no thread_ts in scope"}
    tasks = ledger.list_by_thread(thread_ts)
    summary = []
    for t in tasks[:30]:
        summary.append({
            "id": t["id"],
            "intent": (t.get("intent") or "")[:120],
            "status": t["status"],
            "stage": t.get("stage"),
            "progress": t.get("progress"),
            "attempts": t.get("attempts"),
            "depth": t.get("depth"),
            "parent": t.get("parent_task"),
            "created_at": t.get("created_at"),
            "completed_at": t.get("completed_at"),
        })
    return {"thread_ts": thread_ts, "count": len(tasks), "tasks": summary}


def tool_cancel_task(args: dict) -> dict:
    """Mark a task as cancelled. The worker checks the ledger on its next
    progress callback and exits cleanly. Subagent descendants are also
    cancelled."""
    from tools.task_ledger import get_ledger
    ledger = get_ledger()
    task_id = args["task_id"]
    t = ledger.get(task_id)
    if not t:
        return {"error": f"unknown task_id {task_id!r}"}
    if t["status"] in ("completed", "permanently_failed", "cancelled"):
        return {"task_id": task_id, "already": t["status"]}
    descendants = ledger.descendants(task_id)
    ledger.mark_cancelled(task_id)
    for d in descendants:
        ledger.mark_cancelled(d["id"])
    return {"task_id": task_id, "cancelled": True,
            "descendants_cancelled": len(descendants)}


def tool_spawn_subagent(args: dict) -> dict:
    """Concierge-side entry point for spawn_subagent. Creates a ROOT task
    whose tool_call is `spawn_subagent` itself. The worker that picks it up
    will fan out via orchestrator.spawner.spawn_and_wait."""
    intent = args.get("intent",
                         f"spawn {args.get('role', 'subagent')}")
    channel = args.get("_channel") or os.environ.get(
        "SLACK_CONTROL_CHANNEL", "#cold-email-control")
    return _route_to_ledger(
        intent=intent,
        tool_name="spawn_subagent",
        args=args,
        deadline_seconds=int(args.get("timeout_seconds", 1800)),
        channel=channel,
        thread_ts=args.get("_thread_ts"),
    )


def tool_council_review(args: dict) -> dict:
    """Concierge-side entry: queue a council review as a top-level task.
    Worker spawns one critic per criterion in parallel and returns the
    aggregated verdict. Useful before delivering CSVs to ensure quality."""
    intent = (f"council review of {args.get('target', '?')[:60]} "
                f"on {len(args.get('criteria', []))} criteria")
    channel = args.get("_channel") or os.environ.get(
        "SLACK_CONTROL_CHANNEL", "#cold-email-control")
    return _route_to_ledger(
        intent=intent,
        tool_name="council_review",
        args=args,
        deadline_seconds=600,
        channel=channel,
        thread_ts=args.get("_thread_ts"),
    )


def tool_personalize_to_csv(args: dict) -> dict:
    """Run Strategy → Research → Copy on a lead handle and post a CSV to Slack
    with personalized email_1/2/3 subject + body per prospect. NO Smartlead
    writes — Justin imports the CSV himself.

    Routes through the durable task ledger: the supervisor spawns a worker
    subprocess that does the work, posts progress to this thread, and uploads
    the CSV when done. Survives concierge restart, supervisor restart, and
    transient worker crashes (with one automatic retry on stall).

    args:
      lead_handle: handle from ingest_csv_from_slack / load_leads_from_smartlead /
                    prospect_fetch_confirmed
      niche, offer, variant: which cached brief to use (selects the campaign's
                                voice + framing). Same values as launch_pilot.
      filename:    optional CSV filename (default: <campaign>-<ts>.csv)

    The CSV columns are Smartlead-import-compatible:
      email, first_name, last_name, company_name, title, linkedin_url,
      email_1_subject, email_1_body, email_2_subject, email_2_body,
      email_3_subject, email_3_body, slop_pass, signal_tier
    """
    from squads.smartlead.squad import make_campaign_name
    handle = args["lead_handle"]
    niche = args.get("niche")
    offer = args["offer"]
    variant = args.get("variant", "A")
    if handle not in LEAD_HANDLES:
        return {"error": f"unknown lead_handle '{handle}' — call ingest or fetch first"}
    leads_count = len(LEAD_HANDLES[handle].get("leads", []))
    campaign_name = make_campaign_name(niche, offer, variant)
    intent = (f"personalize {leads_count} leads → CSV against "
                f"{campaign_name} brief")
    channel = os.environ.get("SLACK_CONTROL_CHANNEL", "#cold-email-control")
    return _route_to_ledger(
        intent=intent,
        tool_name="personalize_to_csv",
        args=args,
        # Allow ~1 hr per 1k leads, min 30min, max 4hr
        deadline_seconds=max(1800, min(14400, 60 * leads_count // 17)),
        channel=channel,
        thread_ts=args.get("_thread_ts"),
    )


# Original in-process implementation kept under a new name for legacy callers
# (e.g. v1 callers from before the ledger). Workers do not call this — they
# use orchestrator.worker._run_personalize_direct which has the same logic
# but takes leads-from-disk via the serialized handle.
def _legacy_tool_personalize_to_csv_inprocess(args: dict) -> dict:
    """LEGACY: kept for any caller that needs the old in-process behavior.
    Use `tool_personalize_to_csv` (ledger-routed) for all new calls."""
    from squads.copy import CopySquad
    from squads.research import ResearchSquad
    from squads.strategy import StrategySquad
    from squads.smartlead.squad import make_campaign_name

    handle = args["lead_handle"]
    niche = args.get("niche")
    offer = args["offer"]
    variant = args.get("variant", "A")
    if handle not in LEAD_HANDLES:
        return {"error": f"unknown lead_handle '{handle}' — call ingest or fetch first"}
    leads = LEAD_HANDLES[handle]["leads"]
    source = LEAD_HANDLES[handle]["source"]
    n = len(leads)
    campaign_name = make_campaign_name(niche, offer, variant)

    sc = SlackClient()
    channel = os.environ.get("SLACK_CONTROL_CHANNEL", "#cold-email-control")
    thread_ts = args.get("_thread_ts")

    # Throttled progress callback — same shape as launch_pilot
    last_msg = {"key": ""}
    def progress_post(stage: str, done: int, total: int, detail: str = ""):
        key = f"{stage}:{done // 100}" if stage == "copy" else f"{stage}:{done}"
        if key == last_msg["key"]:
            return
        last_msg["key"] = key
        msg = (f":hourglass_flowing_sand: `{campaign_name}` (CSV) · "
                f"*{stage}* {done}/{total}" + (f" · {detail}" if detail else ""))
        try:
            sc.post(channel, msg, thread_ts=thread_ts)
        except Exception as e:
            print(f"[personalize_to_csv] progress post failed: {e}", flush=True)

    async def _runner():
        import csv
        from squads.research.squad import Lead

        # 1. Strategy brief (cached per campaign, same as launch_pilot)
        brief_dir = REPO_ROOT / "data" / "campaigns" / campaign_name
        brief_dir.mkdir(parents=True, exist_ok=True)
        brief_path = brief_dir / "brief.md"
        if brief_path.exists():
            brief = brief_path.read_text()
            progress_post("strategy", 1, 1, "cached")
        else:
            progress_post("strategy", 0, 1, "generating brief")
            from tools.lead_loader import lead_summary
            strategy = StrategySquad()
            brief = await strategy.build_brief(offer, lead_summary(leads),
                                                  niche=niche, variant=variant)
            brief_path.write_text(brief)
            progress_post("strategy", 1, 1, f"{len(brief)} chars")

        # 2. Research (concurrent, configurable)
        research_parallel = int(os.environ.get("CE2_RESEARCH_PARALLEL", "30"))
        progress_post("research", 0, n, f"max_parallel={research_parallel}")
        research = ResearchSquad(brief=brief)
        t0 = dt.datetime.utcnow()
        signals = await research.research_batch(leads, max_parallel=research_parallel)
        r_dt = (dt.datetime.utcnow() - t0).total_seconds()
        progress_post("research", n, n, f"{r_dt:.0f}s")

        # 3. Copy (concurrent)
        copy_parallel = int(os.environ.get("CE2_COPY_PARALLEL", "20"))
        progress_post("copy", 0, n, f"max_parallel={copy_parallel}")
        copy = CopySquad(brief=brief)
        sem = asyncio.Semaphore(copy_parallel)
        done = {"n": 0}
        last = {"t": dt.datetime.utcnow()}

        async def write_one(lead, signal):
            async with sem:
                try:
                    return await copy.write_one(lead, signal)
                except Exception as e:
                    print(f"[personalize_to_csv] copy failed for {lead.email}: {e}",
                            flush=True)
                    return {"sequence": [], "slop_pass": False, "error": str(e)}
                finally:
                    done["n"] += 1
                    now = dt.datetime.utcnow()
                    if (done["n"] % 25 == 0 or
                            (now - last["t"]).total_seconds() > 30):
                        progress_post("copy", done["n"], n, "")
                        last["t"] = now

        c_t0 = dt.datetime.utcnow()
        emails = await asyncio.gather(*(write_one(l, s)
                                          for l, s in zip(leads, signals)))
        c_dt = (dt.datetime.utcnow() - c_t0).total_seconds()
        slop_pass = sum(1 for e in emails if e.get("slop_pass"))
        progress_post("copy", n, n, f"{c_dt:.0f}s · {slop_pass}/{n} clean")

        # 4. Filter empty rows + write CSV
        exports_dir = REPO_ROOT / "data" / "exports"
        exports_dir.mkdir(parents=True, exist_ok=True)
        ts = dt.datetime.utcnow().strftime("%Y%m%dT%H%M%S")
        filename = args.get("filename") or f"{campaign_name}-{ts}.csv"
        if not filename.endswith(".csv"):
            filename += ".csv"
        out_path = exports_dir / filename

        cols = [
            "email", "first_name", "last_name", "company_name", "title",
            "linkedin_url",
            "email_1_subject", "email_1_body",
            "email_2_subject", "email_2_body",
            "email_3_subject", "email_3_body",
            "slop_pass", "signal_tier",
        ]
        rows_written = 0
        rows_skipped_empty = 0
        with out_path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=cols, quoting=csv.QUOTE_ALL)
            w.writeheader()
            for lead, email_obj, signal in zip(leads, emails, signals):
                seq = email_obj.get("sequence") or []
                step1 = next((s for s in seq if s.get("step") == 1), None)
                step2 = next((s for s in seq if s.get("step") == 2), None)
                step3 = next((s for s in seq if s.get("step") == 3), None)
                # Drop prospects whose step 1 is empty — same standard as
                # launch_pilot's hard-fail, but here we drop the row (the CSV
                # is partially-recoverable; the campaign upload was not).
                if not step1 or not (step1.get("body") or "").strip():
                    rows_skipped_empty += 1
                    continue
                full = (lead.name or "")
                first, _, last = full.partition(" ")
                w.writerow({
                    "email": lead.email or "",
                    "first_name": first,
                    "last_name": last,
                    "company_name": lead.company or "",
                    "title": lead.title or "",
                    "linkedin_url": getattr(lead, "linkedin_url", "") or "",
                    "email_1_subject": (step1.get("subject") or "") if step1 else "",
                    "email_1_body": (step1.get("body") or "") if step1 else "",
                    "email_2_subject": (step2.get("subject") or "") if step2 else "",
                    "email_2_body": (step2.get("body") or "") if step2 else "",
                    "email_3_subject": (step3.get("subject") or "") if step3 else "",
                    "email_3_body": (step3.get("body") or "") if step3 else "",
                    "slop_pass": "1" if email_obj.get("slop_pass") else "0",
                    "signal_tier": signal.get("tier", "") if signal else "",
                })
                rows_written += 1

        # 5. Upload CSV to Slack (file attachment in the thread)
        total_dt = (dt.datetime.utcnow() - t0).total_seconds()
        comment = (
            f":white_check_mark: *Personalized CSV ready* — `{campaign_name}`\n"
            f"  • Source: {source}  ({n} leads in)\n"
            f"  • CSV rows: {rows_written}  (skipped {rows_skipped_empty} empty-copy)\n"
            f"  • Slop-clean: {slop_pass}/{n}\n"
            f"  • Times: research {r_dt:.0f}s · copy {c_dt:.0f}s · "
            f"*total {total_dt:.0f}s*\n"
            f"  • Smartlead import: drag this CSV into a campaign's *Add Leads → "
            f"Upload CSV*. Map `email_1_subject` / `email_1_body` etc. to "
            f"custom fields on first import; mappings persist after that."
        )
        sc.upload_file(channel, out_path,
                         title=f"{campaign_name} personalized leads",
                         initial_comment=comment,
                         thread_ts=thread_ts)

    task_id = _new_handle("personalize")
    task = asyncio.create_task(_runner())

    def _on_done(t: asyncio.Task):
        if t.cancelled():
            sc.post(channel, f":x: `{campaign_name}` (CSV) was *cancelled*",
                     thread_ts=thread_ts)
            return
        exc = t.exception()
        if exc is None:
            return
        import traceback as tb
        err = "".join(tb.format_exception(type(exc), exc, exc.__traceback__))
        tail = err[-1500:]
        try:
            sc.post(channel,
                      f":x: *personalize_to_csv FAILED* — `{campaign_name}`\n"
                      f"```\n{tail}\n```",
                      thread_ts=thread_ts)
        except Exception as post_e:
            print(f"[personalize_to_csv] failed to post error: {post_e}\n"
                   f"{err}", flush=True)
        try:
            crash_dir = REPO_ROOT / "data" / "exports"
            crash_dir.mkdir(parents=True, exist_ok=True)
            (crash_dir / f"CRASH-{campaign_name}-"
                f"{dt.datetime.utcnow().strftime('%Y%m%dT%H%M%S')}.txt"
            ).write_text(err)
        except Exception:
            pass

    task.add_done_callback(_on_done)
    RUNNING_PILOTS[task_id] = task
    return {"started": True, "task_id": task_id, "leads": n,
            "campaign": campaign_name,
            "note": ("Personalizing in background. Progress will post here. "
                       "When done, I'll upload the CSV as a file in this thread.")}


def tool_preview_emails(args: dict) -> dict:
    """Render N sample drafted emails (subject + body for sequence 1/2/3) for a
    given campaign so the user can spot-check copy quality before sending.

    Reads from data/emails/<email>.json (where the Copy squad writes per-prospect
    sequences). Filters by the lead emails currently attached to the named
    campaign in Smartlead.
    """
    from tools.smartlead import SmartleadCLI
    name = args["campaign_name"]
    n = int(args.get("n", 5))
    step_filter = int(args["step"]) if args.get("step") else None

    # 1. Look up campaign + its current leads
    cli = SmartleadCLI()
    camp = cli.get_campaign_by_name(name)
    if not camp:
        return {"error": f"no Smartlead campaign named '{name}'"}
    cid = camp.get("id")
    raw_leads = cli.list_campaign_leads(cid, all_pages=True)
    emails_in_camp = set()
    for item in raw_leads:
        l = item.get("lead") if isinstance(item.get("lead"), dict) else item
        e = (l.get("email") or "").lower()
        if e:
            emails_in_camp.add(e)

    if not emails_in_camp:
        return {"error": f"campaign '{name}' has no leads attached yet — run "
                          f"launch_pilot first to populate it"}

    # 2. Walk data/emails/ and pull matching files
    emails_dir = REPO_ROOT / "data" / "emails"
    if not emails_dir.exists():
        return {"error": "data/emails/ does not exist — no copy generated yet"}
    samples = []
    for f in sorted(emails_dir.glob("*.json")):
        try:
            data = json.loads(f.read_text())
        except Exception:
            continue
        lead = data.get("lead") or {}
        email_addr = (lead.get("email") or data.get("email") or "").lower()
        if email_addr not in emails_in_camp:
            continue
        seq = (data.get("emails") or {}).get("sequence") or data.get("sequence") or []
        sample = {
            "name": lead.get("name", ""),
            "email": email_addr,
            "company": lead.get("company", ""),
            "title": lead.get("title", ""),
            "slop_pass": data.get("slop_pass"),
            "steps": [],
        }
        for step in seq:
            si = step.get("step", 0)
            if step_filter and si != step_filter:
                continue
            sample["steps"].append({
                "step": si,
                "subject": step.get("subject", ""),
                "body": step.get("body", ""),
            })
        if sample["steps"]:
            samples.append(sample)
        if len(samples) >= n:
            break

    if not samples:
        return {"campaign_name": name, "leads_in_campaign": len(emails_in_camp),
                "samples": [],
                "note": ("Campaign has leads but no per-prospect copy in "
                          "data/emails/ yet — pipeline hasn't run for these leads. "
                          "Run launch_pilot first.")}
    return {"campaign_name": name, "leads_in_campaign": len(emails_in_camp),
            "samples_returned": len(samples), "samples": samples}


def tool_import_csv_to_smartlead(args: dict) -> dict:
    """Import a personalized-leads CSV (from data/exports/) directly into a
    Smartlead campaign with custom_fields populated from email_1/2/3 columns.

    Use after personalize_to_csv produces a CSV. Maps email_1_subject /
    email_1_body / email_2_subject / etc. to Smartlead custom_fields so the
    campaign's sequence template substitutes per-prospect copy at send-time.

    args:
      csv_path:    path under data/exports/ (or absolute)
      campaign_id: Smartlead campaign id (int or string)
    """
    from tools.csv_to_smartlead import import_csv
    return import_csv(args["csv_path"], args["campaign_id"])


def tool_update_campaign_schedule(args: dict) -> dict:
    """Update the schedule (sending hours, daily cap, start time, timezone)
    on a Smartlead campaign. Required before flipping DRAFTED → ACTIVE for
    a campaign that doesn't already have a schedule. After update, call
    schedule_campaign(campaign_id) to flip it ACTIVE.

    args:
      campaign_id: Smartlead campaign id
      timezone:    e.g. 'America/New_York' (default)
      days_of_week: list of int 1=Mon..7=Sun (default M-F = [1,2,3,4,5])
      start_hour:  HH:MM (default '08:00')
      end_hour:    HH:MM (default '17:00')
      min_time_btw_emails: minutes (default 5)
      max_new_leads_per_day: int (default 50)
      schedule_start_iso: absolute UTC ISO timestamp for first allowed send
                            (default tomorrow at start_hour in timezone)
    """
    import datetime as dt
    import json as _json
    import tempfile
    from tools.smartlead import SmartleadCLI

    cid = args["campaign_id"]
    schedule = {
        "timezone": args.get("timezone", "America/New_York"),
        "days_of_the_week": args.get("days_of_week", [1, 2, 3, 4, 5]),
        "start_hour": args.get("start_hour", "08:00"),
        "end_hour": args.get("end_hour", "17:00"),
        "min_time_btw_emails": int(args.get("min_time_btw_emails", 5)),
        "max_new_leads_per_day": int(args.get("max_new_leads_per_day", 50)),
    }
    if args.get("schedule_start_iso"):
        schedule["schedule_start_time"] = args["schedule_start_iso"]
    else:
        # default: tomorrow at start_hour ET → UTC
        # 08:00 ET in DST = 12:00 UTC. Approx; user can override.
        tomorrow = (dt.datetime.now(dt.UTC) + dt.timedelta(days=1)).date()
        schedule["schedule_start_time"] = (
            f"{tomorrow.isoformat()}T12:00:00.000Z"
        )

    cli = SmartleadCLI()
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        _json.dump(schedule, fh)
        body_path = fh.name
    try:
        result = cli._run_json(
            ["campaigns", "update-schedule", "--id", str(cid),
             "--from-json", body_path])
    finally:
        try:
            os.unlink(body_path)
        except Exception:
            pass
    return {"campaign_id": cid, "schedule_set": schedule, "result": result}


def tool_schedule_campaign(args: dict) -> dict:
    """Move a DRAFTED Smartlead campaign to ACTIVE (status=START). No timing
    args because Smartlead respects the schedule already configured on the
    campaign in the UI; this just flips the on switch.

    For complex schedule changes (sending hours, daily caps), use the Smartlead
    UI — that surface is richer than what we want to recreate here.
    """
    from tools.smartlead import SmartleadCLI
    cid = args["campaign_id"]
    cli = SmartleadCLI()
    result = cli.set_status(cid, "START")
    return {"campaign_id": cid, "new_status": "START", "result": result}


def tool_generate_preview_pack(args: dict) -> dict:
    """Generate sample emails for one or more pre-created campaigns WITHOUT
    writing anything to Smartlead. Pulls a small lead sample (default 3) from
    a source Smartlead campaign, runs them through Research + Copy with each
    target's cached brief, and returns the rendered subjects + bodies.

    Use this BEFORE launch_pilot when the user wants to "see examples" or
    "preview the copy" for the pre-created campaigns. Cheap (~$0.30 per
    target × N leads), takes ~60-90 seconds per target.

    args:
      source_campaign_id: int|str — Smartlead campaign holding the recruiter list
      targets:           list[str] — campaign names to generate previews for,
                          e.g. ["recruiters-power-partner-A","recruiters-power-partner-B"]
      n:                 int — leads to sample (default 3)
    """
    import asyncio as _asyncio
    from squads.copy import CopySquad
    from squads.research import ResearchSquad
    from tools.lead_loader import load_leads_from_campaign

    source_id = args["source_campaign_id"]
    targets = args["targets"]
    if isinstance(targets, str):
        targets = [t.strip() for t in targets.split(",") if t.strip()]
    n = int(args.get("n", 3))

    sc = SlackClient()
    channel = os.environ.get("SLACK_CONTROL_CHANNEL", "#cold-email-control")
    thread_ts = args.get("_thread_ts")

    async def _runner():
        # 1. Pull leads ONCE (cheap, no copies in pipeline)
        all_leads = load_leads_from_campaign(source_id, max_n=max(n, 5))
        if not all_leads:
            sc.post(channel, f":x: Source Smartlead campaign `{source_id}` has no leads.",
                     thread_ts=thread_ts)
            return
        leads = all_leads[:n]
        sc.post(channel,
                 f":hourglass_flowing_sand: Generating previews for {len(targets)} campaigns "
                 f"× {len(leads)} leads each. Source: campaign `{source_id}`.",
                 thread_ts=thread_ts)

        for target in targets:
            try:
                brief_path = REPO_ROOT / "data" / "campaigns" / target / "brief.md"
                if not brief_path.exists():
                    sc.post(channel,
                             f":warning: `{target}` has no cached brief — skipping. "
                             f"Run `precreate_campaigns` first.", thread_ts=thread_ts)
                    continue
                brief = brief_path.read_text()

                research = ResearchSquad(brief=brief)
                signals = await research.research_batch(leads, max_parallel=len(leads))

                copy = CopySquad(brief=brief)
                emails_out = []
                for lead, sig in zip(leads, signals):
                    emails_out.append(await copy.write_one(lead, sig))

                # Render to Slack mrkdwn — one block per lead
                lines = [f"*:envelope: {target}* — {len(emails_out)} sample(s)"]
                for lead, em in zip(leads, emails_out):
                    seq = (em.get("sequence") or [])
                    slop = ":white_check_mark:" if em.get("slop_pass") else ":warning: slop"
                    lines.append(f"\n———\n*{lead.name}* — {lead.title} @ {lead.company}  {slop}")
                    for step in seq:
                        si = step.get("step", "?")
                        subj = step.get("subject", "")
                        body = step.get("body", "")
                        lines.append(f"\n*Email {si}* — `{subj}`\n```\n{body[:1200]}\n```")
                # Slack hard caps message at 40k; chunk if needed
                msg = "\n".join(lines)
                for chunk in _slack_chunk(msg, 3500):
                    sc.post(channel, chunk, thread_ts=thread_ts)
            except Exception as e:
                tb = traceback.format_exc()
                print(f"[generate_preview_pack] {target} failed: {tb}", flush=True)
                sc.post(channel, f":x: `{target}` preview failed: {type(e).__name__}: {e}",
                         thread_ts=thread_ts)

        sc.post(channel,
                 f":white_check_mark: Preview pack done. Reply *approve* to proceed with "
                 f"a full `launch_pilot` to those campaigns, or tell me what to change.",
                 thread_ts=thread_ts)

    task_id = _new_handle("preview")
    RUNNING_PILOTS[task_id] = asyncio.create_task(_runner())
    return {"started": True, "task_id": task_id, "targets": targets, "n": n,
            "note": "Running in background. I will post each campaign's samples in this thread as they complete (~60-90s each)."}


def _slack_chunk(text: str, limit: int = 3500) -> list[str]:
    """Split a long Slack message at line breaks to stay under message limits."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    cur = ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > limit:
            if cur:
                chunks.append(cur)
            cur = line
        else:
            cur = (cur + "\n" + line) if cur else line
    if cur:
        chunks.append(cur)
    return chunks


def tool_update_voice_rules(args: dict) -> dict:
    """Append or replace global voice rules (Justin's tone overrides for every
    campaign). Writes data/voice_rules.md. Loaded by the Copy squad on top of
    every brief.

    args.append: text to append to the current rules (preserves existing).
    args.replace: full replacement of voice_rules.md (use sparingly).

    Use this when the user gives feedback like "stop saying X across all
    campaigns" or "the voice should sound more like Y" — feedback that's
    NOT specific to one campaign brief.
    """
    p = REPO_ROOT / "data" / "voice_rules.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    if "replace" in args:
        p.write_text(args["replace"])
        return {"updated": True, "mode": "replace", "chars": len(args["replace"]),
                  "file": "data/voice_rules.md"}
    if "append" in args:
        existing = p.read_text() if p.exists() else ""
        block = (
            f"\n\n## Update — {dt.datetime.utcnow().date().isoformat()}\n\n"
            + args["append"]
        )
        p.write_text(existing + block)
        return {"updated": True, "mode": "append",
                  "appended_chars": len(args["append"]), "file": "data/voice_rules.md"}
    return {"error": "supply either 'append' or 'replace'"}


def tool_update_brief(args: dict) -> dict:
    """Append a feedback block to a specific campaign's brief, OR replace it
    entirely. Writes data/campaigns/<campaign_name>/brief.md.

    Use this when the user gives feedback that's specific to ONE campaign's
    angle, hook, or red lines (e.g. "for direct-value-A, drop the 14-day
    sprint mention"). For feedback that applies to ALL campaigns, use
    update_voice_rules instead.
    """
    name = args["campaign_name"]
    bp = REPO_ROOT / "data" / "campaigns" / name / "brief.md"
    if not bp.parent.exists():
        return {"error": f"no campaign at {bp.parent.relative_to(REPO_ROOT)}"}

    if "replace" in args:
        bp.write_text(args["replace"])
        return {"updated": True, "mode": "replace", "campaign_name": name,
                  "chars": len(args["replace"])}
    if "append" in args:
        existing = bp.read_text() if bp.exists() else ""
        block = (
            f"\n\n## Update — {dt.datetime.utcnow().date().isoformat()}\n\n"
            + args["append"]
        )
        bp.write_text(existing + block)
        return {"updated": True, "mode": "append", "campaign_name": name,
                  "appended_chars": len(args["append"])}
    return {"error": "supply either 'append' or 'replace'"}


def tool_log_capability_gap(args: dict) -> dict:
    """Append a row to data/skill_gaps.jsonl when the user asks for something
    the agent has no tool for. Call this BEFORE telling the user you can't
    do it — that way we get a logged backlog of features to build.

    Justin reviews data/skill_gaps.jsonl periodically; recurring gaps become
    the next tools in the registry.
    """
    gap_file = REPO_ROOT / "data" / "skill_gaps.jsonl"
    gap_file.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "logged_at": dt.datetime.utcnow().isoformat(),
        "user_request": args.get("user_request", ""),
        "missing_tool_name": args.get("missing_tool_name", ""),
        "what_it_would_do": args.get("what_it_would_do", ""),
        "workaround_used": args.get("workaround_used", ""),
    }
    with gap_file.open("a") as f:
        f.write(json.dumps(row) + "\n")
    return {"logged": True, "file": str(gap_file.relative_to(REPO_ROOT))}


def tool_archive_campaign(args: dict) -> dict:
    from tools.smartlead import SmartleadCLI
    cid = args["campaign_id"]
    mode = args.get("mode", "STOP").upper()
    cli = SmartleadCLI()
    if mode == "STOP":
        result = cli.set_status(cid, "STOPPED")
    elif mode == "DELETE":
        result = cli.delete_campaign(cid)
    else:
        return {"error": f"unknown mode {mode}; use STOP or DELETE"}
    return {"campaign_id": cid, "mode": mode, "result": result}


def tool_precreate_campaigns(args: dict) -> dict:
    from orchestrator.precreate import precreate_many
    niche = args["niche"]
    offers = args["offers"] if isinstance(args["offers"], list) else args["offers"].split(",")
    variants = args.get("variants", ["A", "B"])
    if isinstance(variants, str):
        variants = variants.split(",")

    async def _go():
        return await precreate_many(niche, offers, variants)
    results = asyncio.get_event_loop().run_until_complete(_go()) if False else None
    # We're already in the event loop — use a task instead
    task = asyncio.create_task(precreate_many(niche, offers, variants))
    RUNNING_PILOTS[_new_handle("precreate")] = task
    return {"started": True, "niche": niche, "offers": offers, "variants": variants,
            "note": "Running in background; will not auto-report on completion. Call list_active_campaigns when ready."}


# Registry — the LLM sees this schema
TOOLS = [
    {"name": "list_active_campaigns",
     "description": "List Smartlead campaigns currently in DRAFTED/ACTIVE/STARTED/PAUSED state.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "list_briefs",
     "description": "List all cached campaign briefs (data/campaigns/*/brief.md) with previews.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "read_brief",
     "description": "Read the full cached brief for a campaign by name.",
     "input_schema": {"type": "object", "required": ["campaign_name"],
                       "properties": {"campaign_name": {"type": "string"}}}},
    {"name": "stats",
     "description": "Operational stats: campaigns by (niche, offer, variant), reply counts.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "ingest_csv_from_slack",
     "description": "Download a CSV the user uploaded to Slack and load it into a lead handle. Required CSV columns: name, email, company, title (linkedin_url optional).",
     "input_schema": {"type": "object", "required": ["file_id"],
                       "properties": {"file_id": {"type": "string"}}}},
    {"name": "pull_prospector_nl",
     "description": "Translate a natural-language description to Smartlead Prospector filters and run a FREE search. Returns count + filter_id. Does NOT consume credits. Always confirm with the user before calling prospect_fetch_confirmed.",
     "input_schema": {"type": "object", "required": ["description"],
                       "properties": {"description": {"type": "string",
                          "description": "e.g. 'recruiting agency owners in US, 11-50 employees, founder/owner seniority'"}}}},
    {"name": "pull_prospector_saved",
     "description": "Run a FREE prospect search against an existing Smartlead saved-search ID. Returns count + filter_id. Always confirm with the user before calling prospect_fetch_confirmed.",
     "input_schema": {"type": "object", "required": ["saved_search_id"],
                       "properties": {"saved_search_id": {"type": "string"}}}},
    {"name": "prospect_fetch_confirmed",
     "description": "CONSUMES SMARTLEAD CREDITS. Fetch leads from a previously-run prospect search (supply filters OR saved_search_id, plus max_fetch). Only call after the user explicitly says yes/proceed/confirmed. Returns a lead handle to use with launch_pilot.",
     "input_schema": {"type": "object",
                       "required": ["max_fetch"],
                       "properties": {
                           "filters": {"type": "object"},
                           "saved_search_id": {"type": "string"},
                           "max_fetch": {"type": "integer"}}}},
    {"name": "launch_pilot",
     "description": "Run the 4-squad pipeline: research + copy + add leads to the (niche, offer, variant) Smartlead campaign. Long-running (2-5 min). Reports back to this thread when done. Only call after the user explicitly says yes/proceed/launch. Pre-created recruiter campaigns are: recruiters-{power-partner,direct-value,capstone}-{A,B}.",
     "input_schema": {"type": "object",
                       "required": ["lead_handle", "offer"],
                       "properties": {
                           "lead_handle": {"type": "string"},
                           "niche": {"type": "string"},
                           "offer": {"type": "string",
                              "description": "power-partner | direct-value | capstone"},
                           "variant": {"type": "string", "default": "A"}}}},
    {"name": "personalize_to_csv",
     "description": "PREFERRED PATH for getting personalized emails to Justin fast. Run Strategy + Research + Copy on a lead handle and post a CSV file to this Slack thread with email_1/2/3 subject + body per prospect. NO Smartlead writes — Justin imports the CSV himself in the Smartlead UI. Use this when the user says 'personalize these leads', 'just give me the CSV', 'I'll upload to Smartlead myself', or any time reliability matters more than the integrated pipeline. Same hard-fail-on-empty guard as launch_pilot. ~2-5 min for a few hundred leads, ~25-35 min for 5,000. Posts progress as research/copy advance.",
     "input_schema": {"type": "object",
                       "required": ["lead_handle", "offer"],
                       "properties": {
                           "lead_handle": {"type": "string"},
                           "niche": {"type": "string"},
                           "offer": {"type": "string",
                              "description": "power-partner | direct-value | capstone — selects the cached brief"},
                           "variant": {"type": "string", "default": "A"},
                           "filename": {"type": "string",
                              "description": "optional CSV filename (default: <campaign>-<timestamp>.csv)"}}}},
    {"name": "load_leads_from_smartlead",
     "description": "Pull leads OUT of an existing Smartlead campaign (by ID) and load them into a lead handle. FREE, no credits consumed. Use when the user says 'use the leads already in Smartlead campaign X' or 'use those existing recruiter leads'. After this, ask the user which target (niche, offer, variant) campaign(s) to launch them into.",
     "input_schema": {"type": "object", "required": ["campaign_id"],
                       "properties": {"campaign_id": {"type": ["string", "integer"]},
                                       "max_n": {"type": "integer"}}}},
    {"name": "preview_emails",
     "description": "Render N sample drafted emails for a campaign so the user can spot-check the copy BEFORE flipping the campaign to ACTIVE. Reads from data/emails/. Use when the user wants to see what the actual emails look like (e.g. 'show me 5 examples of email 1 for recruiters-power-partner-A').",
     "input_schema": {"type": "object", "required": ["campaign_name"],
                       "properties": {
                           "campaign_name": {"type": "string"},
                           "n": {"type": "integer", "default": 5},
                           "step": {"type": "integer",
                              "description": "1, 2, or 3 — restrict to one sequence step (default: all 3)"}}}},
    {"name": "generate_preview_pack",
     "description": "Generate sample emails for a SET of campaigns WITHOUT writing to Smartlead. Pulls n leads from a source Smartlead campaign, runs Research + Copy with each target's cached brief, posts the rendered subjects+bodies to this Slack thread. Use when the user says 'show me examples', 'preview the copy', or 'what would the emails look like'. Costs ~$0.30 × n × len(targets), takes ~60-90s per target.",
     "input_schema": {"type": "object",
                       "required": ["source_campaign_id", "targets"],
                       "properties": {
                           "source_campaign_id": {"type": ["string", "integer"],
                              "description": "Smartlead campaign ID holding the source leads"},
                           "targets": {"type": "array", "items": {"type": "string"},
                              "description": "list of campaign names, e.g. ['recruiters-power-partner-A','recruiters-direct-value-A']"},
                           "n": {"type": "integer", "default": 3,
                              "description": "leads to sample per target (default 3)"}}}},
    {"name": "import_csv_to_smartlead",
     "description": "Import a personalized-leads CSV directly into a Smartlead campaign with custom_fields populated from email_1/2/3 subject + body columns. Use after personalize_to_csv produces a CSV that matches an existing campaign's brief. Returns counts: leads_sent / total_leads (added) / duplicate_count / block_count / already_added_to_campaign.",
     "input_schema": {"type": "object", "required": ["csv_path", "campaign_id"],
                       "properties": {
                           "csv_path": {"type": "string"},
                           "campaign_id": {"type": ["string", "integer"]}}}},
    {"name": "update_campaign_schedule",
     "description": "Configure a Smartlead campaign's schedule (sending hours, days, daily cap, timezone, first allowed send time). REQUIRED before flipping a campaign DRAFTED → ACTIVE if it has no schedule yet — otherwise schedule_campaign succeeds but no emails actually send. Defaults: M-F 8am-5pm ET, 50 leads/day, 5 min between emails, first send tomorrow.",
     "input_schema": {"type": "object", "required": ["campaign_id"],
                       "properties": {
                           "campaign_id": {"type": ["string", "integer"]},
                           "timezone": {"type": "string"},
                           "days_of_week": {"type": "array",
                              "items": {"type": "integer",
                                          "minimum": 1, "maximum": 7}},
                           "start_hour": {"type": "string",
                              "description": "HH:MM, default 08:00"},
                           "end_hour": {"type": "string"},
                           "min_time_btw_emails": {"type": "integer"},
                           "max_new_leads_per_day": {"type": "integer"},
                           "schedule_start_iso": {"type": "string",
                              "description": "absolute UTC ISO timestamp for first allowed send"}}}},
    {"name": "schedule_campaign",
     "description": "Flip a Smartlead campaign from DRAFTED to ACTIVE (start sending). Smartlead's existing schedule (hours, daily cap) on the campaign is respected — this just flips the on switch. DESTRUCTIVE — only call after explicit user confirm AND only after preview_emails has been shown.",
     "input_schema": {"type": "object", "required": ["campaign_id"],
                       "properties": {"campaign_id": {"type": ["string", "integer"]}}}},
    {"name": "update_voice_rules",
     "description": "Append or replace Justin's global voice rules (data/voice_rules.md). The Copy squad loads these on top of every campaign brief — changes propagate to the next preview / launch. Use when the user gives feedback that applies across ALL campaigns ('stop saying X', 'voice should be Y'). Use update_brief instead for feedback that's specific to ONE campaign.",
     "input_schema": {"type": "object",
                       "properties": {
                           "append": {"type": "string",
                              "description": "Markdown block to append to the rules. Preserves existing rules and adds dated section."},
                           "replace": {"type": "string",
                              "description": "Full replacement of voice_rules.md. Use sparingly."}}}},
    {"name": "update_brief",
     "description": "Append or replace a specific campaign's brief at data/campaigns/<name>/brief.md. Use for feedback that's specific to ONE campaign's angle, hook, or red lines (e.g. 'for direct-value-A specifically, drop the 14-day mention'). For cross-campaign feedback, use update_voice_rules.",
     "input_schema": {"type": "object", "required": ["campaign_name"],
                       "properties": {
                           "campaign_name": {"type": "string"},
                           "append": {"type": "string"},
                           "replace": {"type": "string"}}}},
    {"name": "list_my_tasks",
     "description": "Read-only. Returns all tasks (in-flight and completed) recorded in this Slack thread. Call before answering 'what are you working on?' / 'is X done yet?' / 'status?' — NEVER guess from memory; always read the ledger. Returns id, intent, status, stage, progress, attempts, depth, parent.",
     "input_schema": {"type": "object", "properties": {
                           "thread_ts": {"type": "string",
                              "description": "if absent, uses current thread"}}}},
    {"name": "cancel_task",
     "description": "Cancel an in-flight task by id. Marks the task and all its descendant subagents as cancelled in the ledger; the workers detect this on their next progress callback and exit cleanly. Idempotent.",
     "input_schema": {"type": "object", "required": ["task_id"],
                       "properties": {"task_id": {"type": "string"}}}},
    {"name": "spawn_subagent",
     "description": "Delegate work to a subagent (a child task that runs in its own subprocess with its own LLM context, scoped tool subset, and own ledger entry). Use for: (1) parallel fan-out — e.g. personalize the same leads against 6 different briefs in parallel; (2) different model tier — Haiku for cheap batch validation, Opus for synthesis; (3) recursive decomposition — subagent itself can spawn subagents up to max_depth=3. Children report results up to the parent via the ledger. Only the concierge (this agent) talks to Slack; subagents return data, not messages. Returns immediately with a task_id; check status with list_my_tasks.",
     "input_schema": {"type": "object",
                       "required": ["intent", "tool_call"],
                       "properties": {
                           "role": {"type": "string",
                              "description": "short role label, e.g. 'personalizer-A', 'voice-critic'"},
                           "intent": {"type": "string"},
                           "tool_call": {"type": "object",
                              "description": "{name, args} — the deterministic tool the subagent runs"},
                           "children": {"type": "array",
                              "description": "OPTIONAL batch fan-out: list of {role, intent, tool_call} — runs all in parallel, returns when all settle",
                              "items": {"type": "object"}},
                           "max_depth": {"type": "integer", "default": 3},
                           "timeout_seconds": {"type": "integer", "default": 1800}}}},
    {"name": "council_review",
     "description": "Spawn N parallel critic subagents (one per criterion), each reads the target and returns a pass/fail verdict + reasoning. Use BEFORE delivering high-stakes outputs to ensure quality (e.g. before posting a CSV: voice-critic, threading-critic, deliverability-critic). Returns aggregate {pass, blocking_count, verdicts}. If any critic blocks, surface the verdict to Justin instead of delivering.",
     "input_schema": {"type": "object",
                       "required": ["criteria", "target"],
                       "properties": {
                           "criteria": {"type": "array", "items": {"type": "string"},
                              "description": "list of single-criterion descriptions, e.g. ['voice rules compliance', 'no urls in email 1', 'thread subjects match']"},
                           "target": {"type": "string",
                              "description": "either a file path (e.g. data/exports/foo.csv) or inline content to evaluate"},
                           "max_depth": {"type": "integer", "default": 3}}}},
    {"name": "log_capability_gap",
     "description": "Call this BEFORE telling the user 'I can't do that'. Logs a row to data/skill_gaps.jsonl so recurring gaps surface as the next tools to build. After calling, give the user a clean answer about what you can do as an alternative.",
     "input_schema": {"type": "object", "required": ["user_request", "missing_tool_name"],
                       "properties": {
                           "user_request": {"type": "string"},
                           "missing_tool_name": {"type": "string"},
                           "what_it_would_do": {"type": "string"},
                           "workaround_used": {"type": "string"}}}},
    {"name": "archive_campaign",
     "description": "Stop or delete a Smartlead campaign. Destructive — only call after explicit user yes.",
     "input_schema": {"type": "object", "required": ["campaign_id"],
                       "properties": {"campaign_id": {"type": ["string", "integer"]},
                                       "mode": {"type": "string",
                                                  "enum": ["STOP", "DELETE"]}}}},
    {"name": "precreate_campaigns",
     "description": "Pre-create empty DRAFTED Smartlead campaigns for a niche × offers × variants matrix. Each combo gets a Strategy-generated brief cached. ~$2/combo, ~30s/combo.",
     "input_schema": {"type": "object", "required": ["niche", "offers"],
                       "properties": {
                           "niche": {"type": "string"},
                           "offers": {"type": "array", "items": {"type": "string"}},
                           "variants": {"type": "array", "items": {"type": "string"}}}}},
]

TOOL_FUNCS = {
    "list_active_campaigns": tool_list_active_campaigns,
    "list_briefs": tool_list_briefs,
    "read_brief": tool_read_brief,
    "stats": tool_stats,
    "ingest_csv_from_slack": tool_ingest_csv_from_slack,
    "pull_prospector_nl": tool_pull_prospector_nl,
    "pull_prospector_saved": tool_pull_prospector_saved,
    "prospect_fetch_confirmed": tool_prospect_fetch_confirmed,
    "launch_pilot": tool_launch_pilot,
    "personalize_to_csv": tool_personalize_to_csv,
    "list_my_tasks": tool_list_my_tasks,
    "cancel_task": tool_cancel_task,
    "spawn_subagent": tool_spawn_subagent,
    "council_review": tool_council_review,
    "load_leads_from_smartlead": tool_load_leads_from_smartlead,
    "preview_emails": tool_preview_emails,
    "generate_preview_pack": tool_generate_preview_pack,
    "schedule_campaign": tool_schedule_campaign,
    "import_csv_to_smartlead": tool_import_csv_to_smartlead,
    "update_campaign_schedule": tool_update_campaign_schedule,
    "update_voice_rules": tool_update_voice_rules,
    "update_brief": tool_update_brief,
    "log_capability_gap": tool_log_capability_gap,
    "archive_campaign": tool_archive_campaign,
    "precreate_campaigns": tool_precreate_campaigns,
}


# ---------------------------------------------------------------------------
# State snapshot — injected as a system message on every turn so the LLM has
# ground-truth context about the operator's actual world (current campaigns,
# lead handles, briefs, voice rules, recent runs, exports). This is the single
# biggest leverage against "agent asking dumb questions" — the LLM can no
# longer claim ignorance of state that's right in front of it.
# ---------------------------------------------------------------------------

def _build_state_snapshot(thread_ts: str | None = None) -> str:
    """Returns a compact markdown snapshot of operator state. Kept under ~6KB.
    Errors are swallowed — better to send a partial snapshot than to fail the
    whole turn.

    Includes (in order):
      1. Live Smartlead campaigns (recruiter cells + TenXVA source campaigns)
      2. Cached briefs
      3. voice_rules.md tail
      4. Active LEAD_HANDLES (in-memory)
      5. Recent pilot runs
      6. Recent CSV exports
      7. Active tasks for this Slack thread (from durable ledger)
      8. Last 24h of concierge journal entries
    """
    parts: list[str] = ["# CURRENT STATE — read this before answering",
                          "_(Auto-injected; do NOT ask Justin questions whose "
                          "answers are below.)_\n"]

    # Campaigns + lead counts (from Smartlead)
    try:
        from tools.smartlead import SmartleadCLI
        cli = SmartleadCLI()
        cs = cli.list_campaigns() or []
        # Prioritize the recruiter cells, then anything else with leads, then truncate
        recruiter = [c for c in cs if "recruit" in (c.get("name", "") or "").lower()]
        tenxva = [c for c in cs if "tenxva" in (c.get("name", "") or "").lower()][:8]
        lines = ["## Smartlead campaigns (id · status · name)"]
        for c in recruiter:
            lines.append(f"- `{c.get('id')}` · {c.get('status', '?'):8} · "
                            f"`{c.get('name', '?')}`")
        if tenxva:
            lines.append("\n_Source TenXVA campaigns (where Justin's "
                            "~894 unique recruiter leads live):_")
            for c in tenxva:
                lines.append(f"- `{c.get('id')}` · {c.get('status', '?'):8} · "
                                f"`{c.get('name', '?')}`")
        parts.append("\n".join(lines))
    except Exception as e:
        parts.append(f"_(Smartlead snapshot unavailable: {e})_")

    # Cached briefs
    try:
        briefs_dir = REPO_ROOT / "data" / "campaigns"
        briefs = sorted(briefs_dir.glob("*/brief.md"))
        if briefs:
            lines = ["## Cached briefs (data/campaigns/<name>/brief.md)"]
            for b in briefs:
                name = b.parent.name
                lines.append(f"- `{name}`  ({b.stat().st_size:,} bytes)")
            parts.append("\n".join(lines))
    except Exception:
        pass

    # Voice rules
    try:
        vr = REPO_ROOT / "data" / "voice_rules.md"
        if vr.exists():
            txt = vr.read_text()
            tail = txt[-1500:] if len(txt) > 1500 else txt
            parts.append(f"## Active voice_rules.md (last 1500 chars)\n```\n{tail}\n```")
    except Exception:
        pass

    # Active in-memory lead handles
    if LEAD_HANDLES:
        lines = ["## Active LEAD_HANDLES (in-memory; lost on daemon restart)"]
        for h, meta in list(LEAD_HANDLES.items())[-8:]:
            lines.append(f"- `{h}` · {len(meta.get('leads', []))} leads · "
                            f"source=`{meta.get('source', '?')}` · "
                            f"created `{meta.get('created_at', '?')[:19]}`")
        parts.append("\n".join(lines))
    else:
        parts.append("## Active LEAD_HANDLES\n_(none — daemon was restarted "
                        "or no leads loaded yet this session)_")

    # Recent pilot runs (last 5 from data/pilots/*.jsonl)
    try:
        pilots_dir = REPO_ROOT / "data" / "pilots"
        if pilots_dir.exists():
            jsonls = sorted(pilots_dir.glob("*.jsonl"),
                              key=lambda p: p.stat().st_mtime, reverse=True)[:5]
            if jsonls:
                lines = ["## Recent pilot runs (data/pilots/*.jsonl)"]
                for j in jsonls:
                    try:
                        last = j.read_text().strip().splitlines()[-1] if j.read_text().strip() else "{}"
                        d = json.loads(last)
                        lines.append(f"- `{j.name}` · last event "
                                        f"`{d.get('event', '?')}`")
                    except Exception:
                        lines.append(f"- `{j.name}` · (unreadable)")
                parts.append("\n".join(lines))
    except Exception:
        pass

    # Recent CSV exports (the deliverables Justin actually wants)
    try:
        exp_dir = REPO_ROOT / "data" / "exports"
        if exp_dir.exists():
            csvs = sorted(exp_dir.glob("*.csv"),
                            key=lambda p: p.stat().st_mtime, reverse=True)[:5]
            if csvs:
                lines = ["## Recent CSV exports (data/exports/)"]
                for c in csvs:
                    lines.append(f"- `{c.name}` · "
                                    f"{c.stat().st_size:,} bytes · "
                                    f"mtime `{dt.datetime.fromtimestamp(c.stat().st_mtime).isoformat()[:19]}`")
                parts.append("\n".join(lines))
    except Exception:
        pass

    # Active tasks for this thread (durable ledger — survives restarts)
    if thread_ts:
        try:
            from tools.task_ledger import get_ledger
            tasks = get_ledger().list_by_thread(thread_ts)[:8]
            if tasks:
                lines = ["## Active tasks in this thread (durable ledger)"]
                for t in tasks:
                    progress = (f"{int((t.get('progress') or 0) * 100)}%"
                                  if t.get("progress") is not None else "?")
                    lines.append(f"- `{t['id']}` · {t['status']:9} · "
                                    f"stage `{t.get('stage') or '-'}` · "
                                    f"{progress} · "
                                    f"depth {t.get('depth', 0)} · "
                                    f"{(t.get('intent') or '')[:80]}")
                parts.append("\n".join(lines))
        except Exception:
            pass

    # Concierge journal tail (last 24h decisions)
    try:
        jpath = REPO_ROOT / "data" / "concierge" / "journal.jsonl"
        if jpath.exists():
            cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(hours=24)
            recent = []
            for line in jpath.read_text().strip().splitlines()[-50:]:
                try:
                    e = json.loads(line)
                    ets = dt.datetime.fromisoformat(
                        e["ts"].rstrip("Z"))
                    if ets.replace(tzinfo=dt.UTC) >= cutoff:
                        recent.append(e)
                except Exception:
                    continue
            if recent:
                lines = ["## Concierge journal (last 24h, last 8 entries)"]
                for e in recent[-8:]:
                    lines.append(f"- `{e.get('ts','?')[:19]}` "
                                    f"{e.get('event','?')} · "
                                    f"{(e.get('detail') or '')[:120]}")
                parts.append("\n".join(lines))
    except Exception:
        pass

    return "\n\n".join(parts)


def _load_concierge_identity() -> str:
    """Load data/concierge/identity.md verbatim. This is the SOUL of the
    concierge — prepended to SYSTEM_PROMPT on every turn."""
    p = REPO_ROOT / "data" / "concierge" / "identity.md"
    if p.exists():
        return p.read_text()
    return ""


def _load_relevant_playbooks(user_message: str = "") -> str:
    """Naive keyword match over data/concierge/playbooks/*.md. Picks at most
    one playbook to inject (the one whose filename matches keywords in the
    user's message), keeps under 4KB."""
    p = REPO_ROOT / "data" / "concierge" / "playbooks"
    if not p.exists():
        return ""
    msg_lower = (user_message or "").lower()
    candidates = list(p.glob("*.md"))
    if not candidates:
        return ""
    # Score: count of (filename word ∩ message words)
    def score(path):
        words = path.stem.replace("-", " ").split()
        return sum(1 for w in words if w in msg_lower)
    best = max(candidates, key=score)
    if score(best) == 0:
        return ""  # no playbook clearly applies
    body = best.read_text()
    if len(body) > 4000:
        body = body[:4000] + "\n\n[... truncated ...]"
    return f"## Active playbook: {best.name}\n\n{body}"


def _journal_decision(event: str, detail: dict | None = None) -> None:
    """Append a decision to data/concierge/journal.jsonl. Best-effort."""
    try:
        p = REPO_ROOT / "data" / "concierge" / "journal.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        entry = {"ts": dt.datetime.now(dt.UTC).isoformat(),
                  "event": event,
                  "detail": detail or {}}
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are Justin's Cold Email 2.0 *operator* — an expert in cold email, Smartlead mechanics, and THIS specific system. You're not a tool-router. You're the person who knows the playbook cold and runs it.

You are NOT a junior assistant who hedges and asks for confirmation on every step. You are the senior operator. Justin gives you intent ("personalize all the leads") and you decide the right path, execute it, and report results. You ask a clarifying question ONLY when there is genuine ambiguity that the system snapshot below cannot resolve.

# Domain expertise (non-negotiable baseline)

**Cold email**:
- Email 1's first 1-2 lines win or lose attention. Subject must read like one human emailing another, lowercase, ≤6 words, never sales-y. Body opens with a specific observation, not a pitch.
- Emails 2 and 3 thread (same subject as email 1). Day 3 and day 7 default delays.
- Never put links in emails 1-3. Links go in the *reply* after engagement.
- Validators (slop / sales / url / threading) are gates, not warnings — sequences that fail get re-rolled with violation feedback.

**Smartlead**:
- Sequence templates use `{{email_1_subject}}`, `{{email_1_body}}` (etc.) Mustache tokens. Each lead's `custom_fields` provides the substitution. If `custom_fields` is empty for a step, Smartlead ships the literal `{{...}}` string — broken.
- Campaign states: DRAFTED (editable, no sending) → ACTIVE/START (sending) → STOPPED → COMPLETED. `set-status START` flips DRAFTED → ACTIVE; the campaign's existing schedule (sending hours, daily cap, mailbox rotation) is honored.
- Idempotent campaign creation by name: `<niche>-<offer>-<VARIANT>`. Re-running launch_pilot with the same name appends leads, preserves the sequence template.
- CSV import path (Add Leads → Upload CSV) lets the operator import with custom-field mapping. Map `email_1_subject` etc. to custom fields the first time; mapping persists.
- Prospector search is FREE; fetch is what spends credits. Always confirm fetch counts.

**This system**:
- Six pre-created recruiter campaigns: `recruiters-{power-partner,direct-value,capstone}-{A,B}`. Each has a cached brief at `data/campaigns/<name>/brief.md`. Re-using a brief is FREE; regenerating costs ~$2.
- `data/voice_rules.md` is loaded ON TOP of every brief at copy-write time. Voice rules win on conflict.
- `data/emails/<email>.json` holds per-prospect 3-email sequences (the actual generated copy). `data/research/<email>.json` holds per-prospect signals.
- `data/exports/*.csv` is the deliverable Justin imports to Smartlead.
- The system snapshot below is auto-injected on EVERY turn. Read it before answering. If the snapshot has the answer, do NOT ask Justin.

# Important: how the templates work

When campaigns are pre-created, the Smartlead sequence template has placeholder
tokens like `{{email_1_subject}}` and `{{email_1_body}}`. These placeholders
look like a "real template" in the Smartlead UI but they are NOT — the actual
per-prospect copy lives in each lead's `custom_fields`, written by the Copy
squad during `launch_pilot`. Smartlead substitutes the placeholders at
send-time using each lead's custom fields.

So: an empty pre-created campaign with `{{email_1_body}}` showing in the
Sequences tab is correct and expected. You only see the real generated copy
after `launch_pilot` runs and after you call `preview_emails`.

# Your operational world

- **Campaigns** are named `<niche>-<offer>-<VARIANT>` (e.g. `recruiters-power-partner-A`). Always lowercase niche+offer, uppercase variant.
- **Offers available**: `power-partner` (recruiter-stack + AICO partner pitch), `direct-value` (AICO/Expert Series/Sovereign Blueprint direct), `capstone` (high-touch single rebuild).
- **Niches authored so far**: recruiters (full overlay), home-services (direct-value only).
- **Variants**: A and B exist for all 6 recruiter cells. C can be added with precreate_campaigns.
- **Pre-created and ready for leads**: `recruiters-power-partner-{A,B}`, `recruiters-direct-value-{A,B}`, `recruiters-capstone-{A,B}`.

# Decision priorities (in order)

1. **Look at the state snapshot first.** Justin's leads are in the source TenXVA campaigns listed there. The 6 cached briefs are listed there. Active lead handles are listed there. Recent CSV exports are listed there. *Do not ask Justin questions whose answers are in that snapshot.*

2. **Default to action, not interrogation.** When intent is clear, execute. Examples:
   - "Personalize all the leads" → pull the source TenXVA campaigns from the snapshot, dedupe to the unique set, run `personalize_to_csv` against the brief Justin named. If he didn't name a brief, default to `recruiters-power-partner-A` and tell him you defaulted (he can correct in one message).
   - "Use those leads" / "the leads" → the recruiter leads in the source TenXVA campaigns. Don't ask "which leads".
   - "Fan it across all 6 campaigns" → call `personalize_to_csv` six times in parallel, one per cached brief.
   - "Show me examples" → `generate_preview_pack` with the 6 brief names.

3. **`personalize_to_csv` is the default delivery path.** It runs Strategy + Research + Copy and drops a CSV file in the thread. Justin uploads to Smartlead himself. Lower failure surface than `launch_pilot`. Use `launch_pilot` ONLY if Justin explicitly asks you to push leads directly to Smartlead.

4. **Confirmation gate (still real, but tight)**: Before any tool that costs LLM tokens or writes to Smartlead, post a ONE-LINE plan: "Personalizing 894 unique recruiter leads against `recruiters-power-partner-A` brief. ~25 min. Confirm?" — then call after a yes. NEVER spend tokens or write to Smartlead without an affirmative reply in the most recent message.

# How to handle specific requests

1. **CSV upload from Slack**: when a user message has files attached, call `ingest_csv_from_slack` with the file_id. Then route to `personalize_to_csv` (default).
2. **Prospector pull (NL)**: call `pull_prospector_nl` first (FREE search). Show the count + sample. Ask user to confirm `max_fetch` value before calling `prospect_fetch_confirmed` (which spends credits).
3. **Prospector pull (saved)**: same flow with `pull_prospector_saved`.
4. **Use existing Smartlead leads**: when the user says "use the leads already in Smartlead" or "use those recruiter leads", call `load_leads_from_smartlead` with the source campaign ID. The recruiter list is in one of the existing Smartlead campaigns — call `list_active_campaigns` first if you don't know which campaign holds them.
5. **Launch pilot vs personalize-to-CSV (CRITICAL DEFAULT)**: there are TWO paths to personalized emails. PREFER `personalize_to_csv` UNLESS Justin explicitly asks you to push to Smartlead.
   - `personalize_to_csv` (PREFERRED, faster, more reliable): runs Strategy + Research + Copy, drops a CSV file into the Slack thread. Justin uploads to Smartlead himself. No Smartlead-write failure modes.
   - `launch_pilot` (legacy, more failure surface): same pipeline + writes leads with custom_fields directly to a pre-created Smartlead campaign.
   When Justin says "personalize these leads", "give me the CSV", "I'll upload myself", or simply "do it" / "go" without specifying — DEFAULT to `personalize_to_csv`. Confirm explicitly: "Personalizing N leads using `<niche>-<offer>-<VARIANT>` brief, will post CSV when done (~X min). Confirm?". Then call.
6. **Show samples (CRITICAL)**: After every `launch_pilot` completes, AUTOMATICALLY call `preview_emails` for each campaign that received leads, and post 3-5 examples in the thread. The user must always see real generated copy before any campaign moves to ACTIVE.
7. **Schedule (start sending)**: only after the user has reviewed previews and explicitly approved with words like "looks good, start it" or "send" or "schedule". Then call `schedule_campaign` with the campaign_id. The Smartlead UI's pre-configured schedule (sending hours, daily cap) is respected.
8. **Stats / list / read / preview**: call freely without confirmation — they're read-only or near-read-only.
9. **Show examples / preview copy**: when the user says "show me examples", "preview the copy", "what would the emails look like" → call `generate_preview_pack` with the source Smartlead campaign ID and the target campaign names. It runs Research + Copy WITHOUT writing to Smartlead, posts samples to this thread. Confirm the user wants to proceed before each `launch_pilot` after they've reviewed.
10. **Capability gaps**: when a request would require a tool you don't have, FIRST call `log_capability_gap` with what was asked + what tool would help. THEN tell the user what you can do as an alternative. This is how the system learns over time — every gap becomes a candidate for the next tool to build.
11. **Feedback on copy / voice / rules (CRITICAL — DO THE WORK)**: when the user gives feedback like "stop saying X", "the voice should be Y", "for campaign Z drop W" — DO NOT just acknowledge what you'll change. Actually call `update_voice_rules` (for cross-campaign feedback) and/or `update_brief` (for one-campaign feedback) RIGHT THEN. Then call `generate_preview_pack` to regenerate samples. Then post the new samples to the thread. The full sequence in ONE turn: (a) update voice rules / briefs, (b) confirm what was written, (c) re-run preview pack. Never end a turn after just summarizing the user's feedback — that wastes their time.

12. **Inference defaults (CRITICAL — STOP ASKING REDUNDANT QUESTIONS)**: Justin has ~1,500-2,000 recruiter leads already loaded into the existing Smartlead recruiter campaigns. When he says any of these without specifying a source:
    - "take the leads"
    - "split the leads across campaigns"
    - "use those leads"
    - "load some leads"
    - "fire them up"
    - "start sending"
    you must INFER the source — DO NOT ask "what's the lead source?" That's the bug Justin called out. The default is: leads are already inside the existing recruiter Smartlead campaigns. Your job is to:
    (a) Call `list_active_campaigns` to find which recruiter campaigns currently hold leads (the populated ones, not the empty pre-created ones).
    (b) Call `load_leads_from_smartlead` with the source campaign ID(s) to get a lead handle.
    (c) Filter to non-responders (those without a positive reply tag) where applicable.
    (d) Split the handle's leads evenly across the target campaigns the user named.
    (e) Confirm the plan in ONE concise message with counts ("Loading 1,847 non-responders from `<source>`, splitting 369-370 per campaign across A/B/C/D/E. Confirm to proceed?") and only call `launch_pilot` after the user says yes.

    Similarly: when Justin names campaigns by short forms like "Recruiters Power Partner A" → map to `recruiters-power-partner-A`. Never ask him to repeat himself in canonical form. If he says "all 5 campaigns" or "all 6 campaigns", treat the existing pre-created recruiter set as the default.

13. **Waterfall request (lead progression across campaigns)**: when Justin says "leads that don't respond should move to the next campaign" / "tag leads with each campaign they go through" — this is a multi-stage flow we don't have a single tool for yet. Do this:
    (a) Call `log_capability_gap` describing it (so the next tool to build is "waterfall_advance: pull non-responders from campaign A, add to campaign B, tag with `sent-A`").
    (b) Tell Justin the manual waterfall steps clearly: launch A now → wait 7-10 days → use `load_leads_from_smartlead` to pull non-responders from A → `launch_pilot` into B → repeat for C/D/E. Acknowledge that this is manual today and the auto-progression is on the next-tools list.
    (c) Do NOT block the immediate ask (launching A) on the waterfall question — execute today's launch first, document the followup second.

# Confirmation gate (CRITICAL)

NEVER call any of these without an explicit user "yes" / "go" / "confirm" / "do it" / "launch" in the most recent user message:
- `prospect_fetch_confirmed`  (consumes Smartlead credits)
- `launch_pilot`              (writes to Smartlead, costs LLM tokens)
- `personalize_to_csv`        (costs LLM tokens — research + copy on every lead)
- `schedule_campaign`         (starts real outbound email sending)
- `archive_campaign`          (destructive)
- `precreate_campaigns`       (writes to Smartlead, costs LLM tokens)

`schedule_campaign` has an EXTRA gate: never call it without first having shown the user `preview_emails` output for that campaign in the same thread. The user must see real copy and explicitly approve before the campaign goes ACTIVE.

If the user's request implies one of these but they haven't confirmed, ASK first. Show them what you're about to do (counts, costs, campaign name) and wait for confirmation in the next message.

# Style

Slack-mrkdwn formatted. Concise. Use code blocks for IDs and command-like things. No preamble like "Sure!" or "I'd be happy to". Be direct, action-oriented.

When background work finishes, post a short ✅ or ❌ in the same thread.
"""


# ---------------------------------------------------------------------------
# Anthropic tool-use loop
# ---------------------------------------------------------------------------

ANTHROPIC_MODEL = os.environ.get("CE2_SLACK_AGENT_MODEL", "claude-sonnet-4-5-20250929")


async def run_tools(client: anthropic.Anthropic, conversation: list[dict],
                     thread_ts: str | None) -> str:
    """Tool-use loop. Returns the final assistant text.

    A fresh state-snapshot is built and prepended to the system prompt on every
    iteration of the loop so the LLM sees the latest campaign list, lead
    handles, and exports. This is what kills the "agent asks dumb questions"
    pattern — it can't claim ignorance of state that's right in front of it.
    """
    # Latest user message text (for playbook keyword matching)
    last_user_text = ""
    for m in reversed(conversation):
        if m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, str):
                last_user_text = c
                break
            if isinstance(c, list):
                for b in c:
                    if isinstance(b, dict) and b.get("type") == "text":
                        last_user_text = b.get("text", "")
                        break
                if last_user_text:
                    break

    identity = _load_concierge_identity()
    playbook = _load_relevant_playbooks(last_user_text)

    while True:
        snapshot = _build_state_snapshot(thread_ts=thread_ts)
        # Order: identity (SOUL) → operational rules → playbook (if any) → live state
        system_parts = [identity, SYSTEM_PROMPT]
        if playbook:
            system_parts.append(playbook)
        system_parts.append(snapshot)
        system_full = "\n\n---\n\n".join(p for p in system_parts if p)
        msg = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=2048,
            system=system_full,
            tools=TOOLS,
            messages=conversation,
        )
        # Append assistant turn (with possible tool_use blocks) to the convo.
        # Convert content blocks to plain dicts so the thread file can be
        # JSON-serialized later (Anthropic SDK returns TextBlock/ToolUseBlock).
        serialized = []
        for b in msg.content:
            t = getattr(b, "type", None)
            if t == "text":
                serialized.append({"type": "text", "text": b.text})
            elif t == "tool_use":
                serialized.append({"type": "tool_use", "id": b.id,
                                     "name": b.name, "input": dict(b.input or {})})
            else:
                # Fallback: best-effort dict
                try:
                    serialized.append(b.model_dump())
                except Exception:
                    serialized.append({"type": t or "unknown", "raw": str(b)})
        conversation.append({"role": "assistant", "content": serialized})

        if msg.stop_reason != "tool_use":
            # Final answer; return text content
            text_parts = [b.text for b in msg.content if getattr(b, "type", "") == "text"]
            return "\n".join(text_parts).strip() or "(no response)"

        # Execute tool calls, append results — read from the LIVE msg.content
        # (which has SDK objects), not the serialized copy.
        tool_results = []
        for block in msg.content:
            if getattr(block, "type", "") != "tool_use":
                continue
            name = block.name
            args = dict(block.input or {})
            args["_thread_ts"] = thread_ts
            print(f"[slack_agent] tool: {name}({json.dumps({k: v for k, v in args.items() if k != '_thread_ts'})[:200]})", flush=True)
            try:
                fn = TOOL_FUNCS.get(name)
                if not fn:
                    result = {"error": f"unknown tool {name}"}
                else:
                    result = fn(args)
            except Exception as e:
                result = {"error": str(e), "trace": traceback.format_exc()[-800:]}
            tool_results.append({
                "type": "tool_result", "tool_use_id": block.id,
                "content": json.dumps(result, default=str)[:8000],
            })
        conversation.append({"role": "user", "content": tool_results})


# ---------------------------------------------------------------------------
# Polling loop
# ---------------------------------------------------------------------------

def _emit_error(slack: SlackClient, channel: str, thread_ts: str | None,
                  context: str, exc: Exception) -> None:
    """Post a user-visible :warning: message AND log a full traceback. The whole
    point: never let the agent fail silently again. If something breaks anywhere
    in the message-handling path, the user sees it in Slack within seconds."""
    short = f"{type(exc).__name__}: {exc}"
    tb = traceback.format_exc()
    msg = (f":warning: I hit an error in `{context}` and couldn't complete "
           f"that turn.\n```\n{short[:500]}\n```\n_Full trace in the daemon log._")
    print(f"[slack_agent] ERROR in {context}: {short}\n{tb}", flush=True)
    try:
        slack.post(channel, msg, thread_ts=thread_ts)
    except Exception as post_e:
        # Last-resort: at least the log has it.
        print(f"[slack_agent] failed to post error to Slack: {post_e}", flush=True)


async def handle_message(slack: SlackClient, anth: anthropic.Anthropic,
                          channel: str, msg: dict) -> None:
    text = msg.get("text", "")
    files = msg.get("files", [])
    user = msg.get("user", "?")
    ts = msg.get("ts")
    # Use the message ts as the conversation key — replies in thread share thread_ts
    thread_ts = msg.get("thread_ts") or ts
    print(f"[slack_agent] msg from {user} ts={ts} thread={thread_ts} files={len(files)} "
           f"text={text[:120]!r}", flush=True)

    # Register this thread as active so the poll loop also watches for reply
    # messages here (conversations.history doesn't return thread replies).
    _register_active_thread(thread_ts, ts)

    # Journal: every inbound message is a decision point
    _journal_decision("user_message", {
        "user": user, "thread_ts": thread_ts,
        "text_preview": (text or "")[:200],
        "files": len(files),
    })

    # Visual heartbeat: reaction on the user's message changes as we progress.
    # eyes → working, white_check_mark → done, x → error. All best-effort
    # (won't break the main flow if reactions:write scope is missing).
    slack.add_reaction(channel, ts, "eyes")
    final_reaction = "white_check_mark"  # mutated below if anything fails

    lock = THREAD_LOCKS.setdefault(thread_ts, asyncio.Lock())
    async with lock:
        conversation = _load_thread(thread_ts)

        # Build user content — text + file metadata if any
        user_content = text or "(no text)"
        if files:
            file_lines = []
            for f in files:
                file_lines.append(
                    f"[file uploaded] id={f.get('id')} name={f.get('name')} "
                    f"mimetype={f.get('mimetype')}"
                )
            user_content = user_content + "\n\n" + "\n".join(file_lines)
        conversation.append({"role": "user", "content": user_content})

        # The whole turn is wrapped: any crash → user sees a :warning: reply.
        reply_text: str | None = None
        try:
            reply_text = await run_tools(anth, conversation, thread_ts)
        except Exception as e:
            _emit_error(slack, channel, thread_ts, "run_tools (LLM/tool loop)", e)
            final_reaction = "x"
            slack.remove_reaction(channel, ts, "eyes")
            slack.add_reaction(channel, ts, final_reaction)
            return  # don't try to save partial state

        # Persist conversation. If serialization itself blows up, surface it.
        try:
            _save_thread(thread_ts, conversation)
        except Exception as e:
            _emit_error(slack, channel, thread_ts, "save_thread (state persist)", e)
            final_reaction = "x"
            # fall through — we still try to post the reply

        try:
            slack.post(channel, reply_text or "(no response)", thread_ts=thread_ts)
        except Exception as e:
            _emit_error(slack, channel, thread_ts, "slack.post (final reply)", e)
            final_reaction = "x"

        # Swap reactions: remove the working indicator, mark final state
        slack.remove_reaction(channel, ts, "eyes")
        slack.add_reaction(channel, ts, final_reaction)


async def poll_once(slack: SlackClient, anth: anthropic.Anthropic, channel: str) -> int:
    """Single poll cycle: top-level channel messages, THEN active-thread replies.

    Slack's conversations.history only returns parent messages — thread replies
    do NOT show up there. Without the second pass, the agent posts a reply in
    a thread and is then deaf to the user's responses in that same thread,
    even when @-mentioned. Fixed by tracking active threads in
    data/slack/active_threads.json and polling each one's replies.
    """
    cursor = _load_cursor()
    last = cursor.get(channel)
    msgs = slack.read_channel(channel, oldest=last)
    n = 0
    for m in msgs:
        ts = m.get("ts")
        if not ts:
            continue
        # advance cursor as we go so a crash mid-batch doesn't re-process all
        cursor[channel] = ts
        _save_cursor(cursor)
        try:
            await handle_message(slack, anth, channel, m)
        except Exception as e:
            _emit_error(slack, channel, m.get("thread_ts") or ts,
                          "poll_once (outer)", e)
        n += 1

    # Pass 2: poll replies in every active thread.
    active = _prune_active_threads()
    for thread_ts, meta in list(active.items()):
        last_reply = meta.get("last_reply_ts", thread_ts)
        try:
            replies = slack.read_thread(channel, thread_ts, oldest=last_reply)
        except Exception as e:
            print(f"[slack_agent] read_thread({thread_ts}) failed: {e}", flush=True)
            continue
        for r in replies:
            r_ts = r.get("ts")
            if not r_ts:
                continue
            # Inject thread_ts so handle_message threads under the same parent
            r["thread_ts"] = thread_ts
            try:
                await handle_message(slack, anth, channel, r)
            except Exception as e:
                _emit_error(slack, channel, thread_ts,
                              "poll_once (thread reply)", e)
            # advance the per-thread cursor as we go
            active[thread_ts]["last_reply_ts"] = r_ts
            active[thread_ts]["updated_at"] = dt.datetime.utcnow().isoformat() + "Z"
            _save_active_threads(active)
            n += 1
    return n


async def heartbeat_loop(slack: SlackClient, channel: str,
                            interval_seconds: int = 1800) -> None:
    """Post a quiet status line to the channel every N minutes so the user
    can see at a glance that the daemon is alive. Defaults to 30 min.
    Crashes inside this coroutine are logged but never propagate (the
    heartbeat must never take down the agent itself)."""
    started_at = dt.datetime.utcnow()
    counter = 0
    while True:
        await asyncio.sleep(interval_seconds)
        counter += 1
        try:
            up = dt.datetime.utcnow() - started_at
            hrs = int(up.total_seconds() // 3600)
            mins = int((up.total_seconds() % 3600) // 60)
            uptime = f"{hrs}h{mins:02d}m"
            cursor_data = _load_cursor()
            cursor_ts = cursor_data.get(channel, "")
            slack.post(channel,
                f":heartbeat: alive · uptime *{uptime}* · "
                f"cursor `{cursor_ts[:13]}` · pulse #{counter}")
        except Exception as e:
            print(f"[heartbeat] error (suppressed): {e}", flush=True)


async def watch(channel: str, interval: int, once: bool, announce: bool = True) -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    THREADS_DIR.mkdir(parents=True, exist_ok=True)
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

    slack = SlackClient()
    anth = anthropic.Anthropic()
    me = slack.auth_test()
    print(f"[slack_agent] online as bot user_id={me.get('user_id')} "
           f"channel={channel} interval={interval}s once={once}", flush=True)

    if announce and not once:
        try:
            slack.post(channel,
                f":robot_face: orchestrator online — DM me a CSV, ask for Prospector leads, "
                f"or say `stats` for a snapshot. Pre-created campaigns: "
                f"`recruiters-power-partner-{{A,B}}`, `recruiters-direct-value-{{A,B}}`, "
                f"`recruiters-capstone-{{A,B}}`.")
        except Exception as e:
            print(f"[slack_agent] failed to announce: {e}", flush=True)

    # On first boot with no cursor, set cursor to now to avoid re-processing
    # the entire channel history.
    cursor = _load_cursor()
    if channel not in cursor:
        # Slack ts is unix-seconds.microseconds string
        cursor[channel] = f"{dt.datetime.utcnow().timestamp():.6f}"
        _save_cursor(cursor)

    # Background heartbeat — posts a status line every 30 min.
    if not once:
        hb_interval = int(os.environ.get("CE2_HEARTBEAT_SECONDS", "1800"))
        if hb_interval > 0:
            asyncio.create_task(heartbeat_loop(slack, channel, hb_interval))

    while True:
        try:
            n = await poll_once(slack, anth, channel)
            if n:
                print(f"[slack_agent] handled {n} message(s)", flush=True)
        except Exception as e:
            print(f"[slack_agent] poll error: {e}", flush=True)
        if once:
            return 0
        await asyncio.sleep(interval)


def cli_main() -> int:
    p = argparse.ArgumentParser(prog="cold-email-agent",
                                  description="Slack-driven orchestrator daemon")
    p.add_argument("--channel",
                    default=os.environ.get("SLACK_CONTROL_CHANNEL", "#cold-email-control"))
    p.add_argument("--interval", type=int, default=8,
                    help="poll interval seconds (default 8)")
    p.add_argument("--once", action="store_true",
                    help="single poll then exit (testing)")
    p.add_argument("--no-announce", action="store_true",
                    help="skip the boot announcement message")
    args = p.parse_args()
    return asyncio.run(watch(args.channel, args.interval, args.once,
                                announce=not args.no_announce))


if __name__ == "__main__":
    sys.exit(cli_main())
