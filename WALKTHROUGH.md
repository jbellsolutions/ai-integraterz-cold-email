# Cold Email 2.0 — Walkthrough

> **To install:** Open Claude Code in this folder and type `set this up for me` or `/walkthrough`

A multi-agent system that turns lead lists into personalized 3-email Smartlead campaigns and manages replies. Operated through Slack. Single-tenant.

## Prerequisites

- macOS or Linux
- Python 3.13 (managed via [uv](https://github.com/astral-sh/uv) recommended)
- Node.js (for the Smartlead CLI dependency)
- A Slack workspace with admin access (to install the bot)
- Smartlead account with API key + (optional) Prospector credits
- Anthropic API key

## Environment variables

These live in `.env` (gitignored). See `.env.example` for the template.

| Var | Required? | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | yes | `sk-ant-api03-...` |
| `SMARTLEAD_API_KEY` | yes | from Smartlead Settings → API |
| `SLACK_BOT_TOKEN` | yes | `xoxb-...` from your Slack app |
| `SLACK_CONTROL_CHANNEL` | yes | channel ID where you talk to the agent (`C0...`) |
| `SLACK_REPLY_CHANNEL` | yes | channel for reply approval pings (`#cold-email-replies`) |
| `CE2_HEARTBEAT_SECONDS` | no | heartbeat interval, default 1800 (set 0 to disable) |
| `CE2_MOCK_SMARTLEAD` | no | set `1` to use a mock CLI for testing |
| `CE2_MOCK_SLACK` | no | set `1` to print Slack messages instead of posting |

## Install

```bash
# 1. Clone + venv
git clone https://github.com/jbellsolutions/ai-integraterz-cold-email
cd ai-integraterz-cold-email
uv venv && source .venv/bin/activate
uv pip install -e .

# 2. Install the Smartlead CLI globally
npm install -g @smartlead/cli
smartlead login

# 3. Install forge (multi-agent harness)
uv pip install git+https://github.com/jbellsolutions/forge

# 4. Configure env
cp .env.example .env
$EDITOR .env  # fill in keys

# 5. Smoke test (no LLM cost — uses mock profiles)
python -m orchestrator.main --smoke
```

## Run

```bash
# 6. Start both daemons under the watchdog (auto-restart on crash)
nohup ./ops/watchdog.sh > logs/watchdog.log 2>&1 &
disown

# 7. Sanity check
./ops/status.sh
```

That's it. Talk to the agent in `#cold-email-control`.

## Common commands (in Slack `#cold-email-control`)

| You say | Agent does |
|---|---|
| `stats` | Posts a table: campaigns × leads × replies × replies-today |
| `list briefs` | Shows all cached briefs at `data/campaigns/*/brief.md` |
| Drop a CSV with caption "load these into recruiters-power-partner-A" | Ingests, validates, asks for confirmation, then loads |
| "Find me 50 recruiting agency owners in the US" | Free Prospector search → asks before fetching (which spends credits) |
| "Show me 3 examples for all 6 recruiter campaigns from campaign 3103961" | Generates `preview_pack` — 18 sample emails, no Smartlead writes |
| "Archive campaign 3243617" | Asks for explicit yes, then `set-status STOPPED` |
| "Pre-create campaigns for home-services on direct-value and capstone, A and B" | Runs the 4-cell precreate (~$8 LLM) |

## Testing

```bash
# Regression tests (5 of them, all from real bugs we hit)
python tests/test_slack_agent.py

# Or via pytest
pytest tests/

# Mock-mode smoke (no LLM cost)
CE2_MOCK_SMARTLEAD=1 python -m orchestrator.main --smoke
```

## Troubleshooting

**Agent isn't responding in Slack.**
```bash
./ops/status.sh        # daemon health
tail -30 logs/slack_agent.watchdog.log
```

**Replies aren't appearing in `#cold-email-replies`.** Check `reply_loop` is alive (`./ops/status.sh`); restart watchdog if the daemon died.

**Campaign created with the wrong copy.** Archive it via Slack ("archive campaign `<id>`"), delete the cached brief (`rm data/campaigns/<name>/brief.md`), and run `precreate_campaigns` to regenerate.

**Smartlead Prospector returns 0 leads.** Refine your description or build the saved search in the Smartlead UI and pass the saved-search ID.

## Where state lives

| Path | What's there |
|---|---|
| `data/campaigns/<name>/brief.md` | Cached strategy brief, reused on append |
| `data/voice_rules.md` | Justin's tone overrides, loaded on top of every brief |
| `data/emails/<email>.json` | Per-prospect generated 3-email sequence |
| `data/research/<email>.json` | Per-prospect signal |
| `data/replies/<id>.json` | Per-reply daemon state |
| `data/slack/threads/<ts>.json` | Per-conversation memory for the slack agent |
| `data/skill_gaps.jsonl` | Capability gap log |
| `.claude/learning/observations.json` | Auto-captured failures (AGI-1) |

## What this repo does NOT do

- Lead scraping / list building (out of scope)
- Multi-tenant or multi-operator support
- Real-time push (polling at 8s / 60s is intentional)
- Email sending itself (Smartlead does that)

## Where to look next

- `OPERATIONS.md` — operator's daily runbook
- `ARCHITECTURE.md` — full system diagrams
- `AGENTS.md` — every agent's role + boundary
- `AGI1_INTEGRATION.md` — how AGI-1's healer/learner wire into this repo

You should not need the terminal during normal operation. If you do, the runbook above tells you how to right things.
