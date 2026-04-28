# Changelog

All notable changes to Cold Email 2.0 are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- AGI-1 bootstrap on this repo (Phase 4 of `/agi-1` run on 2026-04-28)
  - `.claude/` scaffold: agents, healing, learning, memory, checkpoints, hooks, security, skill-mastery
  - `.agent/` Level 2 persistent Python agent
  - Auto-observation hooks (PostToolUse + Stop)
- Foundational docs: `CLAUDE.md`, `ETHOS.md`, `ARCHITECTURE.md`, `VERSION`, `TODOS.md`
- Council critique results at `.claude/agi-1/candidates/iter_0001/`

## [0.1.0] — 2026-04-28

Initial public release. The system is functional end-to-end and supervised by
a watchdog. AGI-1 is bootstrapped this commit.

### Operational layer (today)
- 3 honest answers (auto-skill, examples, no-crash) with shipped infrastructure
- `generate_preview_pack` tool that previews per-prospect copy without writing to Smartlead
- 4 deterministic validators on every drafted email: `slop_check`, `sales_check`, `url_check`, `threading_check`
- Retry-with-violation-feedback in Copy squad (3 attempts; hook re-rolled when implicated)
- `update_voice_rules` + `update_brief` tools so the Slack agent can apply operator feedback autonomously
- 7 regression tests for bugs we've actually hit

### Always-on layer
- `ops/watchdog.sh` — bash supervisor for both daemons (15s interval, 30s crash-loop throttle)
- Heartbeat: agent posts uptime status to `#cold-email-control` every 30 min
- launchd plists at `ops/com.aiintegraterz.cold-email.*.plist` (TCC-blocked from `~/Desktop` — install path documented)
- `_emit_error` posts every uncaught exception to Slack as `:warning:` within seconds

### Slack agent
- Polling daemon on `#cold-email-control` (8s interval, ~470 LOC)
- 17 tools: list_active_campaigns, list_briefs, read_brief, stats, ingest_csv_from_slack, pull_prospector_nl, pull_prospector_saved, prospect_fetch_confirmed, launch_pilot, load_leads_from_smartlead, preview_emails, schedule_campaign, generate_preview_pack, archive_campaign, precreate_campaigns, update_voice_rules, update_brief, log_capability_gap
- Visual reactions on every message: 👀 working → ✅ done / ❌ error
- Per-thread conversation memory at `data/slack/threads/<ts>.json`

### Pre-created campaigns
- 6 recruiter campaigns DRAFTED in Smartlead with cached briefs:
  - `recruiters-power-partner-{A,B}`
  - `recruiters-direct-value-{A,B}`
  - `recruiters-capstone-{A,B}`

### Reply daemon
- Triage → Drafter → Approver pipeline with thread-reply approval protocol
- URL allowlist enforcement (replies can only link to URLs from offer docs)
- Auto-handles OOO / unsubscribe / pure spam silently

### Squads
- Strategy (Opus 4.7 1M ctx, parallel council), Research (Haiku × N parallel), Copy (Sonnet hook + Haiku body + 4 validators), Smartlead (deterministic CLI plumbing)
- Niche × offer × variant campaign axis with idempotent lookup-or-create
- Brief caching at `data/campaigns/<name>/brief.md` so re-runs stay apples-to-apples

[Unreleased]: https://github.com/jbellsolutions/ai-integraterz-cold-email/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/jbellsolutions/ai-integraterz-cold-email/releases/tag/v0.1.0
