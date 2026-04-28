"""SQLite-backed durable task ledger.

Every long-running user request becomes a row here. Concierge writes; worker
updates progress; supervisor reads to escalate / complete / cancel. Survives
all process restarts.

Schema lives at data/tasks.db. WAL mode so concierge + supervisor + workers
can all read/write concurrently without locking. Append-only audit log in
task_events; full transcripts (LLM messages + tool calls) at
data/tasks/transcripts/<task_id>.jsonl.

The ledger is the source of truth for "is this work done." If it's not in
the ledger, it didn't happen.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = REPO_ROOT / "data" / "tasks.db"
TRANSCRIPTS_DIR = REPO_ROOT / "data" / "tasks" / "transcripts"


SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id                  TEXT PRIMARY KEY,
    created_at          TEXT NOT NULL,
    created_by          TEXT,
    channel             TEXT,
    thread_ts           TEXT,
    intent              TEXT NOT NULL,
    plan                TEXT,
    tool_call           TEXT,
    status              TEXT NOT NULL,
    stage               TEXT,
    progress            REAL,
    deadline_at         TEXT,
    last_progress_at    TEXT,
    started_at          TEXT,
    completed_at        TEXT,
    attempts            INTEGER NOT NULL DEFAULT 0,
    result              TEXT,
    error               TEXT,
    parent_task         TEXT,
    role                TEXT,
    depth               INTEGER NOT NULL DEFAULT 0,
    posted_completion   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_thread ON tasks(thread_ts);
CREATE INDEX IF NOT EXISTS idx_parent ON tasks(parent_task);
CREATE INDEX IF NOT EXISTS idx_deadline ON tasks(deadline_at);

CREATE TABLE IF NOT EXISTS task_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT NOT NULL,
    ts          TEXT NOT NULL,
    event       TEXT NOT NULL,
    detail      TEXT
);
CREATE INDEX IF NOT EXISTS idx_task_events_task ON task_events(task_id, id);
CREATE INDEX IF NOT EXISTS idx_task_events_event ON task_events(event, id);
"""

# Sentinel task_id used by the supervisor for its own heartbeat events.
SUPERVISOR_HEARTBEAT_ID = "__supervisor__"


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


def _ulid() -> str:
    """ULID-ish: timestamp-prefixed so IDs sort chronologically."""
    return f"task-{int(time.time() * 1000):x}-{uuid.uuid4().hex[:8]}"


