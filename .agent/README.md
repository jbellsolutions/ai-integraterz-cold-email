# Level 2: Persistent Agent

Level 2 is a Python process that runs directly on your machine, outside Claude
Code. It maintains cross-session memory, schedules proactive operations, and
gives each repo a persistent AI presence that works even when you are not in
an active Claude Code session.

---

## What Level 2 Is

Level 1 (the `.claude/agents/main-agent.md` file) is session-aware but
stateless between sessions. Every time you start Claude Code, it reads files
from disk and rebuilds context. That is fast and requires no infrastructure.

Level 2 is a Python process with its own memory:

- Conversation history persists across runs in `state.json`
- Session count is tracked — enabling scheduled actions based on cadence
- Proactive checks run on startup: "It's been 7+ days — should we run /agi-heal?"
- Identity: the agent knows this repo's name, path, and history

You run it with `python agent.py`. It starts a CLI loop. Exit with `quit` or
Ctrl+C — state is saved automatically.

---

## Files Created

```
.agent/
  agent.py        — Main agent loop (Anthropic SDK, Claude model)
  state.json      — Persisted state: session count, history, last sync dates
  tasks.json      — Task queue (separate from features.json)
  scheduler.py    — Scheduled action definitions and due-date logic
  README.md       — This file (copied from template)
```

---

## How to Run

```bash
cd {repo-root}/.agent
pip install anthropic
python agent.py
```

Requirements:
- Python 3.10+
- `anthropic` Python SDK (`pip install anthropic`)
- `ANTHROPIC_API_KEY` set in your environment

First run initializes `state.json` if it does not exist.

---

## What Level 2 Does That Level 1 Does Not

| Capability | Level 1 | Level 2 |
|------------|---------|---------|
| Repo health brief | Yes (each session) | Yes (each run) |
| Cross-session memory | No (reads files) | Yes (state.json) |
| Conversation history | No | Yes |
| Proactive scheduling | No | Yes |
| Runs outside Claude Code | No | Yes |
| Independent process | No | Yes |
| Session count tracking | No | Yes |

### Scheduled Actions (built-in)

The agent checks these on every startup:

| Action | Cadence | Trigger |
|--------|---------|---------|
| Suggest `/agi-heal` | Weekly (7 days since last heal check) | "It has been 7+ days. Run /agi-heal to check for error patterns." |
| Suggest `/agi-learn` | Every 10 sessions | "You have completed 10 sessions. Run /agi-learn to process observations." |
| Suggest `/agi-sync` | Monthly (30 days since last genome sync) | "It has been 30+ days since genome sync. Run /agi-sync." |

---

## Prerequisites

- Python 3.10 or higher
- `anthropic` Python SDK: `pip install anthropic`
- `ANTHROPIC_API_KEY` environment variable set
  - Get your key at https://console.anthropic.com/
  - Add to shell profile: `export ANTHROPIC_API_KEY="sk-ant-..."`

---

## Upgrading From Level 1

Level 2 does not replace Level 1. The `.claude/agents/main-agent.md` file
stays in place for Claude Code sessions. Level 2 supplements it with a
standalone process for async and scheduled work.

Both read the same files: `claude-progress.txt`, healing history,
observations.json, features.json. They share state through the filesystem.
