"""Subprocess-based subagent spawner.

`spawn_and_wait(parent_id, children_specs, ...)` creates child tasks in the
ledger, launches a worker subprocess for each, polls their status, returns
results when they all settle (completed/failed/cancelled).

Used by orchestrator/worker.py when handling spawn_subagent / council_review
tool calls. The parent worker BLOCKS on `await spawn_and_wait`, so the
ledger's parent row stays in 'running' state with progress updates flowing
in via the callback.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from tools.task_ledger import TaskLedger


PYTHON = str(REPO_ROOT / ".venv" / "bin" / "python")
WORKER_LOG_DIR = REPO_ROOT / "logs" / "workers"


def _spawn_subprocess(task_id: str) -> subprocess.Popen:
    """Launch `python -m orchestrator.worker --task-id <id>` detached enough
    that supervisor death doesn't kill it, but still under the same process
    group so the user can kill the whole tree if needed."""
    WORKER_LOG_DIR.mkdir(parents=True, exist_ok=True)
    log = (WORKER_LOG_DIR / f"{task_id}.log").open("a", encoding="utf-8")
    return subprocess.Popen(
        [PYTHON, "-u", "-m", "orchestrator.worker", "--task-id", task_id],
        stdout=log, stderr=subprocess.STDOUT,
        cwd=str(REPO_ROOT),
        start_new_session=False,  # share group with supervisor
    )


async def spawn_and_wait(parent_id: str,
                              children_specs: list[dict],
                              max_depth: int = 3,
                              timeout_seconds: int = 1800,
                              poll_interval: float = 2.0,
                              progress_cb=None) -> list[dict]:
    """Create child tasks in the ledger, launch worker subprocesses, await
    completion of all of them.

    children_specs: list of {role: str, intent: str, tool_call: dict,
                                deadline_seconds?: int}

    Returns a list of dicts with the same length as children_specs:
        [{"task_id": str, "status": str, "result": dict | None,
          "error": str | None}]
    """
    ledger = TaskLedger()
    child_ids: list[str] = []

    # 1. Create all child task records
    for spec in children_specs:
        cid = ledger.spawn_child(
            parent_id=parent_id,
            role=spec.get("role", "subagent"),
            intent=spec["intent"],
            tool_call=spec["tool_call"],
            deadline_seconds=spec.get("deadline_seconds", 600),
            max_depth=max_depth,
        )
        child_ids.append(cid)

    ledger.append_event(parent_id, "spawned_children",
                          {"count": len(child_ids), "ids": child_ids})

    # 2. Launch a worker subprocess per child
    procs: dict[str, subprocess.Popen] = {}
    for cid in child_ids:
        procs[cid] = _spawn_subprocess(cid)

    # 3. Poll until all children settle (or timeout)
    pending = set(child_ids)
    settled: dict[str, dict] = {}
    start_loop = asyncio.get_event_loop().time()

    while pending:
        if (asyncio.get_event_loop().time() - start_loop) > timeout_seconds:
            # Timeout — mark remaining as failed and SIGTERM their processes
            for cid in list(pending):
                try:
                    procs[cid].terminate()
                except Exception:
                    pass
                ledger.mark_failed(cid, "timeout in spawn_and_wait",
                                       permanent=True)
                settled[cid] = {"task_id": cid, "status": "permanently_failed",
                                  "result": None,
                                  "error": "timeout"}
            break

        await asyncio.sleep(poll_interval)
        for cid in list(pending):
            t = ledger.get(cid)
            if not t:
                continue
            if t["status"] in ("completed", "failed",
                                  "permanently_failed", "cancelled"):
                settled[cid] = {
                    "task_id": cid,
                    "status": t["status"],
                    "result": t.get("result"),
                    "error": t.get("error"),
                    "role": t.get("role"),
                }
                pending.remove(cid)
                if progress_cb:
                    try:
                        progress_cb("subagent_settled",
                                       len(settled), len(child_ids),
                                       f"{cid} → {t['status']}")
                    except Exception:
                        pass

    # 4. Reap subprocesses
    for cid, p in procs.items():
        if p.poll() is None:
            try:
                p.wait(timeout=5)
            except Exception:
                p.kill()

    return [settled.get(cid, {"task_id": cid, "status": "unknown",
                                "result": None, "error": "no settle record"})
              for cid in child_ids]