class TaskLedger:
    """Thread-safe ledger. Each call opens a short-lived connection so that
    multiple processes can hold their own handles without lock contention."""

    def __init__(self, db_path: Path | str = DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(str(self.db_path), timeout=30.0,
                              isolation_level=None)  # autocommit
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA busy_timeout=30000")
        return c

    def _init_schema(self) -> None:
        c = self._conn()
        try:
            c.executescript(SCHEMA)
        finally:
            c.close()

    # ---------------------------------------------------------------- CRUD

    def create_task(self, intent: str, *, channel: str | None = None,
                       thread_ts: str | None = None, created_by: str | None = None,
                       tool_call: dict | None = None,
                       deadline_seconds: int = 1800,
                       plan: str | None = None,
                       parent_task: str | None = None,
                       role: str | None = None) -> str:
        """Create a pending task. Returns its id."""
        task_id = _ulid()
        deadline_at = (dt.datetime.now(dt.UTC) +
                          dt.timedelta(seconds=deadline_seconds)).isoformat()
        depth = 0
        if parent_task:
            parent = self.get(parent_task)
            if parent:
                depth = (parent.get("depth") or 0) + 1
        c = self._conn()
        try:
            c.execute("""
                INSERT INTO tasks (id, created_at, created_by, channel, thread_ts,
                                    intent, plan, tool_call, status, deadline_at,
                                    last_progress_at, parent_task, role, depth)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)
            """, (task_id, _now(), created_by, channel, thread_ts, intent, plan,
                   json.dumps(tool_call) if tool_call else None,
                   deadline_at, _now(), parent_task, role, depth))
            c.execute("""
                INSERT INTO task_events (task_id, ts, event, detail)
                VALUES (?, ?, 'created', ?)
            """, (task_id, _now(),
                   json.dumps({"intent": intent, "tool_call": tool_call,
                               "parent": parent_task, "depth": depth})))
        finally:
            c.close()
        return task_id

    def spawn_child(self, parent_id: str, role: str, intent: str,
                       tool_call: dict, deadline_seconds: int = 600,
                       max_depth: int = 3) -> str:
        """Create a child task. Refuses beyond max_depth.

        Returns the new child task id. Raises RuntimeError if depth ceiling
        exceeded — the caller (concierge or another worker) gets a clean
        signal that recursion was refused so it can adapt.
        """
        parent = self.get(parent_id)
        if not parent:
            raise ValueError(f"unknown parent_task {parent_id!r}")
        if (parent.get("depth") or 0) + 1 > max_depth:
            raise RuntimeError(
                f"depth_exceeded: parent depth={parent['depth']} "
                f"max_depth={max_depth}"
            )
        return self.create_task(
            intent=intent,
            channel=parent.get("channel"),
            thread_ts=parent.get("thread_ts"),
            created_by=parent.get("created_by"),
            tool_call=tool_call,
            deadline_seconds=deadline_seconds,
            parent_task=parent_id,
            role=role,
        )

    def get(self, task_id: str) -> dict | None:
        c = self._conn()
        try:
            r = c.execute("SELECT * FROM tasks WHERE id = ?",
                            (task_id,)).fetchone()
            return self._row_to_dict(r) if r else None
        finally:
            c.close()

    def list_running(self) -> list[dict]:
        return self._list_where("status = 'running'")

    def list_pending(self) -> list[dict]:
        return self._list_where("status = 'pending' ORDER BY created_at ASC")

    def list_stalled(self, idle_seconds: int = 300) -> list[dict]:
        cutoff = (dt.datetime.now(dt.UTC) -
                    dt.timedelta(seconds=idle_seconds)).isoformat()
        return self._list_where(
            "status = 'running' AND last_progress_at < ?",
            (cutoff,))

    def list_by_thread(self, thread_ts: str) -> list[dict]:
        return self._list_where("thread_ts = ? ORDER BY created_at DESC",
                                  (thread_ts,))

    def list_completed_unposted(self) -> list[dict]:
        """Tasks that finished but the supervisor hasn't yet announced.
        Excludes child tasks (parent_task IS NOT NULL) — those report up
        to the parent, not to Slack."""
        return self._list_where(
            "status IN ('completed', 'failed', 'permanently_failed') "
            "AND posted_completion = 0 AND parent_task IS NULL "
            "ORDER BY completed_at ASC")

    def descendants(self, root_id: str) -> list[dict]:
        """BFS down the parent_task tree."""
        out: list[dict] = []
        queue = [root_id]
        seen: set[str] = set()
        while queue:
            current = queue.pop(0)
            if current in seen:
                continue
            seen.add(current)
            children = self._list_where("parent_task = ?", (current,))
            out.extend(children)
            queue.extend(c["id"] for c in children)
        return out

    def ancestors(self, leaf_id: str) -> list[dict]:
        """Walk up parent_task chain."""
        out: list[dict] = []
        cur = self.get(leaf_id)
        while cur and cur.get("parent_task"):
            parent = self.get(cur["parent_task"])
            if not parent:
                break
            out.append(parent)
            cur = parent
        return out

    def tree_status(self, root_id: str) -> dict:
        descendants = self.descendants(root_id)
        root = self.get(root_id)
        all_tasks = ([root] if root else []) + descendants
        counts: dict[str, int] = {}
        for t in all_tasks:
            s = t.get("status") or "?"
            counts[s] = counts.get(s, 0) + 1
        return {
            "root_id": root_id,
            "total": len(all_tasks),
            "status_counts": counts,
            "max_depth": max((t.get("depth", 0) for t in all_tasks),
                                default=0),
        }

    # -------------------------------------------------------------- mutate

    def mark_running(self, task_id: str) -> None:
        c = self._conn()
        try:
            c.execute("""
                UPDATE tasks SET status = 'running',
                                  started_at = COALESCE(started_at, ?),
                                  last_progress_at = ?,
                                  attempts = attempts + 1
                WHERE id = ?
            """, (_now(), _now(), task_id))
            c.execute("""INSERT INTO task_events (task_id, ts, event, detail)
                          VALUES (?, ?, 'started', NULL)""",
                        (task_id, _now()))
        finally:
            c.close()

    def update_progress(self, task_id: str, *, stage: str | None = None,
                          progress: float | None = None,
                          touch_heartbeat: bool = True) -> None:
        sets = []
        params: list[Any] = []
        if stage is not None:
            sets.append("stage = ?")
            params.append(stage)
        if progress is not None:
            sets.append("progress = ?")
            params.append(progress)
        if touch_heartbeat:
            sets.append("last_progress_at = ?")
            params.append(_now())
        if not sets:
            return
        params.append(task_id)
        c = self._conn()
        try:
            c.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?",
                        params)
        finally:
            c.close()

    def mark_completed(self, task_id: str, result: dict) -> None:
        c = self._conn()
        try:
            c.execute("""
                UPDATE tasks SET status = 'completed',
                                  completed_at = ?, result = ?,
                                  progress = 1.0,
                                  last_progress_at = ?
                WHERE id = ?
            """, (_now(), json.dumps(result, default=str), _now(), task_id))
            c.execute("""INSERT INTO task_events (task_id, ts, event, detail)
                          VALUES (?, ?, 'completed', ?)""",
                        (task_id, _now(),
                          json.dumps({"keys": list(result.keys())[:10]})))
        finally:
            c.close()

    def mark_failed(self, task_id: str, error: str,
                       permanent: bool = False) -> None:
        status = "permanently_failed" if permanent else "failed"
        c = self._conn()
        try:
            c.execute("""
                UPDATE tasks SET status = ?, completed_at = ?, error = ?,
                                  last_progress_at = ?
                WHERE id = ?
            """, (status, _now(), error[:2000], _now(), task_id))
            c.execute("""INSERT INTO task_events (task_id, ts, event, detail)
                          VALUES (?, ?, 'failed', ?)""",
                        (task_id, _now(),
                          json.dumps({"error": error[:500],
                                       "permanent": permanent})))
        finally:
            c.close()

    def mark_cancelled(self, task_id: str) -> None:
        c = self._conn()
        try:
            c.execute("""
                UPDATE tasks SET status = 'cancelled', completed_at = ?,
                                  last_progress_at = ?
                WHERE id = ? AND status NOT IN ('completed', 'permanently_failed')
            """, (_now(), _now(), task_id))
            c.execute("""INSERT INTO task_events (task_id, ts, event, detail)
                          VALUES (?, ?, 'cancelled', NULL)""",
                        (task_id, _now()))
        finally:
            c.close()

    def mark_completion_posted(self, task_id: str) -> None:
        c = self._conn()
        try:
            c.execute("UPDATE tasks SET posted_completion = 1 WHERE id = ?",
                        (task_id,))
        finally:
            c.close()

    def reset_to_pending_for_retry(self, task_id: str) -> None:
        """Move a failed task back to pending so the supervisor re-spawns
        a worker. Used in the retry-once path. Records the retry event."""
        c = self._conn()
        try:
            c.execute("""
                UPDATE tasks SET status = 'pending', started_at = NULL,
                                  error = NULL, last_progress_at = ?
                WHERE id = ?
            """, (_now(), task_id))
            c.execute("""INSERT INTO task_events (task_id, ts, event, detail)
                          VALUES (?, ?, 'retried', NULL)""",
                        (task_id, _now()))
        finally:
            c.close()

    def append_event(self, task_id: str, event: str,
                       detail: dict | None = None) -> None:
        c = self._conn()
        try:
            c.execute("""INSERT INTO task_events (task_id, ts, event, detail)
                          VALUES (?, ?, ?, ?)""",
                        (task_id, _now(), event,
                          json.dumps(detail) if detail is not None else None))
        finally:
            c.close()

    def recent_events(self, task_id: str, limit: int = 20) -> list[dict]:
        c = self._conn()
        try:
            rows = c.execute("""
                SELECT ts, event, detail FROM task_events
                WHERE task_id = ? ORDER BY id DESC LIMIT ?
            """, (task_id, limit)).fetchall()
            return [{"ts": r["ts"], "event": r["event"],
                       "detail": json.loads(r["detail"]) if r["detail"] else None}
                       for r in rows]
        finally:
            c.close()

    def append_transcript(self, task_id: str, role: str, content: Any) -> None:
        """Append a row to data/tasks/transcripts/<task_id>.jsonl — full
        record of every message and tool result the worker handled."""
        try:
            f = TRANSCRIPTS_DIR / f"{task_id}.jsonl"
            with f.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"ts": _now(), "role": role,
                                       "content": content},
                                       default=str) + "\n")
        except Exception:
            pass  # transcript is best-effort; never break the task

    # ----------------------------------------------------------- internal

    def _list_where(self, where: str,
                       params: tuple = ()) -> list[dict]:
        c = self._conn()
        try:
            rows = c.execute(
                f"SELECT * FROM tasks WHERE {where}",
                params,
            ).fetchall()
            return [self._row_to_dict(r) for r in rows]
        finally:
            c.close()

    @staticmethod
    def _row_to_dict(r: sqlite3.Row) -> dict:
        d = dict(r)
        if d.get("tool_call"):
            try:
                d["tool_call"] = json.loads(d["tool_call"])
            except Exception:
                pass
        if d.get("result"):
            try:
                d["result"] = json.loads(d["result"])
            except Exception:
                pass
        return d


# Module-level lazy singleton for convenience
_LEDGER: TaskLedger | None = None


def get_ledger() -> TaskLedger:
    global _LEDGER
    if _LEDGER is None:
        _LEDGER = TaskLedger()
    return _LEDGER
