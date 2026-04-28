"""Supervisor daemon — the accountability layer.

Loops every N seconds:
  1. Picks up pending tasks → spawns worker subprocesses (top-level only;
     subagent children are spawned by their parent worker via spawner.py).
  2. Detects stalled running tasks → escalates via Slack.
  3. Detects completed/failed tasks → posts the result in the originating
     thread (idempotent via posted_completion flag).
  4. Writes its own heartbeat row each loop so a meta-watcher can detect
     supervisor-process wedges.

Run:
    python -m orchestrator.supervisor [--interval 15]
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import os
import subprocess
import sys
import traceback
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from tools.slack_notify import SlackClient
from tools.task_ledger import TaskLedger, SUPERVISOR_HEARTBEAT_ID


PYTHON = str(REPO_ROOT / ".venv" / "bin" / "python")
WORKER_LOG_DIR = REPO_ROOT / "logs" / "workers"

# Track worker subprocesses we've spawned at the top level so we can detect
# zombie children. Workers spawned by a parent worker (subagents) are not
# tracked here — their parent is responsible for them.
_TOP_LEVEL_PROCS: dict[str, subprocess.Popen] = {}


def _say(msg: str) -> None:
    print(f"[supervisor] {msg}", flush=True)


def _spawn_worker(task_id: str) -> subprocess.Popen:
    WORKER_LOG_DIR.mkdir(parents=True, exist_ok=True)
    log = (WORKER_LOG_DIR / f"{task_id}.log").open("a", encoding="utf-8")
    return subprocess.Popen(
        [PYTHON, "-u", "-m", "orchestrator.worker", "--task-id", task_id],
        stdout=log, stderr=subprocess.STDOUT,
        cwd=str(REPO_ROOT),
    )


async def pick_up_pending(ledger: TaskLedger) -> int:
    """Spawn a worker for every top-level pending task. Subagent pending
    tasks are picked up by their parent worker via spawn_and_wait."""
    n = 0
    for t in ledger.list_pending():
        # Skip subagent children — their parent is supposed to spawn them
        if t.get("parent_task"):
            continue
        if t["id"] in _TOP_LEVEL_PROCS and \
                _TOP_LEVEL_PROCS[t["id"]].poll() is None:
            continue  # already running
        _say(f"spawning worker for {t['id']} → {t.get('intent', '')[:80]}")
        proc = _spawn_worker(t["id"])
        _TOP_LEVEL_PROCS[t["id"]] = proc
        ledger.append_event(t["id"], "supervisor_spawned",
                              {"pid": proc.pid})
        n += 1
    return n


async def detect_stalls(ledger: TaskLedger, sc: SlackClient) -> int:
    """Soft-stall after 5 min, hard-stall (retry once) after 15 min."""
    soft_threshold = 300
    hard_threshold = 900

    soft = ledger.list_stalled(idle_seconds=soft_threshold)
    n = 0
    for t in soft:
        # Skip if we've already escalated soft stall in last 5 min
        recent = ledger.recent_events(t["id"], limit=5)
        if any(e["event"] == "soft_stall_warned" for e in recent):
            # Check if it's been long enough to hard-escalate
            last_warn = next((e for e in recent
                                  if e["event"] == "soft_stall_warned"), None)
            if last_warn:
                last_dt = dt.datetime.fromisoformat(last_warn["ts"].rstrip("Z"))
                if (dt.datetime.now(dt.UTC).replace(tzinfo=None) - last_dt
                        ).total_seconds() < hard_threshold - soft_threshold:
                    continue

        # Check if it's actually past the hard threshold
        last_progress = dt.datetime.fromisoformat(
            (t.get("last_progress_at") or t["created_at"]).rstrip("Z"))
        idle = (dt.datetime.now(dt.UTC).replace(tzinfo=None)
                  - last_progress).total_seconds()

        if idle >= hard_threshold:
            await _hard_escalate(ledger, sc, t)
        else:
            await _soft_escalate(ledger, sc, t, idle)
        n += 1
    return n


async def _soft_escalate(ledger: TaskLedger, sc: SlackClient,
                              t: dict, idle: float) -> None:
    intent = (t.get("intent") or "")[:120]
    stage = t.get("stage") or "?"
    msg = (f":warning: still working on `{intent}` — "
            f"stage `{stage}`, no progress for {int(idle/60)}m. "
            f"Task `{t['id']}`. Worker may be slow or wedged; "
            f"will hard-escalate at 15m.")
    if t.get("channel"):
        try:
            sc.post(t["channel"], msg, thread_ts=t.get("thread_ts"))
        except Exception as e:
            _say(f"slack post failed for soft escalate: {e}")
    ledger.append_event(t["id"], "soft_stall_warned",
                          {"idle_seconds": int(idle)})


async def _hard_escalate(ledger: TaskLedger, sc: SlackClient,
                              t: dict) -> None:
    intent = (t.get("intent") or "")[:120]
    attempts = t.get("attempts") or 0
    # Kill the worker if we have a handle
    if t["id"] in _TOP_LEVEL_PROCS:
        try:
            _TOP_LEVEL_PROCS[t["id"]].terminate()
        except Exception:
            pass

    if attempts < 2:
        # Mark failed, then immediately reset to pending for one retry
        ledger.mark_failed(t["id"],
                              f"stalled past 15min (attempt {attempts})")
        ledger.reset_to_pending_for_retry(t["id"])
        msg = (f":x: `{intent}` stalled — *retrying once* (attempt "
                f"{attempts + 1}). Task `{t['id']}`.")
    else:
        ledger.mark_failed(t["id"],
                              f"stalled past 15min after {attempts} attempts",
                              permanent=True)
        msg = (f":rotating_light: `{intent}` *failed permanently* after "
                f"{attempts} attempts. <@{os.environ.get('JUSTIN_SLACK_USER_ID', '')}> "
                f"please check task `{t['id']}` manually.")
    if t.get("channel"):
        try:
            sc.post(t["channel"], msg, thread_ts=t.get("thread_ts"))
        except Exception as e:
            _say(f"slack post failed for hard escalate: {e}")
    ledger.append_event(t["id"], "hard_escalated",
                          {"attempts": attempts})


async def post_completions(ledger: TaskLedger, sc: SlackClient) -> int:
    """Post a final ✅ / ❌ for any completed top-level task that hasn't
    been acknowledged in Slack yet."""
    n = 0
    for t in ledger.list_completed_unposted():
        try:
            await _post_completion(ledger, sc, t)
        except Exception as e:
            _say(f"post_completion failed for {t['id']}: {e}")
        n += 1
    return n


async def _post_completion(ledger: TaskLedger, sc: SlackClient,
                                t: dict) -> None:
    intent = (t.get("intent") or "")[:120]
    status = t["status"]
    if status == "completed":
        result = t.get("result") or {}
        # Workers usually post their own success message (e.g., CSV upload).
        # Supervisor's role is the safety net: if the worker didn't post,
        # post a generic completion. We detect via task_events: if the
        # worker emitted slack_uploaded or similar, skip the generic post.
        recent = ledger.recent_events(t["id"], limit=10)
        worker_posted = any(e["event"] in ("slack_uploaded",
                                                "worker_slack_posted")
                                for e in recent)
        if not worker_posted and t.get("channel"):
            sec = result.get("total_seconds") or result.get("research_seconds")
            line = (f":white_check_mark: `{intent}` complete. "
                     f"Task `{t['id']}`")
            if sec:
                line += f" · {int(sec)}s"
            try:
                sc.post(t["channel"], line, thread_ts=t.get("thread_ts"))
            except Exception:
                pass
    elif status in ("failed", "permanently_failed"):
        err = (t.get("error") or "")[:300]
        permanent = status == "permanently_failed"
        if t.get("channel"):
            try:
                sc.post(t["channel"],
                          f"{':rotating_light:' if permanent else ':x:'} "
                          f"`{intent}` "
                          f"{'failed permanently' if permanent else 'failed'}: "
                          f"{err}\nTask `{t['id']}`",
                          thread_ts=t.get("thread_ts"))
            except Exception:
                pass
    ledger.mark_completion_posted(t["id"])


async def reap_zombies() -> None:
    """Clean up finished subprocess handles to prevent zombies."""
    for tid, proc in list(_TOP_LEVEL_PROCS.items()):
        if proc.poll() is not None:
            del _TOP_LEVEL_PROCS[tid]


async def supervisor_loop(interval: int = 15) -> None:
    ledger = TaskLedger()
    sc = SlackClient()
    _say(f"online; interval={interval}s")
    ledger.append_event(SUPERVISOR_HEARTBEAT_ID, "supervisor_online",
                          {"pid": os.getpid()})

    cycle = 0
    while True:
        cycle += 1
        try:
            spawned = await pick_up_pending(ledger)
            stalls = await detect_stalls(ledger, sc)
            posted = await post_completions(ledger, sc)
            await reap_zombies()
            ledger.append_event(SUPERVISOR_HEARTBEAT_ID, "heartbeat",
                                  {"cycle": cycle,
                                   "spawned": spawned,
                                   "stalls_handled": stalls,
                                   "completions_posted": posted,
                                   "tracked_workers": len(_TOP_LEVEL_PROCS)})
            if spawned or stalls or posted:
                _say(f"cycle {cycle}: spawned={spawned} stalls={stalls} "
                      f"completions={posted} tracked={len(_TOP_LEVEL_PROCS)}")
        except Exception as e:
            tb = traceback.format_exc()
            _say(f"cycle {cycle} error: {e}\n{tb}")
            ledger.append_event(SUPERVISOR_HEARTBEAT_ID, "supervisor_error",
                                  {"error": str(e), "cycle": cycle})
        await asyncio.sleep(interval)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--interval", type=int, default=15)
    a = p.parse_args()
    try:
        asyncio.run(supervisor_loop(a.interval))
    except KeyboardInterrupt:
        _say("interrupted")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
