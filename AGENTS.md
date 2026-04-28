# AGENTS.md — Who works on this repo

This repo is operated by a swarm of specialized agents. This document
declares their roles, boundaries, and handoffs.

## Live in-process agents (forge-spawned)

### Strategy Squad
**Where**: `squads/strategy/squad.py`
**Model**: Opus 4.7 (1M context) via `anthropic-opus-1m` profile
**Topology**: PARALLEL_COUNCIL × 3 members, MAJORITY consensus
**Job**: Read campaign brief context (offer + niche + voice rules) + lead summary, emit a `brief.md` to `data/campaigns/<name>/brief.md`. Runs ONCE per campaign; cached forever.
**Boundary**: Does not touch leads, does not call Smartlead, does not write copy.
**Handoff to**: Research Squad (passes `brief` string).

### Research Squad
**Where**: `squads/research/squad.py`
**Model**: Haiku 4.5 (`anthropic-haiku`)
**Topology**: SOLO × N parallel (max 10 concurrent)
**Job**: For each `Lead`, surface a per-prospect signal (recent post, hire, funding, podcast, etc.). Output: dict written to `data/research/<email>.json`.
**Boundary**: Read-only on the web/sources; never writes Smartlead state.
**Handoff to**: Copy Squad (passes `(lead, signal)` pair).

### Copy Squad
**Where**: `squads/copy/squad.py`
**Models**: Sonnet 4.6 (hook) + Haiku 4.5 (body)
**Job**: Hook draft → Body draft → 4 validators (slop, sales, URL, threading). Retry up to 3 times with violation feedback injected. Output: `data/emails/<email>.json`.
**Boundary**: Never writes to Smartlead. Never overrides voice rules.
**Handoff to**: Smartlead Squad (passes the email sequence).

### Smartlead Squad
**Where**: `squads/smartlead/squad.py`
**Model**: deterministic (no LLM)
**Job**: Lookup-or-create Smartlead campaign by `<niche>-<offer>-<VARIANT>`. On first creation, save sequence template. Always: append leads with per-prospect custom_fields holding the generated copy.
**Boundary**: Idempotent. Will NOT recreate a campaign if one with the name exists; will NOT clobber an existing sequence template.
**Handoff to**: Operator (review in Smartlead UI, then `schedule_campaign` flips DRAFTED → ACTIVE).

### Reply Squad
**Where**: `squads/reply/squad.py`
**Models**: Haiku (triage + approver) + Sonnet (drafter)
**Job**: Per inbound reply: classify → draft response → URL allowlist gate. Auto-handles unsubscribe/OOO/spam silently. Other replies post to `#cold-email-replies` for human approval.
**Boundary**: Never sends a reply without human "send" in the thread (or auto-handle category).
**Handoff to**: Operator via Slack thread; on confirmation, calls `cli.reply_to_thread`.

## Daemons (long-running processes)

### slack_agent
**Where**: `orchestrator/slack_agent.py`
**Model**: Sonnet 4.6 for the routing tool-use loop
**Job**: Poll `#cold-email-control` every 8s; for each user message, run Anthropic tool-use over 17 registered tools.
**Boundary**: Confirms before destructive/credit-spending tools (`launch_pilot`, `prospect_fetch_confirmed`, `archive_campaign`, `precreate_campaigns`, `schedule_campaign`).
**Trigger**: User messages or file uploads in the control channel.
**Termination**: Never (under watchdog supervision).

### reply_loop
**Where**: `orchestrator/reply_loop.py`
**Job**: Poll Smartlead inbox every 60s. Process new replies through Reply Squad.
**Boundary**: Auto-handles only if triage confidence is high (unsubscribe/OOO/spam classes).
**Trigger**: Polling tick.
**Termination**: Never (under watchdog).

### watchdog
**Where**: `ops/watchdog.sh`
**Job**: Supervise both daemons. Restart within 15s of crash. Throttle to one restart per 30s.
**Termination**: User CTRL-C or kill.

## Skill agents (AGI-1 — installed at `~/.claude/skills/agi-1/`)

| Slash command | What it does | When to invoke |
|---|---|---|
| `/agi-1` | Full 8-phase pipeline | Major refactor / first bootstrap |
| `/agi-audit` | Score this repo | Before/after a change set |
| `/agi-heal` | Auto-fix a known error pattern | When a runtime error occurs |
| `/agi-learn` | Extract patterns from observations | Every 10 sessions |
| `/agi-council` | 3-perspective critique | Before a significant design change |
| `/agi-walkthrough` | End-to-end explainer | Onboarding a new collaborator |

## Handoff data schemas

Currently dict-passing (gap; pydantic adoption is a TODO). The implicit contracts:

- **Lead** → `{lead_id, name, email, company, title, linkedin_url}`
- **Signal** (research output) → `{tier: 'S'|'A'|'B', summary: str, evidence: list[str]}` (loose)
- **Email step** → `{step: 1|2|3, subject: str, body: str, delay_days: int}`
- **Smartlead lead payload** → `{first_name, last_name, email, company_name, custom_fields: dict}`

Future work: enforce these via pydantic models (TODO #2 in council critique).

## Escalation path

```
slack_agent (orchestrator)
   ↓ (operator confirms)
launch_pilot → Strategy → Research → Copy → SmartleadSquad
                                                ↓
                                          Smartlead (sends)
                                                ↓
                                          replies arrive
                                                ↓
                                          reply_loop → Reply Squad
                                                ↓
                                          #cold-email-replies (operator approves)
```

Maximum hop count from orchestrator to human = 2 (orchestrator → squads → operator review).

## KPIs (per agent)

Currently informal. Council critique flagged this as the lowest-scoring dimension. Targets to instrument (TODO):

- Strategy: % of briefs reused on append (target >90%)
- Research: signal tier distribution per campaign (target ≥30% S/A)
- Copy: slop_pass rate first attempt (target ≥80%); avg attempts to pass (target ≤1.5)
- Smartlead: lookup-vs-create ratio (target close to 1:1 once campaigns mature)
- Reply Squad: auto-handle rate (target ~30% of replies handled silently)
- slack_agent: median time to first response (target <12s)
- watchdog: restarts/day (target 0; alert if >3)
