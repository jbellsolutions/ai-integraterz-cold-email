"""Natural-language → Prospector filter dict.

One Anthropic Haiku call. Validates against the cached filter values pulled
from Smartlead (`prospect industries`, `prospect levels`, `prospect headcounts`,
etc.) so the agent can't hallucinate filter values that don't exist server-side.

Caches filter values once per day to data/cache/prospect_filters.json — reused
across invocations to avoid hammering the CLI.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

from tools.smartlead import SmartleadCLI

REPO_ROOT = Path(__file__).resolve().parents[1]
CACHE_PATH = REPO_ROOT / "data" / "cache" / "prospect_filters.json"
FILTER_KINDS = ["industries", "sub-industries", "departments", "levels",
                "headcounts", "revenue", "cities", "states", "countries"]


def load_or_refresh_filter_cache(cli: SmartleadCLI | None = None,
                                   force: bool = False) -> dict:
    """Cache filter values for 24h. Returns dict keyed by filter kind."""
    cli = cli or SmartleadCLI()
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)

    if CACHE_PATH.exists() and not force:
        cache = json.loads(CACHE_PATH.read_text())
        cached_at = cache.get("cached_at")
        if cached_at:
            age = dt.datetime.utcnow() - dt.datetime.fromisoformat(cached_at)
            if age.total_seconds() < 86400:
                return cache.get("data", {})

    data: dict[str, list] = {}
    for kind in FILTER_KINDS:
        try:
            data[kind] = cli.list_filter_values(kind)
        except Exception as e:
            data[kind] = []
            print(f"[prospect_filters] warn: failed to fetch {kind}: {e}")
    CACHE_PATH.write_text(json.dumps({
        "cached_at": dt.datetime.utcnow().isoformat(), "data": data,
    }, indent=2, default=str))
    return data


def nl_to_filters(natural_language: str, cli: SmartleadCLI | None = None) -> dict:
    """Translate a natural-language target description to a Prospector filter dict.

    Example input: "recruiting agency owners in US, 11-50 employees, founder/owner seniority"
    Example output: {"industries": ["Staffing & Recruiting"], "countries": ["US"],
                       "headcounts": ["11-50"], "levels": ["Owner", "Founder"]}
    """
    import anthropic   # lazy import — only need this for NL mode

    filter_data = load_or_refresh_filter_cache(cli)
    # Compact a summary of available values to send to the model
    available = {
        kind: [v.get("name") or v.get("value") or v for v in (filter_data.get(kind) or [])][:60]
        for kind in FILTER_KINDS
    }

    prompt = f"""You translate prospect-targeting descriptions into Smartlead Prospector filter dicts.

USER REQUEST:
{natural_language}

AVAILABLE FILTER VALUES (you may ONLY pick from these — do not invent):
{json.dumps(available, indent=2)}

Output a JSON object with keys from this set: industries, sub_industries, departments,
levels, headcounts, revenue, cities, states, countries, keywords, job_titles. Each
value is a list of strings drawn from AVAILABLE FILTER VALUES (or freeform for keywords
and job_titles). OMIT any keys you do not need. If the user names something not in the
available values, pick the closest match or leave that key out.

Output ONLY the JSON, no prose."""
    client = anthropic.Anthropic()
    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    text = msg.content[0].text.strip()
    # tolerate fenced code blocks
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.lstrip().startswith("json"):
            text = text.lstrip()[4:]
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"NL→filters returned invalid JSON: {text[:300]}") from e
