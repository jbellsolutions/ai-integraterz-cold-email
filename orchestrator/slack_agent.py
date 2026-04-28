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
    "load_leads_from_smartlead": tool_load_leads_from_smartlead,
    "preview_emails": tool_preview_emails,
    "generate_preview_pack": tool_generate_preview_pack,
    "schedule_campaign": tool_schedule_campaign,
    "update_voice_rules": tool_update_voice_rules,
    "update_brief": tool_update_brief,
    "log_capability_gap": tool_log_capability_gap,
    "archive_campaign": tool_archive_campaign,
    "precreate_campaigns": tool_precreate_campaigns,
}


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are Justin's Cold Email 2.0 orchestrator, talking to him in a Slack channel.

You operate his cold-email machine. He DMs/messages you natural-language requests; you translate them into tool calls.

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

# How to handle requests

1. **CSV upload**: when a user message has files attached, call `ingest_csv_from_slack` with the file_id. Then ask which campaign(s) to load them into and confirm before `launch_pilot`.
2. **Prospector pull (NL)**: call `pull_prospector_nl` first (FREE search). Show the count + sample. Ask user to confirm `max_fetch` value before calling `prospect_fetch_confirmed` (which spends credits).
3. **Prospector pull (saved)**: same flow with `pull_prospector_saved`.
4. **Use existing Smartlead leads**: when the user says "use the leads already in Smartlead" or "use those recruiter leads", call `load_leads_from_smartlead` with the source campaign ID. The recruiter list is in one of the existing Smartlead campaigns — call `list_active_campaigns` first if you don't know which campaign holds them.
5. **Launch pilot**: after a lead handle exists and the user has named the (niche, offer, variant), confirm explicitly: "Launching N leads into <name>. Confirm?". Then call `launch_pilot`.
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
    """Tool-use loop. Returns the final assistant text."""
    while True:
        msg = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=2048,
            system=SYSTEM_PROMPT,
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
