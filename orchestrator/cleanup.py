"""Cleanup operations against the live Smartlead account.

Today: archive (STOP / DELETE) the existing campaigns so a future click can't
accidentally activate one and send wrong-offer outbound.

Always snapshots first to data/archive/<date>-pre-archive-snapshot.json so the
campaign metadata is recoverable even if the user picks DELETE.

CLI:
  python -m orchestrator.cleanup --archive-existing             (interactive)
  python -m orchestrator.cleanup --archive-existing --yes       (auto-confirm)
  python -m orchestrator.cleanup --archive-existing --dry-run   (no action)
  python -m orchestrator.cleanup --archive-existing --mode DELETE   (permanent)
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from tools.smartlead import SmartleadCLI

ARCHIVE_DIR = REPO_ROOT / "data" / "archive"
# Statuses that are still "active" and worth archiving — COMPLETED is terminal already.
ACTIVE_STATUSES = {"PAUSED", "DRAFTED", "ACTIVE", "STARTED", "INPROGRESS"}


def _today() -> str:
    return dt.date.today().isoformat()


def archive_all(mode: str = "STOP", dry_run: bool = False, yes: bool = False) -> int:
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    cli = SmartleadCLI()

    print("[cleanup] fetching campaigns from Smartlead...", flush=True)
    campaigns = cli.list_campaigns()
    print(f"[cleanup] {len(campaigns)} campaigns total", flush=True)

    # Snapshot first — always, even on dry-run
    snapshot_path = ARCHIVE_DIR / f"{_today()}-pre-archive-snapshot.json"
    snapshot_path.write_text(json.dumps(campaigns, indent=2, default=str))
    print(f"[cleanup] snapshot → {snapshot_path.relative_to(REPO_ROOT)}", flush=True)

    # Filter to candidates
    candidates = [c for c in campaigns if str(c.get("status", "")).upper() in ACTIVE_STATUSES]
    skipped = [c for c in campaigns if str(c.get("status", "")).upper() not in ACTIVE_STATUSES]

    print(f"\n[cleanup] candidates ({len(candidates)} to {mode.upper()}):")
    for c in candidates:
        print(f"  • id={c.get('id')} status={c.get('status')} name={c.get('name')}")
    print(f"\n[cleanup] skipping ({len(skipped)} already-terminal):")
    for c in skipped:
        print(f"  • id={c.get('id')} status={c.get('status')} name={c.get('name')}")

    if dry_run:
        print("\n[cleanup] --dry-run; no action taken.", flush=True)
        return 0

    if not candidates:
        print("\n[cleanup] no candidates to archive.", flush=True)
        return 0

    if not yes:
        prompt = f"\n[cleanup] {mode.upper()} all {len(candidates)} candidates? Type 'yes' to confirm: "
        try:
            answer = input(prompt).strip().lower()
        except EOFError:
            answer = ""
        if answer != "yes":
            print("[cleanup] aborted (no confirmation).", flush=True)
            return 1

    # Execute
    results: list[dict] = []
    for c in candidates:
        cid = c.get("id")
        try:
            cli.archive_campaign(cid, mode=mode)
            results.append({"id": cid, "name": c.get("name"), "from": c.get("status"),
                             "to": mode.upper(), "ok": True})
            print(f"  ✓ {mode.upper()} id={cid} ({c.get('name')})", flush=True)
        except Exception as e:
            results.append({"id": cid, "name": c.get("name"), "ok": False, "error": str(e)})
            print(f"  ✗ {mode.upper()} id={cid} failed: {e}", flush=True)

    result_path = ARCHIVE_DIR / f"{_today()}-archive-result.json"
    result_path.write_text(json.dumps(results, indent=2))
    print(f"\n[cleanup] result → {result_path.relative_to(REPO_ROOT)}", flush=True)

    succeeded = sum(1 for r in results if r["ok"])
    print(f"[cleanup] done. {succeeded}/{len(results)} succeeded.", flush=True)
    return 0 if succeeded == len(results) else 2


def cli_main() -> int:
    p = argparse.ArgumentParser(prog="cold-email-cleanup")
    p.add_argument("--archive-existing", action="store_true",
                    help="archive (STOP or DELETE) all active campaigns")
    p.add_argument("--mode", default="STOP", choices=["STOP", "DELETE"],
                    help="STOP (reversible, default) or DELETE (permanent)")
    p.add_argument("--dry-run", action="store_true", help="snapshot only, no action")
    p.add_argument("--yes", action="store_true", help="skip interactive confirmation")
    args = p.parse_args()

    if args.archive_existing:
        return archive_all(mode=args.mode, dry_run=args.dry_run, yes=args.yes)

    p.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(cli_main())
