"""Lead loader. Two paths into the pipeline:
  1. CSV  — `load_leads(csv_path) -> list[Lead]`
  2. Smartlead Prospector — `load_leads_from_prospect(...)` which runs
     search → fetch → find-emails and maps the results into our Lead shape.

CSV is stdlib (polars 1.40 hangs on Python 3.13 Apple-Silicon — see earlier note).
"""
from __future__ import annotations

import csv
from pathlib import Path

from squads.research.squad import Lead
from tools.smartlead import SmartleadCLI

REQUIRED_COLS = ["name", "email", "company", "title"]
OPTIONAL_COLS = ["linkedin_url"]


def load_leads(csv_path: str | Path) -> list[Lead]:
    csv_path = Path(csv_path)
    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        cols = reader.fieldnames or []
        missing = [c for c in REQUIRED_COLS if c not in cols]
        if missing:
            raise ValueError(
                f"{csv_path}: missing required columns {missing}. "
                f"Found: {cols}. Required: {REQUIRED_COLS}"
            )
        leads = []
        for row in reader:
            leads.append(Lead(
                lead_id=str(row["email"]),
                name=str(row["name"]),
                email=str(row["email"]),
                company=str(row["company"]),
                title=str(row["title"]),
                linkedin_url=str(row.get("linkedin_url", "") or ""),
            ))
    return leads


def load_leads_from_prospect(
    saved_search_id: int | str | None = None,
    filters: dict | None = None,
    max_n: int = 20,
    max_fetch: int | None = None,
    confirm_threshold: int = 50,
    cli: SmartleadCLI | None = None,
) -> list[Lead]:
    """Pull leads from the Smartlead Prospector and map to our Lead shape.

    Two source modes (mutually exclusive):
      saved_search_id — reference an existing saved search built in Smartlead UI
      filters         — ad-hoc filter dict matching `prospect search` schema

    Flow:
      1. prospect search (free, returns filter_id + count)
      2. confirm count is acceptable (≤ confirm_threshold OR explicit max_fetch)
      3. prospect fetch (CONSUMES CREDITS)
      4. prospect find-emails for any contact missing email (batched 10/req)
      5. map → list[Lead]
    """
    cli = cli or SmartleadCLI()
    if saved_search_id is None and filters is None:
        raise ValueError("Provide either saved_search_id or filters.")
    if saved_search_id is not None and filters is not None:
        raise ValueError("Provide saved_search_id OR filters, not both.")

    # 1. search (free)
    if saved_search_id is not None:
        search_result = cli.prospect_search_saved(saved_search_id)
    else:
        search_result = cli.prospect_search(filters)
    filter_id = search_result.get("filter_id") or search_result.get("id")
    total_count = search_result.get("count") or search_result.get("total") or 0
    if not filter_id:
        raise RuntimeError(f"prospect search returned no filter_id. Response: {search_result}")

    # 2. credit-safety gate
    fetch_n = min(max_n, total_count) if total_count else max_n
    if max_fetch is not None:
        fetch_n = min(fetch_n, max_fetch)
    if fetch_n > confirm_threshold and max_fetch is None:
        raise RuntimeError(
            f"Prospector would fetch {fetch_n} contacts (consumes credits) — "
            f"above the safety threshold {confirm_threshold}. Re-run with explicit "
            f"--max-fetch={fetch_n} (or lower) to confirm."
        )
    if fetch_n == 0:
        return []

    # 3. fetch (consumes credits)
    contacts = cli.prospect_fetch(filter_id, fetch_n)

    # 4. find-emails for any without one
    needs_email = [c for c in contacts if not c.get("email")]
    if needs_email:
        emails_found = cli.prospect_find_emails(needs_email)
        # zip resolved emails back into the contact list (best-effort by name + domain)
        resolved = {(e.get("firstName", "").lower(), e.get("lastName", "").lower(),
                       e.get("companyDomain", "").lower()): e.get("email", "")
                      for e in emails_found if e.get("email")}
        for c in contacts:
            if c.get("email"):
                continue
            key = (c.get("firstName", "").lower(), c.get("lastName", "").lower(),
                    (c.get("companyDomain") or c.get("company_domain", "")).lower())
            if key in resolved:
                c["email"] = resolved[key]

    # 5. map → Lead
    leads: list[Lead] = []
    for c in contacts:
        first = c.get("firstName") or c.get("first_name", "")
        last = c.get("lastName") or c.get("last_name", "")
        email = c.get("email", "")
        if not email:
            continue   # skip contacts we couldn't resolve an email for
        leads.append(Lead(
            lead_id=email,
            name=f"{first} {last}".strip(),
            email=email,
            company=c.get("company") or c.get("company_name", ""),
            title=c.get("title") or c.get("job_title", ""),
            linkedin_url=c.get("linkedin") or c.get("linkedin_url", ""),
        ))
    return leads


def load_leads_from_campaign(campaign_id: int | str, max_n: int | None = None,
                                cli: SmartleadCLI | None = None) -> list[Lead]:
    """Pull leads OUT of an existing Smartlead campaign and map to Lead.

    Use case: an old campaign already has a hand-curated list of recruiters;
    we want to run them through OUR pipeline and seed a new (niche, offer,
    variant) campaign with the same people but new copy.
    """
    cli = cli or SmartleadCLI()
    raw = cli.list_campaign_leads(campaign_id, all_pages=True)
    if max_n:
        raw = raw[:max_n]
    leads: list[Lead] = []
    for item in raw:
        # Smartlead returns {lead: {...}, ...} with the actual lead under "lead"
        l = item.get("lead") if isinstance(item.get("lead"), dict) else item
        email = l.get("email") or ""
        if not email:
            continue
        first = l.get("first_name") or l.get("firstName") or ""
        last = l.get("last_name") or l.get("lastName") or ""
        leads.append(Lead(
            lead_id=email,
            name=f"{first} {last}".strip() or email,
            email=email,
            company=l.get("company_name") or l.get("company") or "",
            title=l.get("title") or l.get("job_title") or "",
            linkedin_url=l.get("linkedin_url") or l.get("linkedin") or "",
        ))
    return leads


def lead_summary(leads: list[Lead], n: int = 10) -> str:
    """Compact human-readable sample for the Strategy squad."""
    sample = leads[:n]
    lines = [f"Total leads: {len(leads)}", "Sample:"]
    for lead in sample:
        lines.append(f"  - {lead.name} ({lead.title}) at {lead.company}")
    if len(leads) > n:
        lines.append(f"  ... and {len(leads) - n} more")
    return "\n".join(lines)
