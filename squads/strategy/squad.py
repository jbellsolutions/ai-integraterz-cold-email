"""Strategy Squad — runs once per campaign.

Three specialists (parallel council, MAJORITY consensus):
- Angle Strategist  : picks/confirms the angle, loads campaigns/<angle>/ as context
- Offer Architect   : maps angle to specific value proposition for THIS audience
- ICP Critic        : adversarial — would this prospect actually care?

Output: campaign-brief.md saved to data/campaigns/<run_id>/campaign-brief.md
"""
from __future__ import annotations

from pathlib import Path

from forge import Consensus, Topology

from .._base import REPO_ROOT, Squad

INSTRUCTIONS = """You are a member of the Cold Email 2.0 Strategy Squad.

Your job, collectively, is to produce a campaign-brief.md that the Research \
and Copy squads will use as durable context for an entire cold-email campaign.

The brief must contain:
1. POSITIONING — one paragraph. What we are, why this prospect should care.
2. ANGLE — which campaigns/<angle>/ folder to load. Confirm or change.
3. OFFER — the specific ask, the value lead, the soft CTA shape.
4. SIGNAL CRITERIA — what counts as a strong per-prospect signal for this \
audience (recent post topics, hire types, funding events, podcast appearances).
5. RED LINES — words/phrases the Copy squad must avoid for this audience.
6. SEQUENCE PICK — for power-partner-style angles, recommend Sequence A, B, or C \
from sequences.md based on ICP segment.

Be specific to THIS audience. Generic advice is failure.

The ICP Critic role must push back hard if the angle/offer doesn't fit the \
list. Productive disagreement is the point of running this as a council."""


class StrategySquad(Squad):
    def __init__(self):
        super().__init__(
            name="strategy",
            instructions=INSTRUCTIONS,
            roles=["strategy", "strategy", "strategy"],
            topology=Topology.PARALLEL_COUNCIL,
            consensus=Consensus.MAJORITY,
            max_turns=6,
        )

    @staticmethod
    def load_angle_context(offer: str, niche: str | None = None) -> str:
        """Read all .md files in campaigns/<offer>/ recursively. If niche is
        provided AND campaigns/<offer>/niches/<niche>.md exists, that file is
        included (rglob already picks it up — this method validates and
        loud-fails if niche is requested but file missing).

        Loud failure if angle dir is empty (need to author first) or niche
        requested but no overlay file (would silently fall back to generic).
        """
        angle_dir = REPO_ROOT / "campaigns" / offer
        if not angle_dir.exists():
            raise FileNotFoundError(f"campaigns/{offer}/ does not exist")
        md_files = sorted(angle_dir.rglob("*.md"))
        if not md_files:
            raise FileNotFoundError(
                f"campaigns/{offer}/ has no .md files — author them first "
                f"(see campaigns/{offer}/README.md)"
            )
        if niche:
            niche_path = angle_dir / "niches" / f"{niche}.md"
            if not niche_path.exists():
                raise FileNotFoundError(
                    f"campaigns/{offer}/niches/{niche}.md missing. Author it before "
                    f"running --niche={niche} --offer={offer}, or remove --niche to "
                    f"use the generic offer-level copy."
                )
        chunks = []
        for f in md_files:
            rel = f.relative_to(angle_dir)
            chunks.append(f"### {rel}\n\n{f.read_text()}")
        return "\n\n---\n\n".join(chunks)

    async def build_brief(self, offer: str, lead_summary: str,
                            niche: str | None = None, variant: str = "A") -> str:
        """Run the squad and return the consensus campaign brief.

        offer: angle folder name (power-partner, capstone, direct-value)
        niche: optional segment overlay (recruiters, home-services, lawyers, doctors)
        variant: A/B/C — different copy framework / hook positioning within the same offer.
                 The squad is instructed to produce a meaningfully different brief per variant.
        """
        context = self.load_angle_context(offer, niche=niche)
        niche_line = f"NICHE: {niche}" if niche else "NICHE: (generic — no segment overlay)"
        variant_instructions = {
            "A": "Variant A — primary framing. Lead with the strongest single hook; default angle.",
            "B": "Variant B — alternative framing. Pick a meaningfully different hook than A: "
                 "if A leads with money, B leads with mechanism; if A leads with social proof, B "
                 "leads with curiosity. Keep the offer identical, change the door.",
            "C": "Variant C — third framing. Different again from A and B. Often the "
                 "personality/anti-pitch / give-first angle. Keep the offer identical.",
        }.get(variant.upper(), "Variant A — default framing.")

        task = f"""OFFER: {offer}
{niche_line}
VARIANT: {variant.upper()}
{variant_instructions}

LEAD SUMMARY (sample of the list you're writing for):
{lead_summary}

OFFER CONTEXT (campaigns/{offer}/, with any niche overlay):
{context}

Produce campaign-brief.md. The brief must include positioning, the chosen hook \
angle (different per variant), the offer ladder, the signal criteria, the red \
lines, and the sequence pick (if applicable). Bias the language toward the \
specific niche where one is set."""
        result = await self.run(task)
        # forge Verdict exposes the consensus text as `winner` (not `text`)
        if getattr(result, "verdict", None) and getattr(result.verdict, "winner", None):
            return result.verdict.winner
        if result.members:
            first = result.members[0]
            # members is list[tuple[str, MemberResult]] in forge
            mr = first[1] if isinstance(first, tuple) else first
            return getattr(mr, "final_text", "") or getattr(mr, "output", "") or str(mr)
        return ""
