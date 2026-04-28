# Cold Email 2.0

Opus-orchestrated forge swarm for personalized cold email at scale on Smartlead.

**Scope:** turn a researched lead CSV into a personalized 3-email Smartlead campaign and manage the replies. Nothing else.

- List-building / TAM scraping → out of scope (separate concern)
- Per-prospect deep research → in scope (inseparable from copy)
- Smartlead is the only sender; Instantly removed

> **Operating this system?** Read [OPERATIONS.md](OPERATIONS.md) — the daily runbook. Everything below is for setting it up; OPERATIONS.md is for running it.

## Architecture

```
USER ⇄ ORCHESTRATOR (Opus 4.7, 1M context)
            │
   ┌────────┼─────────┬──────────┐
   ▼        ▼         ▼          ▼
STRATEGY RESEARCH   COPY    SMARTLEAD
(Opus)   (Haiku)  (Sonnet+  (Haiku)
                  Haiku)
```

Built on [forge](https://github.com/jbellsolutions/forge) — the orchestrator + each squad lead is a forge agent; squads parallelize via `Spawner` + `PARALLEL_COUNCIL`.

## Quickstart

```bash
# 1. Set up env
uv venv && source .venv/bin/activate
uv pip install -e .
npm install -g @smartlead/cli && smartlead login

# 2. Configure
cp config/.env.example .env  # ANTHROPIC_API_KEY, SMARTLEAD_API_KEY

# 3. Smoke test the harness
python -m orchestrator.main --smoke

# 4. Pilot run
cp your_leads.csv data/leads/pilot.csv
python -m orchestrator.main --angle=power-partner --leads=data/leads/pilot.csv
```

## Talk to the agent in Slack

Once-and-for-all interface — DM/post to the orchestrator in Slack instead of the CLI.

**One-time Slack setup:**
1. Create a private channel `#cold-email-control` in your workspace.
2. Invite the bot user (the same bot used by `#cold-email-replies`).
3. Required bot scopes (Slack app config → OAuth & Permissions): `channels:history` (or `groups:history` for private), `chat:write`, `files:read`, `users:read`. Re-install the app to your workspace if you add scopes.
4. Set in `.env`: `SLACK_CONTROL_CHANNEL=#cold-email-control`.

**Run the daemon (long-lived):**
```bash
nohup .venv/bin/python -m orchestrator.slack_agent > logs/slack_agent.log 2>&1 &
```

**What you can say in `#cold-email-control`:**
- Drop a CSV with caption "load these into recruiters-power-partner-A" — agent ingests, validates, asks for confirmation, then runs the pilot.
- "Find me 50 recruiting agency owners in US, 11-50 employees, founder/owner seniority" — runs a *free* Prospector search; you confirm before it spends credits.
- "Stats" — campaigns by (niche, offer, variant) + reply counts.
- "Show me the brief for `recruiters-power-partner-A`."
- "Archive campaign 3243617." — destructive, agent will ask for explicit yes.

The agent gates every credit-spending or destructive action behind explicit confirmation. Read-only commands (list, stats, read brief) run without asking.

## Pre-create campaigns ahead of leads

Six recruiter campaigns × 2 variants are already pre-created and DRAFTED in Smartlead with cached briefs. To pre-create more (e.g. home-services niche):

```bash
python -m orchestrator.precreate \
    --niche=home-services \
    --offers=direct-value,capstone \
    --variants=A,B
```

When leads later arrive for that combo, the pipeline skips the Strategy stage (cached) and only runs Research + Copy + add_leads (~2 min).

## Lead CSV format

Required columns (extras passed through):

| name | email | company | title | linkedin_url |
|---|---|---|---|---|

Source-agnostic — Sales Nav, Apollo, Smartlead Prospector, hand-built, all fine.

## Angles (campaign modules)

Each `campaigns/<angle>/` contains a swipe file, positioning, and offer doc that the Strategy squad loads as context. Adding a new angle = adding a folder.

- `campaigns/power-partner/` — Jay-Abraham-style partner recruitment (free deploy, revenue share)
- `campaigns/skool-capstone/` — Skool community capstone-project framing
- `campaigns/direct-value/` — direct sales outreach

## Model routing

See [config/models.yaml](config/models.yaml). Default mode runs ~$25–40 per 1K emails on Anthropic; cost-mode swaps research + body to DeepSeek v3 via OpenRouter for ~$5–10 per 1K.

## Plan

[Full rebuild plan](../../../.claude/plans/alright-so-we-need-structured-castle.md)
