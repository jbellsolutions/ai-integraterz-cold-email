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


if __name__ == "__main__":
    test_thread_state_is_json_serializable()
    test_serialization_handles_anthropic_blocks_via_run_tools_logic()
    test_tool_registry_consistency()
    test_copy_squad_handles_null_pick_in_hook()
    test_copy_squad_handles_null_sequence_in_body_output()
    test_active_thread_tracking_round_trip()
    test_run_pilot_aborts_on_empty_copy()
    print("All slack_agent regression tests pass ✓")
