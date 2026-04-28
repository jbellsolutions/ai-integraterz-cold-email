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
THREADS_DIR = DATA_DIR / "threads"
UPLOADS_DIR = DATA_DIR / "uploads"

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
    """Spawn run_pilot as a background task. Returns immediately with task id."""
    from orchestrator.main import run_pilot
    handle = args["lead_handle"]
    niche = args.get("niche")
    offer = args["offer"]
    variant = args.get("variant", "A")
    if handle not in LEAD_HANDLES:
        return {"error": f"unknown lead_handle '{handle}' — call ingest or fetch first"}
    leads = LEAD_HANDLES[handle]["leads"]
    source = LEAD_HANDLES[handle]["source"]

    # Spawn background task; deliver result via Slack from within the task.
    sc = SlackClient()
    channel = os.environ.get("SLACK_CONTROL_CHANNEL", "#cold-email-control")
    thread_ts = args.get("_thread_ts")  # injected by run_tools

    async def _runner():
        try:
            await run_pilot(offer, leads, niche=niche, variant=variant,
                              source_label=source, dry_run=False)
            sc.post(channel, f":white_check_mark: pilot complete — "
                              f"`{niche}-{offer}-{variant.upper()}` "
                              f"({len(leads)} leads from {source})",
                     thread_ts=thread_ts)
        except Exception as e:
            sc.post(channel, f":x: pilot failed for "
                              f"`{niche}-{offer}-{variant.upper()}`: {e}",
                     thread_ts=thread_ts)

    task_id = _new_handle("pilot")
    RUNNING_PILOTS[task_id] = asyncio.create_task(_runner())
    return {"started": True, "task_id": task_id, "leads": len(leads),
            "campaign": f"{niche}-{offer}-{variant.upper()}",
            "note": "Running in background. I will message this thread when done (~2-5 min)."}


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
    {"name": "schedule_campaign",
     "description": "Flip a Smartlead campaign from DRAFTED to ACTIVE (start sending). Smartlead's existing schedule (hours, daily cap) on the campaign is respected — this just flips the on switch. DESTRUCTIVE — only call after explicit user confirm AND only after preview_emails has been shown.",
     "input_schema": {"type": "object", "required": ["campaign_id"],
                       "properties": {"campaign_id": {"type": ["string", "integer"]}}}},
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
    "schedule_campaign": tool_schedule_campaign,
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

        try:
            reply_text = await run_tools(anth, conversation, thread_ts)
        except Exception as e:
            reply_text = f":warning: orchestrator error: {e}"
            print(f"[slack_agent] error: {e}\n{traceback.format_exc()}", flush=True)

        _save_thread(thread_ts, conversation)
        try:
            slack.post(channel, reply_text, thread_ts=thread_ts)
        except Exception as e:
            print(f"[slack_agent] failed to post reply: {e}", flush=True)


async def poll_once(slack: SlackClient, anth: anthropic.Anthropic, channel: str) -> int:
    cursor = _load_cursor()
    last = cursor.get(channel)
    msgs = slack.read_channel(channel, oldest=last)
    if not msgs:
        return 0
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
            print(f"[slack_agent] handle error: {e}", flush=True)
        n += 1
    return n


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
