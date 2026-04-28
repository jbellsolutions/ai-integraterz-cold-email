# ARCHITECTURE.md

## High-level shape

```
┌─────────────────────────────────────────────────────────────────────┐
│                       OPERATOR (Slack)                              │
│  #cold-email-control          │       #cold-email-replies           │
│  (ask the agent)              │       (approve drafted replies)     │
└──────────┬───────────────────┴──────────────┬──────────────────────┘
           │                                  │
       polls 8s                          polls 60s
           │                                  │
   ┌───────▼────────┐                ┌────────▼─────────┐
   │  slack_agent   │                │   reply_loop     │
   │  (orchestrator) │               │  (reply daemon)  │
   │  Anthropic     │                │  Anthropic       │
   │  tool-use loop │                │  triage→draft→   │
   └───────┬────────┘                │  approve→post    │
           │                         └────────┬─────────┘
       fan-out                                │
           │                                  │
   ┌───────▼─────────────────────────────────▼──────────┐
   │              4 SQUADS (forge.Spawner)              │
   │                                                    │
   │  Strategy ──┐                                      │
   │             ├─→ Research ─→ Copy ─→ Smartlead      │
   │  (cached)   │   (Haiku ×N) (Sonnet+Haiku) (CLI)    │
   │             │                                      │
   │  Reply Squad (separate flow)                       │
   └────┬───────────────────────────────────────────────┘
        │
   ┌────▼────────────────────────────────────────────┐
   │  EXTERNAL SYSTEMS                               │
   │  • Smartlead (campaigns, leads, sequences,      │
   │    Prospector, inbox, replies)                  │
   │  • Anthropic API (Opus 4.7 / Sonnet / Haiku)    │
   │  • Slack Web API (chat, files, reactions)       │
   └─────────────────────────────────────────────────┘
```

## Directory layout

```
.
├── CLAUDE.md / ETHOS.md / ARCHITECTURE.md / README.md / OPERATIONS.md
├── ALWAYS_ON.md / SELF_HEALING.md / AGI1_INTEGRATION.md
├── CHANGELOG.md / VERSION / TODOS.md
│
├── orchestrator/                  Long-running daemons
│   ├── slack_agent.py             Polls #cold-email-control, runs tool loop
│   ├── reply_loop.py              Polls Smartlead inbox, drafts replies
│   ├── main.py                    CLI entry (--smoke, --watch-replies, --slack-agent)
│   ├── precreate.py               Pre-create campaigns (niche × offer × variant)
│   ├── cleanup.py                 Archive old campaigns (snapshot first)
│   └── stats.py                   Operational stats CLI
│
├── squads/                        Multi-agent forge wrappers
│   ├── _base.py                   Squad parent class + monkey-patches forge to find local profiles
│   ├── strategy/                  PARALLEL_COUNCIL × Opus, runs once per campaign
│   ├── research/                  Haiku × N parallel signal mining
│   ├── copy/                      Hook (Sonnet) → Body (Haiku) → 4 validators with retry
│   ├── smartlead/                 Deterministic CLI plumbing, idempotent lookup-or-create
│   └── reply/                     Triage → draft → approve → URL allowlist
│
├── tools/                         Lower-level utilities
│   ├── slack_notify.py            SlackClient + SlackNotifier (read/post/react/files)
│   ├── smartlead.py               SmartleadCLI subprocess wrapper
│   ├── lead_loader.py             CSV + Prospector + existing-campaign loaders
│   └── prospect_filters.py        NL → Prospector filter dict (Haiku call)
│
├── campaigns/                     Authored offer + niche docs (loaded by Strategy)
│   ├── power-partner/  capstone/  direct-value/
│   └── <each>/{positioning,offer,voice,sequences,niches/}.md
│
├── config/
│   ├── models.yaml                Active mode + role→profile mapping
│   └── profiles/                  Anthropic Opus 4.7 / Opus-1m profiles
│
├── data/                          Runtime state (gitignored except .gitkeep)
│   ├── campaigns/<name>/          brief.md, run-*.json, precreated.json
│   ├── emails/<email>.json        Per-prospect generated sequence
│   ├── research/<email>.json      Per-prospect signal
│   ├── replies/<id>.json          Per-reply state from reply_loop
│   ├── slack/                     cursor.json, threads/<ts>.json, uploads/<id>.csv
│   ├── voice_rules.md             Justin's overrides — Copy squad loads on top of brief
│   ├── archive/                   Pre-archive snapshots
│   ├── cache/prospect_filters.json   24h cache of Prospector filter values
│   └── skill_gaps.jsonl           Capability-gap log (agent writes here)
│
├── ops/                           Daemon supervision
│   ├── watchdog.sh                Bash supervisor (15s, 30s throttle)
│   ├── com.aiintegraterz.cold-email.*.plist   launchd plists (TCC-blocked from Desktop)
│   ├── install.sh / uninstall.sh / status.sh
│   └── run-with-env.sh            Wrapper that sources .env
│
├── tests/                         Regression tests (every bug → 1+ test)
│   ├── test_slack_agent.py        Tool registry, JSON ser, null-pick, null-sequence
│   ├── test_reply_pipeline.py
│   └── test_smoke_forge.py
│
├── logs/                          Runtime logs (gitignored)
│
├── .claude/                       AGI-1 + Claude Code scaffolding (this commit)
│   ├── agi-1/                     baseline.json, candidates/iter_NNNN/, rescore.json
│   ├── agents/main-agent.md       Level 1 orchestrator (project-main)
│   ├── healing/patterns.json      Self-healing pattern DB
│   ├── learning/                  observations.json, evolution.json, dream-log.json
│   ├── memory/                    user.md, feedback.md, project.md, reference.md
│   ├── checkpoints/               Session checkpoint extracts
│   ├── hooks/                     PostToolUse + Stop hooks
│   ├── security/pii-patterns.json
│   ├── skill-mastery/             skill-registry.json + evals.json
│   └── settings.json              Hook wiring + MCP scoping
│
└── .agent/                        Level 2 persistent Python agent
    ├── agent.py / identity.json / state.json / README.md
```

