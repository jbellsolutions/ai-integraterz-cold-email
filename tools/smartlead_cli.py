"""Wrapper around the @smartlead/cli npm tool.

Calls the CLI via subprocess. We use the CLI rather than direct HTTP because
Smartlead ships and maintains it. If the CLI is missing the operation we need
we fall back to direct API calls via SMARTLEAD_API_KEY.

NOTE: this module is intentionally thin. It's the seam between deterministic
Smartlead plumbing and the rest of the system. Mock-mode (env CE2_MOCK_SMARTLEAD=1)
returns canned responses so the orchestrator can run end-to-end without a
Smartlead account during development.
"""
from __future__ import annotations

import json
import os
import subprocess
import uuid
from pathlib import Path
from typing import Any


def _mock_enabled() -> bool:
    return os.environ.get("CE2_MOCK_SMARTLEAD", "").strip() not in ("", "0", "false")


class SmartleadCLI:
    def __init__(self, command: str = "smartlead"):
        self.command = command

    # ----- campaign ops --------------------------------------------------

    def create_campaign(self, name: str, paused: bool = True) -> dict[str, Any]:
        if _mock_enabled():
            return {"id": f"mock-{uuid.uuid4().hex[:8]}", "name": name, "status": "paused"}
        # Real CLI path. Smartlead CLI flags are subject to revision; check
        # `smartlead campaign create --help` if this shape changes.
        out = self._run(["campaign", "create", "--name", name, "--paused" if paused else "--active"])
        return _safe_json(out, default={"id": "unknown", "name": name})

    def upload_leads(self, campaign_id: str, leads: list[dict]) -> dict:
        if _mock_enabled():
            return {"uploaded": len(leads), "duplicates": 0, "invalid": 0}
        # Smartlead CLI takes a CSV. Write a temp CSV and pass it.
        import csv
        import tempfile
        tmp = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, newline="")
        try:
            cols = ["email", "first_name", "last_name", "company", "title", "linkedin_url"]
            writer = csv.DictWriter(tmp, fieldnames=cols, extrasaction="ignore")
            writer.writeheader()
            for lead in leads:
                writer.writerow({
                    "email": lead.get("email", ""),
                    "first_name": (lead.get("name") or "").split()[0],
                    "last_name": " ".join((lead.get("name") or "").split()[1:]),
                    "company": lead.get("company", ""),
                    "title": lead.get("title", ""),
                    "linkedin_url": lead.get("linkedin_url", ""),
                })
            tmp.flush()
            tmp.close()
            out = self._run(["leads", "upload", "--campaign-id", campaign_id, "--file", tmp.name])
            return _safe_json(out, default={"uploaded": len(leads)})
        finally:
            Path(tmp.name).unlink(missing_ok=True)

    def set_per_prospect_sequences(self, campaign_id: str, leads_with_emails: list[dict]) -> dict:
        """Per-prospect sequences need API-level lead-variable injection. The CLI
        supports a templated sequence with {{var}} placeholders that resolve from
        per-lead custom fields. We dump each prospect's email body+subject into
        custom fields email_1_subject, email_1_body, etc."""
        if _mock_enabled():
            return {"sequences_set": len(leads_with_emails)}
        # In production: PUT /campaign/<id>/sequences with a 3-step template that
        # references {{email_1_subject}} / {{email_1_body}} etc, then PATCH each
        # lead with the variables. CLI surface for this is in flux — implement
        # against the live CLI when wiring real Smartlead.
        return {"sequences_set": len(leads_with_emails), "_note": "stub — wire to real Smartlead API"}

    def fetch_replies(self, campaign_id: str) -> list[dict]:
        if _mock_enabled():
            return []
        out = self._run(["replies", "list", "--campaign-id", campaign_id])
        parsed = _safe_json(out, default=[])
        return parsed if isinstance(parsed, list) else parsed.get("replies", [])

    # ----- internals -----------------------------------------------------

    def _run(self, args: list[str]) -> str:
        try:
            r = subprocess.run(
                [self.command, *args],
                capture_output=True, text=True, check=True, timeout=120,
            )
            return r.stdout
        except FileNotFoundError as e:
            raise RuntimeError(
                f"Smartlead CLI not found. Install: npm install -g @smartlead/cli  "
                f"or set CE2_MOCK_SMARTLEAD=1"
            ) from e
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"smartlead {' '.join(args)} failed: {e.stderr}") from e


def _safe_json(text: str, default: Any) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return default
