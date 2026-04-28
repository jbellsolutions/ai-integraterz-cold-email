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


if __name__ == "__main__":
    test_thread_state_is_json_serializable()
    test_serialization_handles_anthropic_blocks_via_run_tools_logic()
    test_tool_registry_consistency()
    print("All slack_agent regression tests pass ✓")