## Data flow — outbound campaign

1. **Operator → Slack**: drops CSV or describes target audience in `#cold-email-control`
2. **slack_agent** picks message, runs Anthropic tool-use loop. Tools fire:
   - `ingest_csv_from_slack` OR `pull_prospector_nl` → confirmed → `prospect_fetch_confirmed`
   - `launch_pilot(niche, offer, variant, lead_handle)` after operator says yes
3. **launch_pilot → run_pilot** (asyncio task): Strategy (cached) → Research × N parallel → Copy × N (Hook → Body → Validators × 4 with retry-feedback) → SmartleadSquad.build_campaign (lookup-or-create + add_leads + save_sequence on first creation)
4. **Operator reviews `preview_emails` output → schedule_campaign** flips DRAFTED → ACTIVE
5. **Smartlead sends.** Replies arrive in inbox.

## Data flow — inbound reply

1. **reply_loop** polls Smartlead inbox every 60s
2. For each new reply: Triage (Haiku) → classify (positive/objection/spam/OOO/unsubscribe)
3. Auto-handle (silent): unsubscribe, OOO, pure spam
4. Otherwise: Drafter (Sonnet) drafts response → Approver (Haiku) scores → URL allowlist gate
5. Post to `#cold-email-replies` as Block Kit, operator replies in thread: `send`/`skip`/edited body
6. reply_loop reads thread reply on next tick → posts via `cli.reply_to_thread`

## State machine — campaign lifecycle

```
                ┌── NOT_EXISTS ──────┐
                │                    │
                │  precreate_campaign│
                │  build_campaign    │
                ▼                    │
            DRAFTED ◄────────────────┘
              │   ▲
              │   │ append_leads (idempotent)
              │   │
       schedule_campaign
              │
              ▼
            ACTIVE
              │
              ├── (timed out / done) ──► COMPLETED
              │
              └── archive_campaign ──► STOPPED (reversible)
                                       │
                                       └── delete_campaign ──► gone
```

State is held in Smartlead (canonical) + `data/campaigns/<name>/` (cached brief + run logs).

## Key abstractions

- **Lead** (`squads.research.squad.Lead`): the canonical lead dataclass. `lead_id, name, email, company, title, linkedin_url`.
- **Squad** (`squads._base.Squad`): wraps `forge.Spawner` with squad-specific instructions, role list, topology, consensus.
- **SmartleadCLI** (`tools.smartlead.SmartleadCLI`): subprocess wrapper around `smartlead` npm CLI. All Smartlead reads/writes funnel through here.
- **SlackClient** (`tools.slack_notify.SlackClient`): httpx-based Slack Web API wrapper. `read_channel`, `post`, `add_reaction`, `download_file`, etc.
- **Validators** (`squads.copy.squad`): four deterministic gates (`slop_check`, `sales_check`, `url_check`, `threading_check`) — failures inject violations back into the next prompt attempt.

## Integration points

| External | Coupling | Failure mode |
|---|---|---|
| Smartlead | npm CLI subprocess + token | CLI errors → SmartleadCLI raises, caller catches |
| Anthropic API | python sdk | Rate limit → currently unhandled (gap), tool loop retries |
| Slack Web API | httpx + bot token | best-effort reactions, bubbled-up post failures land as `:warning:` in channel |
| Forge | github editable install | unmonitored — pinned only by lockfile |

## Concurrency model

- **slack_agent**: single-threaded asyncio. One `asyncio.Lock` per Slack thread_ts. Long-running tools (`launch_pilot`, `generate_preview_pack`) spawn into `asyncio.create_task` and report back to the channel.
- **reply_loop**: single-threaded asyncio with 60s sleep between ticks.
- **watchdog**: bash, polls subprocess PIDs every 15s. Restarts on death, throttles to one restart per 30s.

## Known weaknesses (tracked, not yet addressed)

See `.claude/agi-1/candidates/iter_0001/council-synthesis.json` for the council's full critique. Top three:

1. In-memory `LEAD_HANDLES` / `RUNNING_PILOTS` are lost on watchdog restart. No SQLite manifest yet.
2. No typed contracts at layer boundaries. Pydantic is a dep but unused for I/O schemas.
3. No machine-readable per-campaign health endpoint. Stats command outputs prose.

These are Phase 5 autoresearch candidates.
