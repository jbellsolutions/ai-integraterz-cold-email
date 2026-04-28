"""Cold Email 2.0 Orchestrator.

This is the entry point the user talks to. In conversational mode it runs as
an Opus 4.7 (1M context) Claude Code/SDK agent that fans out to the four squads.
In CLI mode it executes a deterministic pipeline for batch runs.

Squads (forge.Spawner under the hood):
  Strategy → builds campaign-brief.md
  Research → per-prospect signal mining (parallel)
  Copy     → 3-email sequence per prospect (hook → body → slop critic)
  Smartlead→ pause-state campaign in Smartlead, ready for human review

CLI usage:
  python -m orchestrator.main --smoke
  python -m orchestrator.main --angle=power-partner --leads=data/leads/pilot.csv
  python -m orchestrator.main --angle=power-partner --leads=data/leads/pilot.csv --max=20
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))  # allow `from squads...` and `from tools...` from CLI

from squads.copy import CopySquad
from squads.research import ResearchSquad
from squads.research.squad import Lead
from squads.smartlead import SmartleadSquad
from squads.strategy import StrategySquad
from tools.lead_loader import lead_summary, load_leads

console = Console(force_terminal=False)


def _say(msg: str) -> None:
    print(msg, flush=True)


async def smoke() -> int:
    """Phase 0 + Phase 1 smoke: forge boots, all 4 squads instantiate."""
    _say("=== Cold Email 2.0 — smoke test ===")

    # Phase 0: forge harness
    from forge import Spawner, SwarmSpec, Topology, Consensus, ToolRegistry
    spawner = Spawner(tools=ToolRegistry(), base_instructions="smoke", max_turns=2)
    spec = SwarmSpec(topology=Topology.PARALLEL_COUNCIL, consensus=Consensus.MAJORITY,
                     members=["mock", "mock", "mock"])
    result = await spawner.run("smoke test", spec)
    assert len(result.members) == 3
    _say("✓ forge harness alive (3-member parallel council against mock)")

    # Phase 1: each squad instantiates
    StrategySquad()
    _say("✓ Strategy squad instantiated")
    rs = ResearchSquad(brief="(smoke brief)")
    _say("✓ Research squad instantiated")
    cs = CopySquad(brief="(smoke brief)")
    _say("✓ Copy squad instantiated")
    SmartleadSquad()
    _say("✓ Smartlead squad instantiated")

    # Phase 2: angle context loads
    try:
        ctx = StrategySquad.load_angle_context("power-partner")
        _say(f"✓ power-partner angle context loads ({len(ctx)} chars)")
    except FileNotFoundError as e:
        _say(f"✗ power-partner angle missing: {e}")
        return 1

    # Phase 3: slop critic deterministic check
    from squads.copy.squad import slop_check
    bad = "I hope this email finds you well — I noticed your great work and wanted to circle back."
    passes, hits = slop_check(bad)
    assert not passes and len(hits) >= 3
    good = "Saw the Lattice news. Curious if your AE team is still running outbound full-cycle."
    passes2, _ = slop_check(good)
    assert passes2
    _say("✓ AI-slop critic rejects bad copy, passes good copy")

    _say("=== ALL SMOKE TESTS PASSED ===")
    return 0


async def run_pilot(
    offer: str,
    leads: list,
    *,
    niche: str | None = None,
    variant: str = "A",
    campaign_name_override: str | None = None,
    source_label: str = "csv",
    dry_run: bool = False,
) -> int:
    """Strategy → Research → Copy → Smartlead. Niche-aware, variant-aware, idempotent.

    leads is a pre-loaded list[Lead] — caller decides CSV vs Prospector vs other source.
    """
    from squads.smartlead.squad import make_campaign_name
    campaign_name = campaign_name_override or make_campaign_name(niche, offer, variant)

    _say(f"=== Cold Email 2.0 — pilot: {campaign_name} ({len(leads)} leads from {source_label}) ===")
    if not leads:
        _say("no leads to process — exiting")
        return 0

    summary = lead_summary(leads)
    _say("--- Lead summary ---\n" + summary)

    if dry_run:
        _say("\n[dry-run] skipping LLM stages and Smartlead writes. Lead loading verified.")
        return 0

    # 1. Strategy squad → brief (cached per <campaign_name>)
    brief_dir = REPO_ROOT / "data" / "campaigns" / campaign_name
    brief_dir.mkdir(parents=True, exist_ok=True)
    brief_path = brief_dir / "brief.md"
    if brief_path.exists():
        _say(f"\nStage 1/4: Strategy — reusing cached brief at {brief_path.relative_to(REPO_ROOT)}")
        brief = brief_path.read_text()
    else:
        _say(f"\nStage 1/4: Strategy (Opus, ~30s) — generating brief for {campaign_name}")
        strategy = StrategySquad()
        brief = await strategy.build_brief(offer, summary, niche=niche, variant=variant)
        brief_path.write_text(brief)
        _say(f"✓ campaign brief → {brief_path.relative_to(REPO_ROOT)} ({len(brief)} chars)")

    # 2. Research squad → per-prospect signals (parallel)
    _say(f"\nStage 2/4: Research (Haiku × {len(leads)} parallel)")
    research = ResearchSquad(brief=brief)
    signals = await research.research_batch(leads, max_parallel=10)
    tier_counts: dict[str, int] = {}
    for s in signals:
        tier_counts[s.get("tier", "?")] = tier_counts.get(s.get("tier", "?"), 0) + 1
    tier_str = ", ".join(f"{t}={n}" for t, n in sorted(tier_counts.items()))
    _say(f"  signal tiers: {tier_str}")

    # 3. Copy squad → per-prospect sequences
    _say(f"\nStage 3/4: Copy (Sonnet hook + Haiku body × {len(leads)})")
    copy = CopySquad(brief=brief)
    sem = asyncio.Semaphore(8)

    async def write_one(lead: Lead, signal: dict):
        async with sem:
            return await copy.write_one(lead, signal)

    emails = await asyncio.gather(*(write_one(lead, sig) for lead, sig in zip(leads, signals)))
    slop_pass = sum(1 for e in emails if e.get("slop_pass"))
    _say(f"✓ {slop_pass}/{len(emails)} sequences passed slop critic")

    # 4. Smartlead squad → DRAFTED campaign (lookup-or-create + append leads)
    _say(f"\nStage 4/4: Smartlead (lookup-or-create '{campaign_name}', append {len(leads)} leads)")
    sl = SmartleadSquad()
    leads_with_emails = [
        {"lead": lead.__dict__, "emails": email}
        for lead, email in zip(leads, emails)
    ]
    run_log = sl.build_campaign(offer, leads_with_emails, niche=niche, variant=variant,
                                  campaign_name_override=campaign_name_override)
    _say("--- Smartlead campaign ---\n" + json.dumps(run_log, indent=2))

    _say(
        f"\n=== Pilot complete ===\n"
        f"Campaign '{campaign_name}' "
        f"{'CREATED' if run_log.get('created_now') else 'APPENDED-TO'} in DRAFTED state.\n"
        f"Inspect data/emails/*.json + Smartlead UI before resuming.\n"
    )
    return 0


def cli() -> int:
    p = argparse.ArgumentParser(prog="cold-email", description="Cold Email 2.0 orchestrator")
    p.add_argument("--smoke", action="store_true", help="run smoke test (no LLM cost beyond mock)")
    p.add_argument("--stats", action="store_true", help="print campaigns + replies summary")

    # campaign axis
    p.add_argument("--offer", "--angle", dest="offer",
                    help="offer (folder under campaigns/) — power-partner|capstone|direct-value")
    p.add_argument("--niche", help="niche segment (recruiters|home-services|lawyers|...)")
    p.add_argument("--variant", default="A", help="copy variant A/B/C (default A)")
    p.add_argument("--campaign-name", help="override campaign name (escape hatch)")

    # lead source — exactly one
    p.add_argument("--leads", help="path to leads CSV")
    p.add_argument("--from-prospect-saved", help="Smartlead Prospector saved-search ID")
    p.add_argument("--from-prospect-filters", help="path to JSON file with Prospector filter dict")
    p.add_argument("--prospect-nl", help="natural-language description; agent translates to filters")

    # safety / limits
    p.add_argument("--max", type=int, default=None, help="cap N leads (pilot)")
    p.add_argument("--max-fetch", type=int, default=None,
                    help="explicit cap for Prospector fetch (acknowledges credit cost)")
    p.add_argument("--dry-run", action="store_true",
                    help="load leads + verify, but skip LLM stages and Smartlead writes")

    # reply daemon
    p.add_argument("--slack-agent", action="store_true",
                    help="run the Slack-driven orchestrator daemon (poll #cold-email-control, route via Anthropic tool-use)")
    p.add_argument("--watch-replies", action="store_true",
                    help="run the reply-handling daemon (poll Smartlead inbox, route through reply squad, ping Slack)")
    p.add_argument("--once", action="store_true",
                    help="with --watch-replies, single pass then exit (testing)")
    p.add_argument("--interval", type=int, default=60, help="polling interval (seconds)")
    p.add_argument("--max-replies", type=int, default=None,
                    help="with --watch-replies, cap N replies per tick (safety for first live tests)")

    args = p.parse_args()

    if args.smoke:
        return asyncio.run(smoke())

    if args.stats:
        from orchestrator.stats import print_stats
        return print_stats()

    if args.slack_agent:
        from orchestrator.slack_agent import watch as _slack_watch
        channel = os.environ.get("SLACK_CONTROL_CHANNEL", "#cold-email-control")
        return asyncio.run(_slack_watch(channel, interval=8, once=False))

    if args.watch_replies:
        from orchestrator.reply_loop import watch as _watch
        offer = args.offer or "power-partner"
        brief = REPO_ROOT / "data" / "campaigns" / "brief.md"
        voice = REPO_ROOT / "campaigns" / offer / "voice.md"
        return asyncio.run(_watch(str(brief), str(voice), args.interval, args.once,
                                    args.max_replies, angle=offer))

    # pilot path — must have an offer + a lead source
    sources = [args.leads, args.from_prospect_saved, args.from_prospect_filters, args.prospect_nl]
    if sum(1 for s in sources if s) != 1:
        p.error("provide exactly ONE of: --leads, --from-prospect-saved, "
                 "--from-prospect-filters, --prospect-nl")
    if not args.offer:
        p.error("--offer is required for a pilot run")

    # Resolve leads
    if args.leads:
        from tools.lead_loader import load_leads
        leads = load_leads(Path(args.leads))
        source_label = f"csv:{Path(args.leads).name}"
    else:
        from tools.lead_loader import load_leads_from_prospect
        if args.from_prospect_saved:
            leads = load_leads_from_prospect(saved_search_id=args.from_prospect_saved,
                                               max_n=args.max or 20, max_fetch=args.max_fetch)
            source_label = f"prospect-saved:{args.from_prospect_saved}"
        elif args.from_prospect_filters:
            filters = json.loads(Path(args.from_prospect_filters).read_text())
            leads = load_leads_from_prospect(filters=filters,
                                               max_n=args.max or 20, max_fetch=args.max_fetch)
            source_label = f"prospect-filters:{Path(args.from_prospect_filters).name}"
        else:
            from tools.prospect_filters import nl_to_filters
            filters = nl_to_filters(args.prospect_nl)
            print(f"[nl→filters] {filters}", flush=True)
            leads = load_leads_from_prospect(filters=filters,
                                               max_n=args.max or 20, max_fetch=args.max_fetch)
            source_label = f"prospect-nl:{args.prospect_nl[:40]}"

    if args.max:
        leads = leads[:args.max]

    return asyncio.run(run_pilot(args.offer, leads,
                                    niche=args.niche, variant=args.variant,
                                    campaign_name_override=args.campaign_name,
                                    source_label=source_label, dry_run=args.dry_run))

    return asyncio.run(run_pilot(args.angle, Path(args.leads), max_leads=args.max))


if __name__ == "__main__":
    sys.exit(cli())
