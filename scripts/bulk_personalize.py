"""Bulk-personalize: pull leads from Smartlead source campaigns, run
Strategy + Research + Copy with a chosen brief, write CSV, post to Slack
(if files:write scope present, else post path + preview).

Usage:
  python scripts/bulk_personalize.py \
      --sources 3103961,3103959,3085916,3085917,3080680 \
      --brief recruiters-power-partner-A \
      [--max 200] [--no-slack]

Designed for safety after the 2026-04-28 launch_pilot reliability incident:
  - Same hard-fail-on-empty guard as run_pilot
  - Per-prospect copy errors don't kill the batch (logged + skipped)
  - Concurrency env-tunable (CE2_RESEARCH_PARALLEL, CE2_COPY_PARALLEL)
  - CSV is the deliverable; Justin imports to Smartlead himself.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import datetime as dt
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from squads.copy import CopySquad
from squads.research import ResearchSquad
from squads.research.squad import Lead
from tools.smartlead import SmartleadCLI


def pull_leads(source_ids: list[int]) -> list[Lead]:
    cli = SmartleadCLI()
    by_email: dict[str, Lead] = {}
    for cid in source_ids:
        print(f"[bulk] pulling {cid}...", flush=True)
        try:
            raw = cli.list_campaign_leads(cid)
        except Exception as e:
            print(f"[bulk]   failed: {e}", flush=True)
            continue
        for r in raw:
            r = r.get("lead", r) if isinstance(r, dict) else r
            email = (r.get("email") or "").lower().strip()
            if not email or email in by_email:
                continue
            cf = r.get("custom_fields") or {}
            full = " ".join([r.get("first_name") or "",
                              r.get("last_name") or ""]).strip()
            by_email[email] = Lead(
                lead_id=email,
                email=email,
                name=full or email.split("@", 1)[0].title(),
                company=r.get("company_name") or "",
                title=(r.get("title") or r.get("job_title")
                        or cf.get("job_title") or cf.get("title") or ""),
                linkedin_url=r.get("linkedin_url") or "",
            )
        print(f"[bulk]   total unique leads: {len(by_email)}", flush=True)
    return list(by_email.values())


async def run(args) -> int:
    source_ids = [int(s) for s in args.sources.split(",")]
    leads = pull_leads(source_ids)
    if args.max:
        leads = leads[: args.max]
    n = len(leads)
    print(f"[bulk] {n} leads after dedup/limit", flush=True)
    if n == 0:
        print("[bulk] no leads to personalize, exiting", flush=True)
        return 0

    brief_path = REPO_ROOT / "data" / "campaigns" / args.brief / "brief.md"
    if not brief_path.exists():
        print(f"[bulk] no cached brief at {brief_path}", flush=True)
        return 1
    brief = brief_path.read_text()
    print(f"[bulk] brief loaded: {brief_path} ({len(brief)} chars)", flush=True)

    # Research
    rp = int(os.environ.get("CE2_RESEARCH_PARALLEL", "30"))
    print(f"[bulk] research × {n} leads, max_parallel={rp}", flush=True)
    research = ResearchSquad(brief=brief)
    rt0 = dt.datetime.now(dt.UTC)
    signals = await research.research_batch(leads, max_parallel=rp)
    rdt = (dt.datetime.now(dt.UTC) - rt0).total_seconds()
    print(f"[bulk] research done in {rdt:.0f}s "
           f"({rdt/max(n,1):.2f}s/lead)", flush=True)

    # Copy
    cp = int(os.environ.get("CE2_COPY_PARALLEL", "20"))
    print(f"[bulk] copy × {n} leads, max_parallel={cp}", flush=True)
    copy = CopySquad(brief=brief)
    sem = asyncio.Semaphore(cp)
    progress = {"n": 0}
    last_print = {"t": dt.datetime.now(dt.UTC)}

    async def write_one(lead, signal):
        async with sem:
            try:
                return await copy.write_one(lead, signal)
            except Exception as e:
                print(f"[bulk] copy failed for {lead.email}: {e}", flush=True)
                return {"sequence": [], "slop_pass": False, "error": str(e)}
            finally:
                progress["n"] += 1
                now = dt.datetime.now(dt.UTC)
                if (progress["n"] % 50 == 0
                        or (now - last_print["t"]).total_seconds() > 60):
                    pct = 100 * progress["n"] / max(n, 1)
                    elapsed = (now - rt0).total_seconds()
                    print(f"[bulk]   copy {progress['n']}/{n} ({pct:.0f}%) "
                           f"· elapsed {elapsed:.0f}s", flush=True)
                    last_print["t"] = now

    ct0 = dt.datetime.now(dt.UTC)
    emails = await asyncio.gather(*(write_one(l, s)
                                       for l, s in zip(leads, signals)))
    cdt = (dt.datetime.now(dt.UTC) - ct0).total_seconds()
    slop = sum(1 for e in emails if e.get("slop_pass"))
    print(f"[bulk] copy done in {cdt:.0f}s · {slop}/{n} slop-clean", flush=True)

    # Write CSV (drop empty rows)
    cols = [
        "email", "first_name", "last_name", "company_name", "title",
        "linkedin_url",
        "email_1_subject", "email_1_body",
        "email_2_subject", "email_2_body",
        "email_3_subject", "email_3_body",
        "slop_pass", "signal_tier",
    ]
    ts = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%S")
    out = REPO_ROOT / "data" / "exports" / f"bulk-{args.brief}-{ts}.csv"
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
    total_dt = (dt.datetime.now(dt.UTC) - rt0).total_seconds()
    print(f"[bulk] wrote {written} rows ({skipped} skipped empty) → {out}",
            flush=True)
    print(f"[bulk] file size: {out.stat().st_size:,} bytes", flush=True)
    print(f"[bulk] total wall-time: {total_dt:.0f}s", flush=True)

    # Slack notification
    if not args.no_slack:
        try:
            from tools.slack_notify import SlackClient
            sc = SlackClient()
            channel = os.environ.get("SLACK_CONTROL_CHANNEL", "#cold-email-control")
            # Try real upload first; fall back to post if no scope
            comment = (
                f":white_check_mark: *Bulk personalization done* — `{args.brief}`\n"
                f"  • {written} rows ({skipped} dropped empty / {n} attempted)\n"
                f"  • Slop-clean: {slop}/{n}\n"
                f"  • Research {rdt:.0f}s · Copy {cdt:.0f}s · "
                f"*total {total_dt:.0f}s*\n"
                f"  • File: `{out}`"
            )
            try:
                sc.upload_file(channel, out, title=out.name,
                                  initial_comment=comment)
                print(f"[bulk] uploaded to Slack {channel}", flush=True)
            except Exception as e:
                if "missing_scope" in str(e):
                    sc.post(channel,
                              f"{comment}\n\n_(Slack file upload blocked — "
                              f"`files:write` scope missing on bot. CSV is on disk; "
                              f"open it from Finder.)_")
                    print(f"[bulk] posted path to Slack (no upload scope)",
                            flush=True)
                else:
                    raise
        except Exception as e:
            print(f"[bulk] slack notify failed: {e}", flush=True)
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--sources", required=True,
                    help="comma-separated Smartlead campaign IDs to pull from")
    p.add_argument("--brief", required=True,
                    help="campaign name with cached brief, e.g. recruiters-power-partner-A")
    p.add_argument("--max", type=int, default=0,
                    help="max leads to process (0 = no limit)")
    p.add_argument("--no-slack", action="store_true")
    args = p.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
