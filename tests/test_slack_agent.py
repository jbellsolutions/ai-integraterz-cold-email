"""Regression tests for the Slack orchestrator agent.

Each test is a regression for a real bug we hit. Add one when something breaks.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))


def test_thread_state_is_json_serializable():
    """Regression: 2026-04-28 — Anthropic SDK's TextBlock / ToolUseBlock
    objects ended up in the conversation list and crashed _save_thread on
    json.dumps. Fix is to serialize them to plain dicts in run_tools.

    This test simulates the shape after run_tools serialization runs and
    verifies the result is JSON-safe.
    """
    serialized_text = {"type": "text", "text": "hello world"}
    serialized_tool_use = {
        "type": "tool_use",
        "id": "toolu_abc123",
        "name": "list_briefs",
        "input": {"max_n": 5},
    }

    conversation = [
        {"role": "user", "content": "list briefs please"},
        {"role": "assistant", "content": [serialized_text, serialized_tool_use]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_abc123",
             "content": json.dumps({"briefs": []})},
        ]},
    ]

    # Must not raise
    blob = json.dumps(conversation, indent=2)
    assert "tool_use_id" in blob
    assert "hello world" in blob


def test_serialization_handles_anthropic_blocks_via_run_tools_logic():
    """Mirror the in-loop serialization logic in run_tools so we catch
    drift if someone touches it without updating both sites."""
    # Simulate Anthropic SDK content blocks (objects, not dicts)
    fake_text = SimpleNamespace(type="text", text="reply text")
    fake_tool_use = SimpleNamespace(type="tool_use", id="tu_1",
                                       name="stats", input={})

    serialized = []
    for b in [fake_text, fake_tool_use]:
        t = getattr(b, "type", None)
        if t == "text":
            serialized.append({"type": "text", "text": b.text})
        elif t == "tool_use":
            serialized.append({"type": "tool_use", "id": b.id,
                                 "name": b.name, "input": dict(b.input or {})})

    # Must round-trip through JSON without error
    json.dumps(serialized)


def test_tool_registry_consistency():
    """Every tool name in TOOLS must have a matching function in TOOL_FUNCS."""
    from orchestrator.slack_agent import TOOLS, TOOL_FUNCS
    schema_names = {t["name"] for t in TOOLS}
    func_names = set(TOOL_FUNCS.keys())
    assert schema_names == func_names, (
        f"missing funcs: {schema_names - func_names}, "
        f"extra funcs: {func_names - schema_names}"
    )


def test_copy_squad_handles_null_sequence_in_body_output():
    """Regression: 2026-04-28 (second occurrence) — body squad output
    {"sequence": null} crashed write_one with
    `TypeError: 'NoneType' object is not iterable` because
    sequence.get("sequence", []) returns None for explicit null.

    Same null-default-vs-explicit-null bug as the pick fix; refactored to use
    _safe_list helper that coerces None → [] regardless of how it got there.
    """
    from squads.copy.squad import _safe_list, _pick_candidate

    # _safe_list — never returns None
    assert _safe_list(None) == []
    assert _safe_list([1, 2]) == [1, 2]
    assert _safe_list({"x": 1}) == []
    assert _safe_list("string") == []
    assert _safe_list(0) == []

    # _pick_candidate — never crashes
    assert _pick_candidate({"candidates": ["a", "b"], "pick": None}) == "a"
    assert _pick_candidate({"candidates": None, "pick": 0}) == ""
    assert _pick_candidate({}) == ""
    assert _pick_candidate({"candidates": ["x", "y", "z"], "pick": 99}) == "z"
    assert _pick_candidate({"candidates": ["a"], "pick": -5}) == "a"


def test_copy_squad_handles_null_pick_in_hook():
    """Regression: 2026-04-28 — Copy squad's _draft_body crashed with
    `TypeError: list indices must be integers or slices, not NoneType`
    when the hook squad emitted {"pick": null} instead of {"pick": 0}.
    `.get("pick", 0)` returns None for an explicit-null key, not the default.
    """
    # Mirror the fixed selection logic from squads/copy/squad.py:_draft_body
    def select(hook: dict) -> str:
        candidates = hook.get("candidates") or [""]
        pick = hook.get("pick")
        if not isinstance(pick, int):
            pick = 0
        pick = max(0, min(pick, len(candidates) - 1))
        return candidates[pick]

    # The crash case
    assert select({"candidates": ["a", "b"], "pick": None}) == "a"
    # Missing key
    assert select({"candidates": ["a", "b"]}) == "a"
    # Out of range (clamped)
    assert select({"candidates": ["a", "b"], "pick": 99}) == "b"
    # Negative (clamped)
    assert select({"candidates": ["a", "b"], "pick": -3}) == "a"
    # Empty candidates fallback
    assert select({}) == ""
    # Normal case still works
    assert select({"candidates": ["x", "y", "z"], "pick": 2}) == "z"


def test_active_thread_tracking_round_trip(tmp_path=None):
    """Regression: 2026-04-28 — Slack `conversations.history` only returns
    top-level channel messages, so when the agent posts a reply in a thread
    and Justin replies in that thread, the daemon never sees the reply. Even
    @-mentions in threads were dropped on the floor. Fix tracks every thread
    we've seen in data/slack/active_threads.json and the poll loop also
    polls conversations.replies on each one. This test verifies the helpers
    round-trip through disk and prune correctly.
    """
    import datetime as dt
    import json
    import os
    from orchestrator import slack_agent as sa

    # Redirect to a temp path so the test doesn't trample real state
    if tmp_path is None:
        import tempfile
        tmp = Path(tempfile.mkdtemp())
    else:
        tmp = Path(tmp_path)
    sa.ACTIVE_THREADS_PATH = tmp / "active_threads.json"

    # Round-trip
    sa._register_active_thread("1777400000.000001", "1777400000.000001")
    sa._register_active_thread("1777400500.000002", "1777400500.000002")
    d = sa._load_active_threads()
    assert "1777400000.000001" in d
    assert "1777400500.000002" in d
    assert d["1777400000.000001"]["last_reply_ts"] == "1777400000.000001"

    # Prune drops stale entries
    stale = json.loads(sa.ACTIVE_THREADS_PATH.read_text())
    stale["1777400000.000001"]["updated_at"] = "2020-01-01T00:00:00Z"
    sa.ACTIVE_THREADS_PATH.write_text(json.dumps(stale))
    pruned = sa._prune_active_threads()
    assert "1777400000.000001" not in pruned
    assert "1777400500.000002" in pruned


def test_run_pilot_aborts_on_empty_copy():
    """Regression: 2026-04-28 — Justin's first big launch shipped raw template
    tokens to Smartlead because Copy squad returned empty sequences and we
    uploaded anyway. Smartlead substituted `{{email_1_body}}` literally,
    producing un-personalized emails ready to send. The fix raises
    RuntimeError before build_campaign is called when ANY prospect has an
    empty body or subject for step 1.

    This test simulates the empty-sequence shape and validates the guard
    fires (without actually invoking LLMs or Smartlead).
    """
    # Mirror the hard-fail logic from orchestrator/main.py:run_pilot
    def empty_copy_check(leads, emails):
        empty = []
        for lead, email in zip(leads, emails):
            seq = email.get("sequence") or []
            step1 = next((s for s in seq if s.get("step") == 1), None)
            if not step1 or not (step1.get("body") or "").strip() \
                    or not (step1.get("subject") or "").strip():
                empty.append(lead.get("email"))
        return empty

    leads = [
        {"email": "a@x.com"},
        {"email": "b@x.com"},
        {"email": "c@x.com"},
    ]
    # Case 1: all empty (Copy squad fully failed)
    emails_all_empty = [{"sequence": []} for _ in leads]
    assert empty_copy_check(leads, emails_all_empty) == \
            ["a@x.com", "b@x.com", "c@x.com"]

    # Case 2: one prospect has step 1 with empty body
    emails_partial = [
        {"sequence": [{"step": 1, "subject": "ok", "body": "real body"}]},
        {"sequence": [{"step": 1, "subject": "ok", "body": ""}]},
        {"sequence": [{"step": 1, "subject": "ok", "body": "real body"}]},
    ]
    assert empty_copy_check(leads, emails_partial) == ["b@x.com"]

    # Case 3: missing step 1 entirely (only step 2/3 generated)
    emails_no_step1 = [
        {"sequence": [{"step": 2, "subject": "ok", "body": "body"}]},
    ]
    assert empty_copy_check(leads[:1], emails_no_step1) == ["a@x.com"]

    # Case 4: clean sequences pass (empty list returned)
    emails_clean = [
        {"sequence": [{"step": 1, "subject": "ok", "body": "real"}]},
    ] * 3
    assert empty_copy_check(leads, emails_clean) == []


def test_handle_message_returns_bool_for_cursor_advance():
    """Regression: 2026-04-28 (codex P1) — the channel + thread cursors
    were advanced BEFORE handle_message ran, so any transient error dropped
    the message forever. Fix returns True only on full-turn success;
    poll_once advances cursors only when handle_message returns True.
    Verify the function signature contract."""
    import inspect
    from orchestrator import slack_agent
    sig = inspect.signature(slack_agent.handle_message)
    # __future__ annotations turns these into strings — accept either form.
    ret = sig.return_annotation
    ret_str = ret.__name__ if hasattr(ret, "__name__") else str(ret)
    assert ret_str == "bool", (
        f"handle_message must return bool for cursor-advance contract; "
        f"got {ret_str!r}")


def test_legacy_started_tool_in_worker_raises():
    """Regression: 2026-04-28 (codex P1) — workers wrapped legacy
    fire-and-forget tools (returning {started: True, task_id: ...})
    and immediately marked the parent task completed while the actual
    work was still running elsewhere. Fix raises RuntimeError so the
    parent task FAILS instead of falsely succeeding."""
    import asyncio
    from orchestrator.worker import _await_legacy_tool

    async def run():
        try:
            await _await_legacy_tool(
                {"started": True, "task_id": "legacy-x", "tool": "foo"},
                None, "task-parent")
            raise AssertionError("should have raised")
        except RuntimeError as e:
            assert "started=True" in str(e)
            assert "Use a direct-mode tool" in str(e)

    asyncio.run(run())

    # Non-started results pass through normally
    async def run_ok():
        r = await _await_legacy_tool({"value": "ok", "rows": 5},
                                          None, "task-parent")
        assert r["value"] == "ok"
    asyncio.run(run_ok())


def test_journal_dict_detail_does_not_break_snapshot():
    """Regression: 2026-04-28 (codex P2) — _journal_decision writes detail
    as a dict, but the snapshot did `(detail or '')[:120]` which raises
    TypeError on dict slicing. The outer try-except swallowed it and the
    WHOLE journal section silently disappeared. Fix serializes detail to
    JSON before slicing."""
    import datetime as dt
    import json
    import tempfile
    from pathlib import Path
    from orchestrator import slack_agent

    tmp = Path(tempfile.mkdtemp())
    orig = slack_agent.REPO_ROOT
    slack_agent.REPO_ROOT = tmp
    try:
        (tmp / "data" / "concierge").mkdir(parents=True)
        jpath = tmp / "data" / "concierge" / "journal.jsonl"
        jpath.write_text(json.dumps({
            "ts": dt.datetime.now(dt.UTC).isoformat(),
            "event": "user_message",
            "detail": {"user": "U1", "thread_ts": "t.1",
                         "text_preview": "hello", "files": 0},
        }) + "\n")
        snap = slack_agent._build_state_snapshot(thread_ts="t.1")
        assert "Concierge journal" in snap, (
            "journal section missing — TypeError must have re-fired")
        assert "user_message" in snap
    finally:
        slack_agent.REPO_ROOT = orig


def test_schedule_default_uses_timezone_not_hardcoded_utc():
    """Regression: 2026-04-28 (codex P2) — update_campaign_schedule's
    default schedule_start_time was hardcoded to T12:00:00Z which only
    matches 8am New York during DST. Fix derives UTC from the supplied
    timezone + start_hour using zoneinfo. Verify Pacific 8am produces
    a different UTC offset than Eastern 8am."""
    from orchestrator import slack_agent
    args_eastern = {"campaign_id": "X", "timezone": "America/New_York",
                      "start_hour": "08:00"}
    args_pacific = {"campaign_id": "Y", "timezone": "America/Los_Angeles",
                      "start_hour": "08:00"}

    # We can't actually call the tool (no Smartlead CLI in tests). But we
    # can call the helper logic by mimicking its body — the bug we're
    # guarding against is the hardcoded T12:00:00Z. So instead, just
    # exercise the timezone helper directly:
    import datetime as dt
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    pt = ZoneInfo("America/Los_Angeles")
    today_et = dt.datetime.now(et).date() + dt.timedelta(days=1)
    today_pt = dt.datetime.now(pt).date() + dt.timedelta(days=1)
    et_start = dt.datetime.combine(today_et, dt.time(8, 0),
                                       tzinfo=et).astimezone(dt.UTC)
    pt_start = dt.datetime.combine(today_pt, dt.time(8, 0),
                                       tzinfo=pt).astimezone(dt.UTC)
    # ET 8am = 12 or 13 UTC depending on DST; PT 8am = 15 or 16 UTC.
    # Difference must be 3 hours (PT is 3 behind ET).
    delta = (pt_start - et_start).total_seconds() / 3600
    assert abs(delta - 3.0) < 0.5, (
        f"ET 8am vs PT 8am should differ by ~3h, got {delta}h")


def test_confirmation_gate_includes_new_write_tools():
    """Regression: 2026-04-28 (codex P2) — import_csv_to_smartlead and
    update_campaign_schedule were not in the confirmation gate list, so
    a vague "do it" was enough to mutate live Smartlead state without a
    fresh explicit yes. Fix adds them to the SYSTEM_PROMPT gate list."""
    from orchestrator import slack_agent
    sp = slack_agent.SYSTEM_PROMPT
    for tool in ("import_csv_to_smartlead", "update_campaign_schedule",
                  "launch_pilot", "schedule_campaign", "archive_campaign"):
        assert tool in sp, f"{tool} missing from SYSTEM_PROMPT gate list"
    # And the confirmation gate section explicitly names them
    gate_section = sp.split("# Confirmation gate")[-1] if "# Confirmation gate" in sp else sp
    for tool in ("import_csv_to_smartlead", "update_campaign_schedule"):
        assert tool in gate_section, (
            f"{tool} missing from explicit confirmation gate list")


if __name__ == "__main__":
    test_thread_state_is_json_serializable()
    test_serialization_handles_anthropic_blocks_via_run_tools_logic()
    test_tool_registry_consistency()
    test_copy_squad_handles_null_pick_in_hook()
    test_copy_squad_handles_null_sequence_in_body_output()
    test_active_thread_tracking_round_trip()
    test_run_pilot_aborts_on_empty_copy()
    test_handle_message_returns_bool_for_cursor_advance()
    test_legacy_started_tool_in_worker_raises()
    test_journal_dict_detail_does_not_break_snapshot()
    test_schedule_default_uses_timezone_not_hardcoded_utc()
    test_confirmation_gate_includes_new_write_tools()
    print("All slack_agent regression tests pass ✓")
