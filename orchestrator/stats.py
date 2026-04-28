"""Stats command — quick operational snapshot.

Reads:
  data/replies/*.json    (per-reply records the daemon writes)
  data/campaigns/*/      (per-campaign run logs)
  smartlead campaigns list --format json   (live lead counts + status)

Prints a table grouped by (niche, offer, variant). No LLM calls.
"""
from __future__ import annotations

import datetime as dt
import json
from collections import defaultdict
from pathlib import Path

from rich.console import Console
from rich.table import Table

REPO_ROOT = Path(__file__).resolve().parents[1]


def _parse_campaign_name(name: str) -> tuple[str, str, str]:
    """<niche>-<offer>-<VARIANT> or <offer>-<VARIANT>. Returns (niche, offer, variant)."""
    if not name:
        return ("", "", "")
    parts = name.rsplit("-", 1)
    if len(parts) != 2:
        return ("", name, "A")
    base, variant = parts
    chunks = base.split("-")
    # offer slugs we know about
    known_offers = {"power-partner", "capstone", "direct-value", "aico",
                     "expert-series", "sovereign-blueprint"}
    # try longest-suffix match for offer
    for n_parts in range(len(chunks) - 1, -1, -1):
        candidate = "-".join(chunks[n_parts:])
        if candidate in known_offers:
            niche = "-".join(chunks[:n_parts])
            return (niche, candidate, variant.upper())
    return ("", base, variant.upper())


def _load_smartlead_campaigns() -> list[dict]:
    try:
        from tools.smartlead import SmartleadCLI
        return SmartleadCLI().list_campaigns()
    except Exception as e:
        print(f"[stats] warn: failed to list Smartlead campaigns: {e}")
        return []


def _load_replies() -> list[dict]:
    out = []
    rep_dir = REPO_ROOT / "data" / "replies"
    if not rep_dir.exists():
        return out
    for f in sorted(rep_dir.glob("*.json")):
        try:
            data = json.loads(f.read_text())
        except Exception:
            continue
        if isinstance(data, list):
            out.extend(x for x in data if isinstance(x, dict))
        elif isinstance(data, dict):
            out.append(data)
    return out


def print_stats() -> int:
    console = Console()
    campaigns = _load_smartlead_campaigns()
    replies = _load_replies()

    # bucket campaigns
    rows: dict[tuple, dict] = {}
    for c in campaigns:
        name = c.get("name", "")
        niche, offer, variant = _parse_campaign_name(name)
        key = (niche or "—", offer or "—", variant or "—")
        rows.setdefault(key, {
            "name": name, "id": c.get("id"), "status": c.get("status", ""),
            "leads": 0, "replies_total": 0, "replies_today": 0,
        })
        rows[key]["leads"] = c.get("lead_count") or c.get("leads_count") or rows[key]["leads"]

    # bucket replies by campaign name
    today = dt.date.today().isoformat()
    pending = 0
    sent_today = 0
    for r in replies:
        cname = r.get("campaign_name", "")
        niche, offer, variant = _parse_campaign_name(cname)
        key = (niche or "—", offer or "—", variant or "—")
        if key in rows:
            rows[key]["replies_total"] += 1
            if (r.get("created_at", "") or "").startswith(today):
                rows[key]["replies_today"] += 1
        if r.get("status") == "pending_approval":
            pending += 1
        if r.get("sent_at", "").startswith(today):
            sent_today += 1

    # render
    t = Table(title="Cold Email 2.0 — campaigns")
    for col in ("niche", "offer", "variant", "smartlead status", "leads", "replies", "replies today"):
        t.add_column(col)
    for (niche, offer, variant), v in sorted(rows.items()):
        t.add_row(niche, offer, variant, v["status"],
                   str(v["leads"]), str(v["replies_total"]), str(v["replies_today"]))
    console.print(t)

    # summary
    summary = Table(title="Replies daemon")
    summary.add_column("metric"); summary.add_column("value")
    summary.add_row("total reply records", str(len(replies)))
    summary.add_row("pending approval (Slack)", str(pending))
    summary.add_row("sent today", str(sent_today))
    summary.add_row("snapshot at", dt.datetime.now().isoformat(timespec="seconds"))
    console.print(summary)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(print_stats())
