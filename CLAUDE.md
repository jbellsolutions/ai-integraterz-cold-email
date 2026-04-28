# CLAUDE.md — Project Context for Claude Code

This file is loaded automatically by Claude Code when working in this repo. It contains the project's mission, the specific constraints an agent must respect, and a session-start checklist.

## Project Mission

Cold Email 2.0 is a four-squad multi-agent system that turns lead lists into personalized 3-email Smartlead campaigns and manages the replies. Operated through Slack: `#cold-email-control` for outbound work, `#cold-email-replies` for reply approval. Single-tenant, single-operator (Justin / AI Integraterz).

Read in this order to onboard:
1. `README.md` — architecture + quickstart
2. `OPERATIONS.md` — daily runbook (this is the operator manual)
3. `ALWAYS_ON.md` — uptime model + watchdog
4. `AGI1_INTEGRATION.md` — how AGI-1 self-healing wires in
5. `SELF_HEALING.md` — failure-handling philosophy

## Session Start Checklist

At the start of every session in this repo:

- [ ] Run `/project-main` for a repo health brief (Level 1 orchestrator at `.claude/agents/main-agent.md`)
- [ ] Check `data/skill_gaps.jsonl` for capability gaps the operator has surfaced
- [ ] Check `logs/watchdog.log` to confirm both daemons are healthy
- [ ] Read the most recent entry under `.claude/agi-1/candidates/iter_NNNN/` if relevant to the task

## Hard Rules — NEVER

- **NEVER** push leads to a Smartlead campaign without explicit user confirmation in the same Slack thread. The `launch_pilot`, `prospect_fetch_confirmed`, `archive_campaign`, and `precreate_campaigns` tools require an explicit "yes / proceed / launch / confirm" from the user.
- **NEVER** commit `.env` to git. The `.gitignore` covers it; if you find yourself unsticking a commit that includes it, stop and ask.
- **NEVER** delete `data/campaigns/<name>/brief.md` casually. That's the cached strategy brief. Regenerating it costs money AND produces a different brief — the variant becomes apples-to-oranges.
- **NEVER** rename or hand-edit Smartlead campaigns mid-pilot via the UI. The orchestrator's idempotent lookup-by-name will create duplicates.
- **NEVER** include URLs in cold emails 1, 2, or 3. The Copy squad's `url_check` validator enforces this. Links land only in the *reply* after the prospect engages.
- **NEVER** skip regression tests when fixing a bug. Every bug we've hit has a test in `tests/test_slack_agent.py`. Add one.

## Hard Rules — ALWAYS

- **ALWAYS** run `tests/test_slack_agent.py` before committing changes that touch `orchestrator/slack_agent.py`, `tools/slack_notify.py`, or `squads/copy/squad.py`.
- **ALWAYS** restart the daemons via the watchdog (`ops/watchdog.sh`) after substantive code changes. Cursor preservation prevents missed messages.
- **ALWAYS** use `_emit_error` for any uncaught exception path so failures surface in Slack within seconds.
- **ALWAYS** preserve the brief cache when adding new campaigns to an existing (niche, offer, variant). The pipeline is designed to skip the Strategy stage on append.

## Hard Rules — MUST

- **MUST** treat `data/voice_rules.md` as authoritative over campaign briefs. The Copy squad loads voice rules ON TOP of every brief — voice rules win on conflicts.
- **MUST** keep `tests/test_slack_agent.py` green. New tools added without a registration test = drift bug waiting to happen.
- **MUST** use `_safe_list` and `_pick_candidate` helpers when consuming LLM output. Never `dict.get(key, default)` for list/int values where the LLM might emit explicit `null`.
- **MUST** call `log_capability_gap` BEFORE telling the user "I can't do that". Recurring gaps drive the next-tools backlog.

## Tool Permissions

Default permissions for an in-session agent:
- `Read`, `Glob`, `Grep` — full access
- `Bash` — full access (the daemons rely on it)
- `Write`, `Edit` — full access except `.env` (gitignored, must not be modified by an agent)
- `WebFetch` — only against documented sources (Anthropic docs, Smartlead docs, Slack docs)

## Workflow Conventions

- **Plan files** live at `~/.claude/plans/<descriptor>.md` (the global Claude Code convention). Reference them in commits.
- **Capability gaps** go to `data/skill_gaps.jsonl` via `log_capability_gap`.
- **Voice updates** that apply to all campaigns go to `data/voice_rules.md` via `update_voice_rules`. Per-campaign feedback goes to `data/campaigns/<name>/brief.md` via `update_brief`.
- **Regression tests** for every bug. Naming: `test_<symptom>_<root_cause>` in `tests/test_slack_agent.py`.

## Model Selection

This repo runs cost-aware multi-model routing through `config/models.yaml`:
- **Strategy squad** → `anthropic-opus-1m` (1M context, runs once per campaign, cached)
- **Hook squad** → `anthropic` (Sonnet 4.6 — first 1-2 lines is where replies are won)
- **Body squad** → `anthropic-haiku` (Haiku 4.5 — bulk text generation)
- **Research squad** → `anthropic-haiku` (parallel per-prospect signal mining)
- **Reply triage / drafter / approver** → `anthropic-haiku` + `anthropic` (Sonnet for the drafter)
- **Slack agent (orchestrator routing)** → `anthropic` (Sonnet)

Switch `active_mode: cost` in `config/models.yaml` to swap research + body to DeepSeek via OpenRouter for ~5x cost reduction at modest quality cost. The hook squad stays on Sonnet — that's the line that buys attention.

## What This Repo Is NOT

- Not a list-building / scraping tool. Lead intake is CSV upload OR Smartlead Prospector — that's it.
- Not a CRM. Smartlead is the system of record for lead state.
- Not a multi-tenant platform. Single-operator, single Smartlead account.
- Not a real-time agent. ~5–8s polling latency is intentional and fine.

## Related Skills (AGI-1)

If a task involves debugging, healing, learning, auditing, or council critique, use the AGI-1 skill set already installed at `~/.claude/skills/agi-1/`:
- `/agi-heal` — fix errors with verification
- `/agi-learn` — extract patterns from observations
- `/agi-audit` — re-score the repo
- `/agi-council` — three-perspective critique
- `/agi-walkthrough` — explain a feature end-to-end

Skill-obsession: before writing custom code for a recurring problem, search `~/.claude/skills/` for an existing skill. Log misses to `.claude/skill-mastery/evals.json`.
