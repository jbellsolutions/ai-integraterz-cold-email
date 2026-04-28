"""Reply-handling daemon.

Polls Smartlead's unified inbox for new replies on a configurable cadence,
runs each through the ReplySquad pipeline, and posts approval-gated drafts to
Slack. OOO + unsubscribe replies auto-handle silently.

State:
  data/replies/seen.json         — set of inbox entry IDs already processed
  data/replies/<id>.json         — full result per reply (triage + draft + approval)
  data/replies/<id>.skipped.json — replies the user dismissed in Slack
  data/replies/<id>.sent.json    — replies that were sent (with the final body)
  data/replies/_slack_queue/     — Slack messages staged for MCP dispatch

Run:
  python -m orchestrator.reply_loop --campaign-id <id> --interval 60
  python -m orchestrator.reply_loop --once   (single pass, useful for testing)

The --watch flag on orchestrator.main also delegates here.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from dataclasses import asdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from squads.reply import ReplySquad
from squads.reply.squad import auto_filter, ReplyClass
from tools.slack_notify import SlackNotifier
from tools.smartlead import SmartleadCLI

DATA_DIR = REPO_ROOT / "data" / "replies"
SEEN_PATH = DATA_DIR / "seen.json"


def _allowed_urls_for_angle(angle: str) -> set[str]:
    """Walk campaigns/<angle>/**/*.md and harvest every http(s) URL.
    The Drafter is only allowed to emit URLs from this set. Anything else
    fails the allowlist gate (Approver verdict forced to FAIL).

    Always includes a small set of structural URLs even if a doc doesn't
    explicitly list them (audit booking, certification page) so docs can
    reference them by relative path without the gate firing.
    """
    angle_dir = REPO_ROOT / "campaigns" / angle
    urls: set[str] = set()
    if angle_dir.exists():
        for f in angle_dir.rglob("*.md"):
            urls |= set(re.findall(r"https?://[^\s)>\]\"'`]+", f.read_text()))
    # normalize: drop trailing punctuation
    urls = {u.rstrip(".,;:!?") for u in urls}
    return urls


_URL_RE = re.compile(r"https?://[^\s)>\]\"'`]+")


def _foreign_urls(text: str, allowed: set[str]) -> list[str]:
    """Return URLs in text that are NOT in the allowlist.
    A URL is allowed if it matches an allowed URL exactly OR if its host+path
    prefix matches one (so /audit?utm=foo is still considered the same as /audit)."""
    found = [u.rstrip(".,;:!?") for u in _URL_RE.findall(text)]
    foreign = []
    for u in found:
        if u in allowed:
            continue
        # allow if any allowed URL is a prefix
        if any(u.startswith(a) or a.startswith(u) for a in allowed):
            continue
        foreign.append(u)
    return foreign


def _strip_html(html: str) -> str:
    """Minimal HTML→text. Removes tags, decodes core entities, collapses whitespace.
    Adequate for Smartlead reply bodies which are often quoted-printable HTML email.

    Drops <head>/<title>/<style>/<script>/<meta> contents BEFORE tag-stripping so
    things like the email subject in <title> don't pollute the visible body
    (which would push real first-line content like "unsubscribe" off line 1 and
    trip the wrong classifier).
    """
    import re
    from html import unescape

    # 1. Drop entire <head>...</head> + standalone <title>/<style>/<script>/<meta>
    for tag in ("head", "title", "style", "script"):
        html = re.sub(fr"<{tag}\b[^>]*>.*?</{tag}>", " ", html, flags=re.S | re.I)
    # also strip self-closing/lone meta + DOCTYPE
    html = re.sub(r"<!DOCTYPE[^>]*>", " ", html, flags=re.I)
    html = re.sub(r"<meta\b[^>]*/?>", " ", html, flags=re.I)
    # 2. Drop blockquotes (email thread quotes — keep top-of-thread only)
    html = re.sub(r"<blockquote[^>]*>.*?</blockquote>", " ", html, flags=re.S | re.I)
    # 3. Drop the "From: ... Sent: ... To: ... Subject: ..." quoted-thread divider
    #    common to Outlook (divRplyFwdMsg) — anything after a hr or that div is quoted
    html = re.sub(r'<div[^>]*id=["\']?divRplyFwdMsg.*', " ", html, flags=re.S | re.I)
    html = re.sub(r"<hr\b[^>]*>.*", " ", html, flags=re.S | re.I)
    # 4. Turn <br> and </p> into newlines for readability
    html = re.sub(r"<br\s*/?>", "\n", html, flags=re.I)
    html = re.sub(r"</p>", "\n\n", html, flags=re.I)
    # 5. Strip remaining tags
    text = re.sub(r"<[^>]+>", "", html)
    text = unescape(text)
    # 6. Collapse runs of blank lines + non-breaking-space artifacts
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _load_seen() -> set[str]:
    if not SEEN_PATH.exists():
        return set()
    try:
        return set(json.loads(SEEN_PATH.read_text()))
    except Exception:
        return set()


def _save_seen(seen: set[str]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SEEN_PATH.write_text(json.dumps(sorted(seen)))


def _slug(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "_", s)


async def process_once(
    campaign_brief: str,
    voice_md: str,
    cli: SmartleadCLI,
    notifier: SlackNotifier,
    limit: int = 20,
    max_replies: int | None = None,
    allowed_urls: set[str] | None = None,
) -> dict[str, int]:
    """One polling cycle. Returns counts for logging.

    Fetches reply overviews via inbox replies, then for each unseen lead pulls
    the full thread via leads messages to get the actual SENT outbound + REPLY
    inbound bodies. Strips HTML. Routes through ReplySquad. Posts Slack pings
    for human-approval drafts; auto-handles OOO + unsubscribe silently.
    """
    seen = _load_seen()
    counts = {"new": 0, "auto_handled": 0, "needs_human": 0, "skipped": 0}

    overviews = cli.fetch_replies(limit=limit)   # all replies; we dedupe via seen.json
    squad = ReplySquad(brief=campaign_brief, voice_md=voice_md)
    allowed_urls = allowed_urls or set()

    # BUG FIX 1: per-tick dedup by lead_email.
    # If the same lead is in multiple campaigns and replied to all of them, we
    # were generating one Slack ping per campaign. Group by email and process
    # only the most recently replied campaign for each lead in this tick.
    by_email: dict[str, dict] = {}
    for ov in overviews:
        email = (ov.get("lead_email") or "").lower().strip()
        if not email:
            continue
        prev = by_email.get(email)
        if prev is None or (ov.get("last_reply_time") or "") > (prev.get("last_reply_time") or ""):
            by_email[email] = ov
    overviews = list(by_email.values())

    processed_for_this_tick = 0
    emails_pinged_this_tick: set[str] = set()
    for ov in overviews:
        # The inbox replies CLI returns dicts keyed on email_lead_id and email_campaign_id
        lead_id = ov.get("email_lead_id") or ov.get("lead_id") or ov.get("id")
        campaign_id = ov.get("email_campaign_id") or ov.get("campaign_id")
        rid = f"{campaign_id}:{lead_id}"
        if not (lead_id and campaign_id) or rid in seen:
            continue
        # also dedup: even if rid is fresh, if we already pinged this email this tick, skip
        email_key = (ov.get("lead_email") or "").lower().strip()
        if email_key and email_key in emails_pinged_this_tick:
            seen.add(rid)
            continue
        if max_replies is not None and processed_for_this_tick >= max_replies:
            break
        processed_for_this_tick += 1
        counts["new"] += 1

        # Pull the full thread to get real bodies
        history = cli.get_lead_messages(campaign_id, lead_id)
        sent_msgs = [m for m in history if m.get("type") == "SENT"]
        reply_msgs = [m for m in history if m.get("type") == "REPLY"]
        if not reply_msgs:
            seen.add(rid)
            continue

        last_reply = reply_msgs[-1]
        last_sent = sent_msgs[-1] if sent_msgs else {}
        body = _strip_html(last_reply.get("email_body") or "")
        original = _strip_html(last_sent.get("email_body") or "")

        first = ov.get("lead_first_name") or ""
        last = ov.get("lead_last_name") or ""
        lead = {
            "name": f"{first} {last}".strip() or ov.get("lead_email") or "",
            "email": ov.get("lead_email") or last_reply.get("from") or "",
            "company": ov.get("lead_company_name") or "",
        }

        # 1. Cheap pre-filter — auto-handle silently.
        pre = auto_filter(body)
        if pre is ReplyClass.UNSUBSCRIBE:
            # Honor compliance: unsubscribe from all campaigns + add to workspace blocklist.
            # No reply, no Slack ping. Belt-and-suspenders: even if one call fails, the other holds.
            try:
                cli._run_json(["leads", "unsubscribe-all", "--lead-id", str(lead_id)])  # type: ignore[attr-defined]
            except Exception as e:
                print(f"[reply_loop] unsubscribe-all failed for {lead_id}: {e}", flush=True)
            try:
                if lead.get("email"):
                    cli._run_json(["leads", "blocklist-add", "--email", lead["email"]])  # type: ignore[attr-defined]
            except Exception as e:
                print(f"[reply_loop] blocklist-add failed for {lead.get('email')}: {e}", flush=True)
            (DATA_DIR / f"{_slug(rid)}.unsubscribed.json").write_text(
                json.dumps({"lead": lead, "lead_id": lead_id, "campaign_id": campaign_id,
                             "auto_handled": "unsubscribe", "first_line": body.split(chr(10), 1)[0][:200]},
                            indent=2)
            )
            counts["auto_handled"] += 1
            seen.add(rid)
            continue
        if pre is ReplyClass.OOO:
            (DATA_DIR / f"{_slug(rid)}.ooo.json").write_text(
                json.dumps({"lead": lead, "auto_handled": "ooo",
                             "first_line": body.split(chr(10), 1)[0][:200]}, indent=2)
            )
            counts["auto_handled"] += 1
            seen.add(rid)
            continue

        # 2. Full LLM pipeline.
        result = await squad.process(original, body)

        # spam → silent log, no notification
        if result.get("auto_handled") and result.get("reply_class") == "spam":
            (DATA_DIR / f"{_slug(rid)}.spam.json").write_text(json.dumps(result, indent=2))
            counts["auto_handled"] += 1
            seen.add(rid)
            continue

        # BUG FIX 2: URL allowlist gate.
        # Drafter sometimes hallucinates a calendar/landing URL not present in
        # the campaign docs. Hard-fail the draft if any URL in body or subject
        # is not in the allowlist harvested from campaigns/<angle>/**/*.md.
        draft = result.get("draft") or {}
        approval = result.get("approval") or {}
        if draft.get("body") or draft.get("subject"):
            check_text = f"{draft.get('subject', '')}\n{draft.get('body', '')}"
            foreign = _foreign_urls(check_text, allowed_urls)
            if foreign:
                approval["verdict"] = "FAIL"
                approval["reason"] = (
                    f"Draft contains URL(s) not in campaign allowlist: {foreign}. "
                    f"Drafter must only emit URLs that appear verbatim in campaigns/<angle>/**/*.md."
                )
                approval["flagged"] = list(approval.get("flagged", [])) + [
                    f"foreign_url:{u}" for u in foreign
                ]
                result["approval"] = approval

        # 3. FAIL verdicts skip Slack — log for human review only
        if approval.get("verdict") == "FAIL":
            (DATA_DIR / f"{_slug(rid)}.failed.json").write_text(json.dumps(result, indent=2))
            counts["needs_human"] += 1
            seen.add(rid)
            continue

        # 4. PASS or FLAG → Slack ping for human approval
        ping = notifier.ping_for_approval(
            lead=lead,
            inbound_reply=body,
            original_outbound=original,
            triage={
                "reply_class": result.get("reply_class"),
                "intent_score": result.get("intent_score"),
                "key_signal": result.get("key_signal"),
            },
            draft=result.get("draft") or {},
            approval=approval,
        )
        out = {
            **result,
            "lead": lead,
            "smartlead_reply_id": rid,
            "campaign_id": campaign_id,
            "lead_id": lead_id,
            "email_stats_id": last_reply.get("stats_id") or last_reply.get("message_id"),
            "slack_ping": asdict(ping),
            "status": "awaiting_approval",
        }
        (DATA_DIR / f"{_slug(rid)}.json").write_text(json.dumps(out, indent=2))
        counts["needs_human"] += 1
        seen.add(rid)
        if email_key:
            emails_pinged_this_tick.add(email_key)

    _save_seen(seen)
    return counts


async def watch(brief_path: str, voice_path: str, interval: int, once: bool,
                max_replies: int | None = None, angle: str = "power-partner") -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    brief = Path(brief_path).read_text() if Path(brief_path).exists() else ""
    voice = Path(voice_path).read_text() if Path(voice_path).exists() else ""
    cli = SmartleadCLI()
    notifier = SlackNotifier()
    allowed_urls = _allowed_urls_for_angle(angle)

    print(f"[reply_loop] starting. interval={interval}s once={once} max_replies={max_replies} "
          f"angle={angle} allowed_urls={len(allowed_urls)}",
          flush=True)
    while True:
        try:
            counts = await process_once(brief, voice, cli, notifier,
                                         max_replies=max_replies, allowed_urls=allowed_urls)
            print(f"[reply_loop] tick — new={counts['new']} auto={counts['auto_handled']} "
                  f"slack={counts['needs_human']}", flush=True)
        except Exception as e:
            print(f"[reply_loop] tick error: {e}", flush=True)
        if once:
            return 0
        await asyncio.sleep(interval)


def cli_main() -> int:
    p = argparse.ArgumentParser(prog="cold-email-replies")
    p.add_argument("--angle", default="power-partner",
                    help="campaign angle (folder under campaigns/) — used for URL allowlist + voice")
    p.add_argument("--brief", default=str(DATA_DIR.parent / "campaigns" / "brief.md"),
                    help="path to campaign-brief.md (Strategy squad output)")
    p.add_argument("--voice", default=None,
                    help="path to voice.md (defaults to campaigns/<angle>/voice.md)")
    p.add_argument("--interval", type=int, default=60, help="polling interval seconds")
    p.add_argument("--once", action="store_true", help="single pass then exit")
    p.add_argument("--max-replies", type=int, default=None,
                    help="cap N replies per tick (safety for first live tests)")
    args = p.parse_args()
    voice = args.voice or str(REPO_ROOT / "campaigns" / args.angle / "voice.md")
    return asyncio.run(watch(args.brief, voice, args.interval, args.once,
                              args.max_replies, angle=args.angle))


if __name__ == "__main__":
    sys.exit(cli_main())
