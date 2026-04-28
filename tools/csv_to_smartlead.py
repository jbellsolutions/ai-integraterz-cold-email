"""Import a personalized-leads CSV directly into a Smartlead campaign.

Bridges the gap between data/exports/*.csv (output of personalize_to_csv)
and Smartlead's lead store. Handles:
  - CSV parsing
  - Mapping email_1/2/3_subject + body columns to custom_fields
  - Deduplication by email (within a single CSV; Smartlead dedupes
    cross-import server-side)
  - Bulk upload via SmartleadCLI.add_leads
  - Detailed result reporting (added / duplicate / invalid)

Usage:
  from tools.csv_to_smartlead import import_csv
  result = import_csv("data/exports/foo.csv", campaign_id=3249954)

Or via CLI:
  python -m tools.csv_to_smartlead --csv data/exports/foo.csv --campaign-id 3249954
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from tools.smartlead import SmartleadCLI


# Columns we treat as top-level Smartlead lead fields. Everything else
# (email_1_subject, email_1_body, email_2_subject, ..., signal_tier, etc.)
# goes into custom_fields. Smartlead's sequence templates reference custom
# fields with `{{field_name}}`.
TOP_LEVEL_FIELDS = {
    "email": "email",
    "first_name": "first_name",
    "last_name": "last_name",
    "company_name": "company_name",
    "phone": "phone_number",
    "phone_number": "phone_number",
    "linkedin_url": "linkedin_profile",
}


def import_csv(csv_path: str | Path, campaign_id: int | str) -> dict:
    """Read a personalized-leads CSV, push to Smartlead campaign, return
    a structured result dict."""
    p = Path(csv_path)
    if not p.exists():
        return {"error": f"csv not found: {csv_path}"}

    with p.open("r", encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        return {"error": f"csv has 0 rows: {csv_path}"}

    # Build Smartlead lead payload
    seen: set[str] = set()
    leads: list[dict] = []
    skipped_no_email = 0
    skipped_no_subject_body = 0
    deduped_within_csv = 0
    for r in rows:
        email = (r.get("email") or "").strip().lower()
        if not email:
            skipped_no_email += 1
            continue
        if email in seen:
            deduped_within_csv += 1
            continue
        seen.add(email)
        # Hard guard: don't ship a row with no email_1_body — that's the
        # exact bug we're trying not to repeat.
        if not (r.get("email_1_body") or "").strip() or \
                not (r.get("email_1_subject") or "").strip():
            skipped_no_subject_body += 1
            continue

        custom_fields: dict[str, str] = {}
        for k, v in r.items():
            if k in TOP_LEVEL_FIELDS or k == "email":
                continue
            if v is None:
                continue
            v = str(v).strip()
            if not v:
                continue
            custom_fields[k] = v

        leads.append({
            "first_name": r.get("first_name", "") or "",
            "last_name": r.get("last_name", "") or "",
            "email": email,
            "company_name": r.get("company_name", "") or "",
            "phone_number": r.get("phone") or r.get("phone_number") or "",
            "linkedin_profile": r.get("linkedin_url", "") or "",
            "custom_fields": custom_fields,
        })

    if not leads:
        return {
            "error": "no valid leads after filtering",
            "csv_rows": len(rows),
            "skipped_no_email": skipped_no_email,
            "skipped_no_subject_body": skipped_no_subject_body,
            "deduped_within_csv": deduped_within_csv,
        }

    cli = SmartleadCLI()
    upload = cli.add_leads(campaign_id, leads)

    return {
        "csv_path": str(p),
        "campaign_id": campaign_id,
        "csv_rows": len(rows),
        "leads_sent": len(leads),
        "skipped_no_email": skipped_no_email,
        "skipped_no_subject_body": skipped_no_subject_body,
        "deduped_within_csv": deduped_within_csv,
        "smartlead_response": upload,
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--csv", required=True)
    p.add_argument("--campaign-id", required=True)
    a = p.parse_args()
    r = import_csv(a.csv, a.campaign_id)
    print(json.dumps(r, indent=2, default=str))
    return 0 if "error" not in r else 2


if __name__ == "__main__":
    sys.exit(main())
