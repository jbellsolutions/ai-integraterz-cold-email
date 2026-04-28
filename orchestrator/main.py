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
    progress_cb=None,
) -> dict:
    """Strategy → Research → Copy → Smartlead. Niche-aware, variant-aware, idempotent.

    leads is a pre-loaded list[Lead] — caller decides CSV vs Prospector vs other source.

    Returns a dict (NOT exit code) with the run_log + counts so callers can
    surface real numbers in Slack. Raises on hard failure — caller posts ❌.

    `progress_cb` is an optional callable `cb(stage: str, done: int, total: int,
                                                detail: str)` invoked at milestones
    so long pilots can post progress (e.g., "research 200/698 done").

    Concurrency tunables via env:
      CE2_RESEARCH_PARALLEL (default 30)
      CE2_COPY_PARALLEL     (default 20)
    """
    from squads.smartlead.squad import make_campaign_name
    campaign_name = campaign_name_override or make_campaign_name(niche, offer, variant)
    n = len(leads)

    # Per-pilot structured log — append-only JSONL we can grep when something
    # goes wrong. Path: data/pilots/<campaign>-<ts>.jsonl.
    pilots_dir = REPO_ROOT / "data" / "pilots"
    pilots_dir.mkdir(parents=True, exist_ok=True)
    run_id = dt.datetime.utcnow().strftime("%Y%m%dT%H%M%S")
    pilot_log_path = pilots_dir / f"{campaign_name}-{run_id}.jsonl"

    def _logp(event: str, **kw):
        rec = {"ts": dt.datetime.utcnow().isoformat() + "Z", "event": event,
                "campaign": campaign_name, **kw}
        try:
            with pilot_log_path.open("a") as f:
                f.write(json.dumps(rec) + "\n")
        except Exception:
            pass  # never let logging break the pilot

    def _progress(stage: str, done: int, total: int, detail: str = ""):
        if progress_cb:
            try:
                progress_cb(stage, done, total, detail)
            except Exception as e:
                print(f"[run_pilot] progress_cb failed (suppressed): {e}", flush=True)

    _logp("pilot_start", leads=n, source=source_label, niche=niche,
           offer=offer, variant=variant)
    _say(f"=== Cold Email 2.0 — pilot: {campaign_name} ({n} leads from {source_label}) ===")
    _say(f"=== run_id={run_id}  log={pilot_log_path.relative_to(REPO_ROOT)} ===")
    if not leads:
        _say("no leads to process — exiting")
        _logp("pilot_abort", reason="no_leads")
        return {"campaign_name": campaign_name, "leads_in_this_run": 0, "aborted": True}

    summary = lead_summary(leads)
    _say("--- Lead summary ---\n" + summary)

    if dry_run:
        _say("\n[dry-run] skipping LLM stages and Smartlead writes. Lead loading verified.")
        return {"campaign_name": campaign_name, "leads_in_this_run": 0, "dry_run": True}

    # 1. Strategy squad → brief (cached per <campaign_name>)
    brief_dir = REPO_ROOT / "data" / "campaigns" / campaign_name
    brief_dir.mkdir(parents=True, exist_ok=True)
    brief_path = brief_dir / "brief.md"
    if brief_path.exists():
        _say(f"\nStage 1/4: Strategy — reusing cached brief at {brief_path.relative_to(REPO_ROOT)}")
        brief = brief_path.read_text()
        _logp("strategy_cached", chars=len(brief))
    else:
        _say(f"\nStage 1/4: Strategy (Opus, ~30s) — generating brief for {campaign_name}")
        _logp("strategy_start")
        strategy = StrategySquad()
        brief = await strategy.build_brief(offer, summary, niche=niche, variant=variant)
        brief_path.write_text(brief)
        _say(f"✓ campaign brief → {brief_path.relative_to(REPO_ROOT)} ({len(brief)} chars)")
        _logp("strategy_done", chars=len(brief))

    # 2. Research squad → per-prospect signals (parallel)
    research_parallel = int(os.environ.get("CE2_RESEARCH_PARALLEL", "30"))
    _say(f"\nStage 2/4: Research (Haiku × {n} leads, max_parallel={research_parallel})")
    _logp("research_start", parallel=research_parallel)
    _progress("research", 0, n, f"max_parallel={research_parallel}")
    research = ResearchSquad(brief=brief)
    research_t0 = dt.datetime.utcnow()
    signals = await research.research_batch(leads, max_parallel=research_parallel)
    research_dt = (dt.datetime.utcnow() - research_t0).total_seconds()
    tier_counts: dict[str, int] = {}
    for s in signals:
        tier_counts[s.get("tier", "?")] = tier_counts.get(s.get("tier", "?"), 0) + 1
    tier_str = ", ".join(f"{t}={n}" for t, n in sorted(tier_counts.items()))
    _say(f"  signal tiers: {tier_str}  (took {research_dt:.0f}s, "
          f"{research_dt/max(n,1):.2f}s/lead)")
    _logp("research_done", duration_seconds=round(research_dt, 1),
           tiers=tier_counts)
    _progress("research", n, n, f"{research_dt:.0f}s · {tier_str}")

    # 3. Copy squad → per-prospect sequences
    copy_parallel = int(os.environ.get("CE2_COPY_PARALLEL", "20"))
    _say(f"\nStage 3/4: Copy (Sonnet hook + Haiku body × {n}, max_parallel={copy_parallel})")
    _logp("copy_start", parallel=copy_parallel)
    _progress("copy", 0, n, f"max_parallel={copy_parallel}")
    copy = CopySquad(brief=brief)
    sem = asyncio.Semaphore(copy_parallel)
    copy_done_counter = {"n": 0}
    last_progress = {"t": dt.datetime.utcnow()}

    async def write_one(lead: Lead, signal: dict):
        async with sem:
            try:
                result = await copy.write_one(lead, signal)
            except Exception as e:
                # Never let one prospect's crash kill the batch — record it.
                _logp("copy_lead_error", email=lead.email, error=str(e)[:200])
                return {"sequence": [], "slop_pass": False, "error": str(e)}
            copy_done_counter["n"] += 1
            # Throttled progress: every 25 leads OR every 30 seconds
            now = dt.datetime.utcnow()
            if (copy_done_counter["n"] % 25 == 0 or
                    (now - last_progress["t"]).total_seconds() > 30):
                _progress("copy", copy_done_counter["n"], n, "")
                last_progress["t"] = now
            return result

    copy_t0 = dt.datetime.utcnow()
    emails = await asyncio.gather(*(write_one(lead, sig)
                                       for lead, sig in zip(leads, signals)))
    copy_dt = (dt.datetime.utcnow() - copy_t0).total_seconds()
    slop_pass = sum(1 for e in emails if e.get("slop_pass"))
    _say(f"✓ {slop_pass}/{len(emails)} sequences passed slop critic "
          f"(took {copy_dt:.0f}s, {copy_dt/max(n,1):.2f}s/lead)")
    _logp("copy_done", duration_seconds=round(copy_dt, 1),
           slop_pass=slop_pass, total=len(emails))
    _progress("copy", n, n, f"{copy_dt:.0f}s · {slop_pass}/{n} slop-clean")

    # 3b. HARD FAIL on empty copy. This is the bug Justin called out:
    # "the big list was not personalized at all" — Smartlead substitutes
    # `{{email_1_body}}` literally if custom_fields is empty. Better to abort
    # the whole batch than ship raw template tokens to thousands of leads.
    empty = []
    for lead, email in zip(leads, emails):
        seq = email.get("sequence") or []
        # Need at least step 1 with a non-empty body. Step 2/3 follow same path
        # so if step 1 is missing, the whole prospect is broken.
        step1 = next((s for s in seq if s.get("step") == 1), None)
        if not step1 or not (step1.get("body") or "").strip() \
                or not (step1.get("subject") or "").strip():
            empty.append(lead.email)
    empty_pct = 100.0 * len(empty) / max(n, 1)
    _logp("empty_copy_check", empty=len(empty), total=n,
           empty_pct=round(empty_pct, 1),
           sample_emails=empty[:5])
    if empty:
        msg = (f"Copy squad produced {len(empty)}/{n} ({empty_pct:.1f}%) "
                f"empty sequences. Refusing to upload to Smartlead — would ship "
                f"raw `{{{{email_1_body}}}}` tokens. First 5 broken leads: "
                f"{empty[:5]}. See {pilot_log_path.relative_to(REPO_ROOT)}.")
        _say(f"\n✗ {msg}")
        raise RuntimeError(msg)

    # 4. Smartlead squad → DRAFTED campaign (lookup-or-create + append leads)
    _say(f"\nStage 4/4: Smartlead (lookup-or-create '{campaign_name}', append {n} leads)")
    _logp("smartlead_start")
    _progress("smartlead", 0, n, "uploading...")
    sl = SmartleadSquad()
    leads_with_emails = [
        {"lead": lead.__dict__, "emails": email}
        for lead, email in zip(leads, emails)
    ]
    sl_t0 = dt.datetime.utcnow()
    run_log = sl.build_campaign(offer, leads_with_emails, niche=niche, variant=variant,
                                  campaign_name_override=campaign_name_override)
    sl_dt = (dt.datetime.utcnow() - sl_t0).total_seconds()
    _logp("smartlead_done", duration_seconds=round(sl_dt, 1),
           campaign_id=run_log.get("campaign_id"),
           created_now=run_log.get("created_now"))
    _progress("smartlead", n, n, f"{sl_dt:.0f}s")
    _say("--- Smartlead campaign ---\n" + json.dumps(run_log, indent=2))

    total_dt = (dt.datetime.utcnow() - research_t0).total_seconds()
    _say(
        f"\n=== Pilot complete ({total_dt:.0f}s total) ===\n"
        f"Campaign '{campaign_name}' "
        f"{'CREATED' if run_log.get('created_now') else 'APPENDED-TO'} in DRAFTED state.\n"
        f"  Research: {research_dt:.0f}s · Copy: {copy_dt:.0f}s · Smartlead: {sl_dt:.0f}s\n"
        f"  Empty copies: 0/{n} (hard-fail check passed)\n"
        f"Inspect data/emails/*.json + Smartlead UI before resuming.\n"
    )
    _logp("pilot_done", total_seconds=round(total_dt, 1))
    return {**run_log, "research_seconds": round(research_dt, 1),
            "copy_seconds": round(copy_dt, 1),
            "smartlead_seconds": round(sl_dt, 1),
            "total_seconds": round(total_dt, 1),
            "slop_pass": slop_pass,
            "pilot_log": str(pilot_log_path.relative_to(REPO_ROOT))}


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

    asyncio.run(run_pilot(args.offer, leads,
                              niche=args.niche, variant=args.variant,
                              campaign_name_override=args.campaign_name,
                              source_label=source_label, dry_run=args.dry_run))
    return 0


if __name__ == "__main__":
    sys.exit(cli())
