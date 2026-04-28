"""Pre-create empty DRAFTED Smartlead campaigns for (niche × offer × variant)
combos so they are ready to receive leads without paying the Strategy-squad
cost again at lead-arrival time.

Usage:
  python -m orchestrator.precreate \
      --niche=recruiters \
      --offers=power-partner,direct-value,capstone \
      --variants=A,B

Per combo:
  1. Resolve campaign name (`<niche>-<offer>-<VARIANT>`).
  2. If brief.md cached at data/campaigns/<name>/brief.md → reuse.
     Else → run Strategy squad with placeholder lead_summary, save brief.
  3. Look up Smartlead campaign by name; if missing → create + save sequence
     template (DRAFTED).
  4. Persist data/campaigns/<name>/precreated.json with the run log.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from squads.smartlead import SmartleadSquad
from squads.smartlead.squad import make_campaign_name
from squads.strategy import StrategySquad


PLACEHOLDER_LEAD_SUMMARY = (
    "Total leads: 0 (pre-creation — no leads attached yet)\n"
    "This brief will be reused for every batch of leads later appended to the\n"
    "campaign, so write to the niche+offer+variant level — not to specific\n"
    "individuals."
)


async def precreate_one(niche: str, offer: str, variant: str,
                         force_brief: bool = False) -> dict:
    name = make_campaign_name(niche, offer, variant)
    brief_dir = REPO_ROOT / "data" / "campaigns" / name
    brief_dir.mkdir(parents=True, exist_ok=True)
    brief_path = brief_dir / "brief.md"

    # 1. Brief — cached or fresh
    if brief_path.exists() and not force_brief:
        brief = brief_path.read_text()
        brief_status = f"reused ({len(brief)} chars)"
    else:
        print(f"  [{name}] Strategy squad generating brief…", flush=True)
        strategy = StrategySquad()
        brief = await strategy.build_brief(offer, PLACEHOLDER_LEAD_SUMMARY,
                                            niche=niche, variant=variant)
        brief_path.write_text(brief)
        brief_status = f"generated ({len(brief)} chars)"

    # 2. Smartlead campaign — create-or-confirm
    sl = SmartleadSquad()
    run_log = sl.precreate_campaign(offer, niche=niche, variant=variant)
    run_log["brief_status"] = brief_status
    run_log["precreated_at"] = dt.datetime.utcnow().isoformat()

    (brief_dir / "precreated.json").write_text(json.dumps(run_log, indent=2))
    return run_log


async def precreate_many(niche: str, offers: list[str], variants: list[str],
                          force_brief: bool = False) -> list[dict]:
    results = []
    for offer in offers:
        for variant in variants:
            print(f"\n=== {make_campaign_name(niche, offer, variant)} ===", flush=True)
            try:
                r = await precreate_one(niche, offer, variant, force_brief=force_brief)
                results.append(r)
                action = "CREATED" if r.get("created_now") else "EXISTS"
                print(f"  ✓ Smartlead campaign {action} (id={r.get('campaign_id')}) "
                       f"— brief {r.get('brief_status')}", flush=True)
            except Exception as e:
                print(f"  ✗ FAILED: {e}", flush=True)
                results.append({"niche": niche, "offer": offer, "variant": variant,
                                  "error": str(e)})
    return results


def main() -> int:
    p = argparse.ArgumentParser(prog="precreate",
                                  description="Pre-create DRAFTED Smartlead campaigns")
    p.add_argument("--niche", required=True, help="niche (e.g. recruiters, home-services)")
    p.add_argument("--offers", required=True,
                    help="comma-separated offer slugs (e.g. power-partner,direct-value,capstone)")
    p.add_argument("--variants", default="A,B",
                    help="comma-separated variants (default A,B)")
    p.add_argument("--force-brief", action="store_true",
                    help="regenerate briefs even if cached")
    args = p.parse_args()

    offers = [o.strip() for o in args.offers.split(",") if o.strip()]
    variants = [v.strip().upper() for v in args.variants.split(",") if v.strip()]

    print(f"Pre-creating {len(offers) * len(variants)} campaign(s) "
           f"for niche={args.niche}: offers={offers} variants={variants}", flush=True)
    results = asyncio.run(precreate_many(args.niche, offers, variants,
                                            force_brief=args.force_brief))

    # summary
    ok = [r for r in results if not r.get("error")]
    fail = [r for r in results if r.get("error")]
    print(f"\n=== precreate summary === {len(ok)} ok, {len(fail)} failed", flush=True)
    for r in ok:
        print(f"  {r.get('campaign_name')} → id={r.get('campaign_id')} "
               f"({'NEW' if r.get('created_now') else 'EXISTING'})")
    for r in fail:
        print(f"  ✗ {r.get('niche')}-{r.get('offer')}-{r.get('variant')}: {r.get('error')}")
    return 0 if not fail else 1


if __name__ == "__main__":
    sys.exit(main())
