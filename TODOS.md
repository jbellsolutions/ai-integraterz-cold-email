# TODOS — Outstanding work + capability gaps

Auto-generated from grep of TODO/FIXME/HACK + the council critique top gaps.

## From council critique (Phase 3 of /agi-1, 2026-04-28)

- [ ] **Durable pipeline state store (SQLite).** LEAD_HANDLES, RUNNING_PILOTS, and per-stage outputs are in-memory only. A daemon restart silently drops in-flight pilots. Council priority #1.
- [ ] **Typed I/O contracts via pydantic.** Pydantic is a dep but unused. LLM JSON output is regex-parsed via `_safe_json`; Smartlead responses are dict-fished. Council priority #2.
- [ ] **Machine-readable campaign health.** `tool_stats` outputs prose; need `data/campaigns/<name>/state.json` with `last_run, lead_count, reply_rate, last_error` fields. Council priority #3.
- [ ] **PreToolUse hooks for destructive actions.** Currently the "ask before launching" gate lives in the Slack agent's system prompt only. A model regression silently disables it. Move to `.claude/settings.json` hooks.
- [ ] **Idempotency keys on launch_pilot.** A retry currently double-writes leads to Smartlead.
- [ ] **Token-budget circuit breaker on the tool-use loop.** `while True` is unbounded.
- [ ] **Cursor preserve-and-process pattern.** `poll_once` advances the cursor BEFORE `handle_message` completes — a crash mid-message permanently skips it.
- [ ] **Smartlead webhook intake.** Reply daemon polls every 60s; webhook would push instantly.
- [ ] **Slack Socket Mode.** Currently 8s polling. Socket Mode = sub-second.

## From grep TODO/FIXME/HACK

## Capability gaps logged in data/skill_gaps.jsonl

       3 data/skill_gaps.jsonl
