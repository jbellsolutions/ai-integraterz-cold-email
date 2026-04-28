"""Research Squad — fans out per-prospect research in parallel.

Per the user's request and the transcript: we use polars to rip through CSVs and
spawn parallel agents (one per prospect) so 1000-row research finishes in minutes
not hours.

Per-prospect output: data/research/<lead_id>.json with
  { signal_type, signal_quote, signal_url, why_it_matters, tier (S/A/B/C) }
"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from pathlib import Path

from forge import Consensus, Spawner, SwarmSpec, ToolRegistry, Topology

from .._base import REPO_ROOT, load_routing

WORKER_INSTRUCTIONS = """You are a per-prospect research worker for Cold Email 2.0.

Given ONE prospect (name, email, company, title, linkedin_url) and a campaign \
brief that defines what counts as a "signal", your job is to find ONE specific, \
verifiable signal worth writing an email about.

A good signal is: a recent post, a recent hire, a product launch, a funding \
round, a podcast appearance, a conference talk, a public stance on something \
relevant. It must have a date, a quote, and a URL.

A bad signal is: "they're growing", "interesting company", "active on LinkedIn".

Output format (JSON only, no prose):
{
  "lead_id": "<email>",
  "signal_type": "post|hire|launch|funding|podcast|talk|stance|none",
  "signal_quote": "<verbatim quote, <200 chars>",
  "signal_url": "<URL>",
  "signal_date": "<YYYY-MM-DD>",
  "why_it_matters": "<1 sentence — how this connects to our offer>",
  "tier": "S|A|B|C"   // S = headline-grade, C = weak/skip
}

If you cannot find a real signal in 1-2 minutes of work, return tier=C with \
signal_type=none. Do NOT fabricate signals. A fabricated signal poisons the \
whole campaign — better to skip the lead."""

CRITIC_INSTRUCTIONS = """You are the Signal Critic for Cold Email 2.0.

Read the worker's JSON output. Score it against the campaign brief's SIGNAL CRITERIA.

Reject (force re-run) if:
- signal_quote is vague or paraphrased
- signal_url is missing or generic (homepage, not the actual signal)
- "why_it_matters" is generic (could apply to anyone)
- tier=S claimed but the signal is actually B or C

Pass otherwise. Output ONE word: PASS or REJECT, followed by a one-line reason."""


@dataclass
class Lead:
    lead_id: str
    name: str
    email: str
    company: str
    title: str
    linkedin_url: str = ""


class ResearchSquad:
    def __init__(self, brief: str):
        self.brief = brief
        self.tools = ToolRegistry()

    async def research_one(self, lead: Lead) -> dict:
        """Run a single research worker against one prospect."""
        routing = load_routing()
        spawner = Spawner(
            tools=self.tools,
            base_instructions=WORKER_INSTRUCTIONS + f"\n\nCAMPAIGN BRIEF:\n{self.brief}",
            max_turns=4,
        )
        spec = SwarmSpec(
            topology=Topology.SOLO,
            consensus=Consensus.QUEEN,
            members=[routing["research"]],
        )
        task = f"""Research this prospect:
name: {lead.name}
email: {lead.email}
company: {lead.company}
title: {lead.title}
linkedin: {lead.linkedin_url}"""
        result = await spawner.run(task, spec)
        text = result.members[0][1].final_text if result.members else "{}"
        # Best-effort JSON extraction
        m = re.search(r"\{.*\}", text, re.DOTALL)
        try:
            return json.loads(m.group(0)) if m else {"lead_id": lead.email, "tier": "C", "signal_type": "none"}
        except Exception:
            return {"lead_id": lead.email, "tier": "C", "signal_type": "none", "_raw": text}

    async def research_batch(self, leads: list[Lead], max_parallel: int = 10) -> list[dict]:
        """Fan out research over a batch of leads with a parallelism cap."""
        sem = asyncio.Semaphore(max_parallel)

        async def one(lead: Lead) -> dict:
            async with sem:
                signal = await self.research_one(lead)
                self._save(lead, signal)
                return signal

        return await asyncio.gather(*(one(lead) for lead in leads))

    @staticmethod
    def _save(lead: Lead, signal: dict) -> None:
        out_dir = REPO_ROOT / "data" / "research"
        out_dir.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^a-zA-Z0-9]+", "_", lead.email)
        (out_dir / f"{safe}.json").write_text(json.dumps(signal, indent=2))
