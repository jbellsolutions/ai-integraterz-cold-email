"""Smartlead Squad — campaign builder.

This squad's job is the deterministic plumbing of getting a campaign + per-
prospect sequences uploaded to Smartlead in DRAFTED state. No LLM reasoning
here — just CLI calls. Reply handling lives in squads/reply/.

Campaigns are named <niche>-<offer>-<variant> and are LOOKED UP first; new
leads are appended to an existing campaign rather than creating duplicates.
This makes the same command idempotent — you can run a pilot, get more leads
from a different prospect filter, run again, and the campaign accumulates.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path

from .._base import REPO_ROOT
from tools.smartlead import SmartleadCLI


def make_campaign_name(niche: str | None, offer: str, variant: str = "A") -> str:
    """Canonical campaign name. <niche>-<offer>-<variant> if niche set,
    else <offer>-<variant> for generic / cross-niche campaigns."""
    parts = [p for p in [niche, offer] if p]
    parts.append((variant or "A").upper())
    name = "-".join(parts).lower()
    # variant token in upper for readability — recompose
    base, var = name.rsplit("-", 1)
    return f"{base}-{var.upper()}"


class SmartleadSquad:
    def __init__(self):
        self.cli = SmartleadCLI()

    def precreate_campaign(self, offer: str, niche: str | None = None,
                            variant: str = "A",
                            campaign_name_override: str | None = None) -> dict:
        """Create empty DRAFTED campaign with sequence template baked in.
        Idempotent: if a campaign with the resolved name already exists, returns
        without re-saving the template.
        """
        return self.build_campaign(offer, leads_with_emails=[], niche=niche,
                                    variant=variant,
                                    campaign_name_override=campaign_name_override)

    def build_campaign(self, offer: str, leads_with_emails: list[dict],
                        niche: str | None = None, variant: str = "A",
                        campaign_name_override: str | None = None) -> dict:
        """Create-or-append. Look up `<niche>-<offer>-<variant>` first; if it
        exists, append leads to it. If not, create + save sequence template +
        add leads. Idempotent.

        Each item in leads_with_emails is {lead: Lead.__dict__, emails: {sequence: [...]}}.

        Per-prospect bodies are injected via custom fields on each lead. The
        sequence template references {{email_1_subject}} / {{email_1_body}} so
        each prospect renders their own copy at send time.
        """
        name = campaign_name_override or make_campaign_name(niche, offer, variant)

        # 1. Lookup-or-create
        existing = self.cli.get_campaign_by_name(name)
        if existing:
            campaign_id = existing.get("id")
            created = False
        else:
            campaign = self.cli.create_campaign(name=name)
            campaign_id = campaign["id"]
            created = True

        # 2. Build per-prospect leads with custom fields holding their copy
        leads_payload = []
        for item in leads_with_emails:
            lead = item["lead"]
            sequence = item["emails"].get("sequence", [])
            custom_fields = {}
            for step in sequence:
                i = step.get("step", 0)
                custom_fields[f"email_{i}_subject"] = step.get("subject", "")
                custom_fields[f"email_{i}_body"] = step.get("body", "")
            full_name = lead.get("name", "")
            first, *rest = full_name.split(" ", 1) if full_name else ("", "")
            leads_payload.append({
                "first_name": first,
                "last_name": rest[0] if rest else "",
                "email": lead.get("email", ""),
                "company_name": lead.get("company", ""),
                "custom_fields": custom_fields,
            })
        # Skip add_leads when payload is empty — supports leadless precreation
        # (campaign created in DRAFTED state with sequence template, ready for
        # later add_leads calls).
        upload_result = (self.cli.add_leads(campaign_id, leads_payload)
                          if leads_payload else {"skipped": "no leads supplied"})

        # 3. Save sequence template ONLY on first creation. Re-saving on append
        #    would clobber the variant's tuned sequence with a fresh one — bad.
        if created:
            delays = [0, 3, 7]
            sequences_template = []
            for i, delay in enumerate(delays, start=1):
                sequences_template.append({
                    "seq_number": i,
                    "seq_delay_details": {"delay_in_days": delay},
                    "variant_distribution_type": "MANUAL_EQUAL",
                    "seq_variants": [{
                        "subject": f"{{{{email_{i}_subject}}}}",
                        "email_body": f"{{{{email_{i}_body}}}}",
                        "variant_label": "v1",
                    }],
                })
            self.cli.save_sequence(campaign_id, sequences_template)

        # 4. Persist a per-campaign record under data/campaigns/<name>/
        slot = REPO_ROOT / "data" / "campaigns" / name
        slot.mkdir(parents=True, exist_ok=True)
        run_log = {
            "campaign_id": campaign_id,
            "campaign_name": name,
            "niche": niche,
            "offer": offer,
            "variant": variant.upper(),
            "leads_in_this_run": len(leads_payload),
            "upload_result": upload_result,
            "created_now": created,
            "run_at": dt.datetime.utcnow().isoformat(),
        }
        run_id = re.sub(r"[^0-9T]", "", run_log["run_at"])[:14]
        (slot / f"run-{run_id}.json").write_text(json.dumps(run_log, indent=2))
        return run_log
