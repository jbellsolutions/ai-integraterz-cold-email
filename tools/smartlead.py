"""Unified Smartlead integration layer.

Three seams, used by job:

  CLI    — bulk data ops: campaign create, lead add, inbox fetch, status changes,
           webhook config, exports. Fast, no LLM context spent.
  REST   — fallback for endpoints not in CLI v0.1.0 (currently: none we need).
           Keep the seam so we can wire new endpoints without re-architecting.
  LLM    — reply reasoning happens in our reply squad (Triage/Drafter/Approver),
           NOT through a Smartlead MCP. The community smartlead-mcp-server is
           stale (last updated 2025-04, no inbox support) and would add a hop
           without capability.

Mock mode: set CE2_MOCK_SMARTLEAD=1 to short-circuit all real calls. Useful for
dry-run pilots and offline tests.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any

import httpx


def _mock_enabled() -> bool:
    return os.environ.get("CE2_MOCK_SMARTLEAD", "").strip() not in ("", "0", "false")


# ---- CLI seam --------------------------------------------------------------

class SmartleadCLI:
    """Thin subprocess wrapper around the @smartlead/cli npm tool.

    Surface (v0.1.0): config, campaigns, leads, mailboxes, stats, delivery,
    webhooks, clients, senders, analytics, inbox, prospect.

    All methods that take complex bodies serialize via --from-json (a tempfile)
    rather than long flag strings — Smartlead's CLI prefers this form.
    """

    def __init__(self, command: str = "smartlead"):
        self.command = command

    # campaigns ----------------------------------------------------------

    def create_campaign(self, name: str, client_id: int | None = None) -> dict[str, Any]:
        if _mock_enabled():
            return {"id": f"mock-{uuid.uuid4().hex[:8]}", "name": name, "status": "DRAFTED"}
        args = ["campaigns", "create", "--name", name]
        if client_id:
            args += ["--client-id", str(client_id)]
        return self._run_json(args)

    def save_sequence(self, campaign_id: int | str, sequences: list[dict]) -> dict:
        """Save a multi-step sequence to a campaign.

        `sequences` is a list of step dicts in Smartlead's shape. Per-prospect
        bodies use Smartlead spintax + custom-field placeholders ({{var}}).
        """
        if _mock_enabled():
            return {"saved": True, "step_count": len(sequences)}
        body = {"sequences": sequences}
        return self._run_json_body(["campaigns", "save-sequence", "--id", str(campaign_id)], body)

    def set_status(self, campaign_id: int | str, status: str) -> dict:
        """API allowed values: START | PAUSED | STOPPED (CLI help misleadingly
        documents 'STOP' but the API rejects it; use STOPPED)."""
        if _mock_enabled():
            return {"id": campaign_id, "status": status}
        return self._run_json(["campaigns", "set-status",
                                "--id", str(campaign_id), "--status", status])

    def list_campaigns(self) -> list[dict]:
        """Full campaigns list with statuses."""
        if _mock_enabled():
            return []
        out = self._run_json(["campaigns", "list"])
        return out if isinstance(out, list) else out.get("data", [])

    def list_campaign_leads(self, campaign_id: int | str, limit: int = 100,
                              all_pages: bool = True) -> list[dict]:
        """Pull leads attached to an existing Smartlead campaign.

        all_pages=True uses --all and ignores limit. Useful when you want every
        lead the campaign has (e.g. taking an existing recruiter list and
        re-targeting it with new copy).
        """
        if _mock_enabled():
            return []
        args = ["leads", "list", "--campaign-id", str(campaign_id)]
        if all_pages:
            args.append("--all")
        else:
            args.extend(["--limit", str(limit)])
        out = self._run_json(args)
        return out if isinstance(out, list) else out.get("data", []) or []

    def delete_campaign(self, campaign_id: int | str) -> dict:
        if _mock_enabled():
            return {"deleted": True, "id": campaign_id}
        return self._run_json(["campaigns", "delete", "--id", str(campaign_id), "--confirm"])

    def archive_campaign(self, campaign_id: int | str, mode: str = "STOP") -> dict:
        """Archive a campaign — STOP (reversible, → STOPPED status) or DELETE (permanent).
        mode: 'STOP' | 'DELETE'."""
        if mode.upper() == "DELETE":
            return self.delete_campaign(campaign_id)
        return self.set_status(campaign_id, "STOPPED")

    def get_campaign_by_name(self, name: str) -> dict | None:
        """Look up an existing campaign by exact name. None if not found.
        Used by SmartleadSquad to implement idempotent 'append leads to
        <niche>-<offer>-<variant>' semantics."""
        for c in self.list_campaigns():
            if c.get("name") == name:
                return c
        return None

    # prospector ---------------------------------------------------------

    def list_saved_searches(self) -> list[dict]:
        if _mock_enabled():
            return []
        out = self._run_json(["prospect", "saved-searches"])
        return out if isinstance(out, list) else out.get("data", [])

    def list_filter_values(self, kind: str) -> list[dict]:
        """kind: industries | sub-industries | departments | levels | headcounts |
        revenue | cities | states | countries. Free (no credits)."""
        if _mock_enabled():
            return []
        out = self._run_json(["prospect", kind])
        return out if isinstance(out, list) else out.get("data", [])

    def prospect_search(self, filters: dict) -> dict:
        """Run a prospect search with the given filter shape. Returns {filter_id, count, ...}.
        Free — does NOT consume credits. Use the returned filter_id with prospect_fetch."""
        if _mock_enabled():
            return {"filter_id": "mock-fid", "count": 0}
        return self._run_json_body(["prospect", "search"], filters)

    def prospect_search_saved(self, saved_search_id: int | str) -> dict:
        """Resolve a saved search to a fresh filter_id + count."""
        if _mock_enabled():
            return {"filter_id": "mock-fid", "count": 0, "saved_search_id": saved_search_id}
        return self._run_json_body(["prospect", "search"], {"saved_search_id": saved_search_id})

    def prospect_fetch(self, filter_id: str, limit: int) -> list[dict]:
        """Fetch contact details. CONSUMES CREDITS — call only after confirming
        the count from prospect_search is acceptable. Returns list of contacts."""
        if _mock_enabled():
            return []
        out = self._run_json_body(["prospect", "fetch"],
                                    {"filter_id": filter_id, "limit": int(limit)})
        if isinstance(out, list):
            return out
        return out.get("data", out.get("contacts", []))

    def prospect_find_emails(self, contacts: list[dict]) -> list[dict]:
        """Find emails for contacts that don't already have one. Max 10/request — batched."""
        if _mock_enabled():
            return contacts
        results: list[dict] = []
        for i in range(0, len(contacts), 10):
            batch = contacts[i:i + 10]
            payload = {"contacts": [
                {"firstName": c.get("firstName", ""),
                 "lastName": c.get("lastName", ""),
                 "companyDomain": c.get("companyDomain") or c.get("company_domain", "")}
                for c in batch
            ]}
            out = self._run_json_body(["prospect", "find-emails"], payload)
            chunk = out if isinstance(out, list) else out.get("data", [])
            results.extend(chunk)
        return results

    # leads --------------------------------------------------------------

    def add_leads(self, campaign_id: int | str, leads: list[dict]) -> dict:
        """leads is a list of dicts with smartlead's lead shape:
            {first_name, last_name, email, company_name, custom_fields: {...}}
        """
        if _mock_enabled():
            return {"upload_count": len(leads), "duplicate_count": 0, "invalid_count": 0,
                    "campaign_id": campaign_id}
        body = {
            "lead_list": leads,
            "settings": {"ignore_global_block_list": False, "ignore_unsubscribe_list": False},
        }
        return self._run_json_body(["leads", "add", "--campaign-id", str(campaign_id)], body)

    # inbox --------------------------------------------------------------

    def fetch_unread_replies(self, limit: int = 20, offset: int = 0) -> list[dict]:
        if _mock_enabled():
            return []
        out = self._run_json(["inbox", "unread", "--limit", str(limit), "--offset", str(offset)])
        if isinstance(out, list):
            return out
        return out.get("data", out.get("replies", []))

    def fetch_replies(self, limit: int = 20, offset: int = 0) -> list[dict]:
        """Fetch inbox reply overviews. Each item has lead info + last_reply_time
        but NO body — bodies must be fetched via get_lead_messages()."""
        if _mock_enabled():
            return []
        out = self._run_json(["inbox", "replies", "--limit", str(limit), "--offset", str(offset)])
        if isinstance(out, list):
            return out
        return out.get("data", out.get("replies", []))

    def get_lead_messages(self, campaign_id: int | str, lead_id: int | str) -> list[dict]:
        """Full thread for one lead in one campaign.
        Returns list of {type: SENT|REPLY, from, to, time, subject, email_body (HTML), email_seq_number}."""
        if _mock_enabled():
            return []
        out = self._run_json(["leads", "messages", "--campaign-id", str(campaign_id),
                               "--lead-id", str(lead_id)])
        if isinstance(out, dict):
            return out.get("history", out.get("data", []))
        return out if isinstance(out, list) else []

    def reply_to_thread(self, campaign_id: int | str, email_stats_id: str,
                         email_body: str, reply_body_text: str | None = None) -> dict:
        """Send an outbound reply on a thread. email_stats_id comes from a fetch_replies row.

        Per the CLI, reply expects a JSON body with at least:
            campaign_id, email_stats_id, email_body, reply_body_text (optional)
        """
        if _mock_enabled():
            return {"sent": True, "email_stats_id": email_stats_id}
        body: dict[str, Any] = {
            "campaign_id": campaign_id,
            "email_stats_id": email_stats_id,
            "email_body": email_body,
        }
        if reply_body_text:
            body["reply_body_text"] = reply_body_text
        return self._run_json_body(["inbox", "reply"], body)

    def update_lead_category(self, lead_id: int | str, category_id: int) -> dict:
        if _mock_enabled():
            return {"lead_id": lead_id, "category_id": category_id}
        body = {"category_id": category_id}
        return self._run_json_body(["inbox", "update-category", "--lead-id", str(lead_id)], body)

    # webhooks (for upgrade path from polling → push) -------------------

    def upsert_webhook(self, campaign_id: int | str, url: str, events: list[str]) -> dict:
        if _mock_enabled():
            return {"webhook_id": f"mock-{uuid.uuid4().hex[:8]}", "url": url}
        body = {"webhook_url": url, "event_types": events}
        return self._run_json_body(["webhooks", "upsert", "--campaign-id", str(campaign_id)], body)

    # internals ----------------------------------------------------------

    def _run(self, args: list[str], extra_env: dict | None = None) -> str:
        env = os.environ.copy()
        if extra_env:
            env.update(extra_env)
        try:
            r = subprocess.run(
                [self.command, "--format", "json", "--retry", *args],
                capture_output=True, text=True, check=True, timeout=120, env=env,
            )
            return r.stdout
        except FileNotFoundError as e:
            raise RuntimeError(
                "Smartlead CLI not found. Install: npm install -g @smartlead/cli  "
                "or set CE2_MOCK_SMARTLEAD=1"
            ) from e
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"smartlead {' '.join(args)} failed: {e.stderr.strip()}") from e

    def _run_json(self, args: list[str]) -> Any:
        out = self._run(args)
        return _safe_json(out, default={})

    def _run_json_body(self, args: list[str], body: dict) -> Any:
        """Write body to a temp file and pass via --from-json. Smartlead's CLI
        prefers this for any non-trivial payload."""
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        try:
            json.dump(body, tmp)
            tmp.flush()
            tmp.close()
            return self._run_json([*args, "--from-json", tmp.name])
        finally:
            Path(tmp.name).unlink(missing_ok=True)


# ---- REST seam (fallback) --------------------------------------------------

class SmartleadREST:
    """Direct REST API client for any operation the CLI doesn't expose cleanly.

    As of @smartlead/cli v0.1.0 the CLI covers our needs end-to-end, so this
    class is dormant — kept as a seam so we can add endpoints without
    re-architecting if the CLI lags.
    """

    def __init__(self, api_key: str | None = None, base_url: str = "https://server.smartlead.ai/api/v1"):
        self.api_key = api_key or os.environ.get("SMARTLEAD_API_KEY", "")
        self.base_url = base_url
        self.client = httpx.Client(timeout=60.0)

    def _url(self, path: str) -> str:
        sep = "&" if "?" in path else "?"
        return f"{self.base_url}{path}{sep}api_key={self.api_key}"

    def get(self, path: str) -> dict:
        if _mock_enabled():
            return {}
        r = self.client.get(self._url(path))
        r.raise_for_status()
        return r.json()

    def post(self, path: str, body: dict) -> dict:
        if _mock_enabled():
            return {"_mock": True}
        r = self.client.post(self._url(path), json=body)
        r.raise_for_status()
        return r.json()


def _safe_json(text: str, default: Any) -> Any:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return default
