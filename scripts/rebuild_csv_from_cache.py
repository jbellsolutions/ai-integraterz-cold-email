"""Rebuild a personalized-leads CSV from on-disk cache.

Use case: 2026-04-28 — Justin had ~175 real personalized 3-email sequences
sitting in data/emails/*.json from earlier launch_pilot runs. The Slack
agent then ran archive_campaign(DELETE) + precreate_campaigns on the
Smartlead side, nuking the campaign records but leaving the disk cache
intact. This script joins the cache against the source Smartlead campaigns
(by email) to recover lead metadata and writes a CSV.

Output: data/exports/recovered-<timestamp>.csv

Then optionally uploads to Slack #cold-email-control.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from tools.smartlead import SmartleadCLI


def populated_emails() -> dict[str, dict]:
    """Return {email_lower: email_dict_with_sequence} for every cached file
    that has a real step-1 subject + body."""
    out = {}
    for f in (REPO_ROOT / "data" / "emails").glob("*.json"):
        try:
            d = json.loads(f.read_text())
        except Exception:
            continue
        seq = d.get("sequence") or []
        step1 = next((s for s in seq if s.get("step") == 1), None)
        if not step1:
            continue
        if not (step1.get("body") or "").strip():
            continue
        if not (step1.get("subject") or "").strip():
            continue
        email = (d.get("lead_id") or "").lower().strip()
        if not email:
            continue
        out[email] = d
    return out


def lead_metadata_index(campaign_ids: list[int]) -> dict[str, dict]:
    """Pull leads from each Smartlead campaign and index by email — gives us
    name / company / title for every lead we have copy for."""
    cli = SmartleadCLI()
    idx: dict[str, dict] = {}
    for cid in campaign_ids:
        print(f"[rebuild] pulling leads from campaign {cid}...", flush=True)
        try:
            leads = cli.list_campaign_leads(cid)
        except Exception as e:
            print(f"[rebuild]   failed: {e}", flush=True)
            continue
        for lead in leads:
            # Smartlead returns wrapped {lead: {...}, ...} on some endpoints
            lead = lead.get("lead", lead) if isinstance(lead, dict) else lead
            email = (lead.get("email") or "").lower().strip()
            if not email or email in idx:
                continue
            cf = lead.get("custom_fields") or {}
            idx[email] = {
                "first_name": lead.get("first_name") or "",
                "last_name": lead.get("last_name") or "",
                "company_name": lead.get("company_name") or "",
                "title": lead.get("title") or lead.get("job_title")
                          or cf.get("job_title") or cf.get("title") or "",
                "linkedin_url": lead.get("linkedin_url") or "",
                "phone": lead.get("phone_number") or "",
                "source_campaign": cid,
            }
        print(f"[rebuild]   indexed {len(idx)} unique emails so far", flush=True)
    return idx


def write_csv(out_path: Path, rows: list[dict]) -> int:
    cols = [
        "email", "first_name", "last_name", "company_name", "title",
        "linkedin_url",
        "email_1_subject", "email_1_body",
        "email_2_subject", "email_2_body",
        "email_3_subject", "email_3_body",
        "slop_pass", "signal_tier", "source_campaign",
    ]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, quoting=csv.QUOTE_ALL)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in cols})
    return len(rows)


def main() -> int:
    # The 5 STOPPED TenXVA campaigns where his ~1,500-2,000 recruiter leads live
    src_ids = [3103961, 3103959, 3085916, 3085917, 3080680]

    emails = populated_emails()
    print(f"[rebuild] {len(emails)} populated email files in data/emails/", flush=True)

    meta = lead_metadata_index(src_ids)
    print(f"[rebuild] {len(meta)} unique lead metadata entries from Smartlead", flush=True)

    rows = []
    no_meta = 0
    for email_addr, em in emails.items():
        seq = em.get("sequence") or []
        s1 = next((s for s in seq if s.get("step") == 1), {})
        s2 = next((s for s in seq if s.get("step") == 2), {})
        s3 = next((s for s in seq if s.get("step") == 3), {})
        m = meta.get(email_addr, {})
        if not m:
            no_meta += 1
            # try to infer a first name from the email local-part
            first = (email_addr.split("@", 1)[0] or "").split(".")[0].title()
            m = {"first_name": first, "last_name": "", "company_name": "",
                 "title": "", "linkedin_url": "", "source_campaign": ""}
        # also try to enrich tier from research cache
        rfile = REPO_ROOT / "data" / "research" / (
            email_addr.replace("@", "_").replace(".", "_") + ".json"
        )
        tier = ""
        if rfile.exists():
            try:
                tier = json.loads(rfile.read_text()).get("tier", "") or ""
            except Exception:
                pass
        rows.append({
            "email": email_addr,
            "first_name": m.get("first_name", ""),
            "last_name": m.get("last_name", ""),
            "company_name": m.get("company_name", ""),
            "title": m.get("title", ""),
            "linkedin_url": m.get("linkedin_url", ""),
            "email_1_subject": s1.get("subject", ""),
            "email_1_body": s1.get("body", ""),
            "email_2_subject": s2.get("subject", ""),
            "email_2_body": s2.get("body", ""),
            "email_3_subject": s3.get("subject", ""),
            "email_3_body": s3.get("body", ""),
            "slop_pass": "1" if em.get("slop_pass") else "0",
            "signal_tier": tier,
            "source_campaign": m.get("source_campaign", ""),
        })
    print(f"[rebuild] {no_meta} leads had no Smartlead metadata "
           f"(used email-derived first name)", flush=True)

    ts = dt.datetime.utcnow().strftime("%Y%m%dT%H%M%S")
    out = REPO_ROOT / "data" / "exports" / f"recovered-personalized-{ts}.csv"
    n = write_csv(out, rows)
    print(f"[rebuild] wrote {n} rows → {out}", flush=True)
    print(f"[rebuild] file size: {out.stat().st_size:,} bytes", flush=True)

    # Upload to Slack
    if "--upload" in sys.argv:
        from tools.slack_notify import SlackClient
        sc = SlackClient()
        channel = os.environ.get("SLACK_CONTROL_CHANNEL", "#cold-email-control")
        comment = (
            f":file_folder: *Recovered personalized leads CSV* — {n} leads\n"
            f"  • Source: `data/emails/*.json` cache (175 had real copy from "
            f"earlier launch_pilot runs)\n"
            f"  • Joined against your TenXVA Smartlead campaigns "
            f"(3103961, 3103959, 3085916, 3085917, 3080680) for name/company/title\n"
            f"  • Smartlead import: drag this into a campaign's *Add Leads → "
            f"Upload CSV* — map email_1/2/3 subject + body to custom fields.\n"
            f"  • The 324 leads NOT in this CSV had failed Copy runs (empty "
            f"sequences) and need re-personalization — kicking that off now."
        )
        sc.upload_file(channel, out, title=out.name,
                          initial_comment=comment)
        print(f"[rebuild] posted to Slack {channel}", flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
