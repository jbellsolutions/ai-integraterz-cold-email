# Always-On — What's Real, What's Marketing, What We Have

You said: *"I thought Forge is the most advanced SDK… AGI on the forge… do we need Hermes? It's not always on, not always paying attention."*

Honest engineering answer.

## The vocabulary

**Forge** is *our internal multi-agent harness library*. It's a Python coordinator: it spawns several Anthropic API calls in parallel, applies a consensus algorithm to their outputs, and persists results. It's useful — that's why we use it for the Strategy / Research / Copy / Reply squads — but Forge alone is a coordinator, not a self-improving system.

**AGI-1** *is* real and Justin has it installed globally — see `AGI1_INTEGRATION.md` in this repo. AGI-1 (v2.2.1, at `~/Desktop/Rethinking Repo's/agi-1/`) provides the self-healing/self-learning layer with 17 skills (auditor, healer, learner, council, etc.) and an 8-phase audit-and-improve pipeline. It is *not yet bootstrapped on this repo* — that's the gap to close. Earlier drafts of this document dismissed it as marketing; that was wrong.

**Hermes** isn't part of this stack. Even if we added a different LLM, that wouldn't fix uptime — uptime is infrastructure, not model choice.

## What "always on" actually means and what it costs

For a Slack-style agent to feel **always-on, always-attentive**, four things have to be true:

| Property | What it requires | What we have today |
|---|---|---|
| Process stays alive | Crash auto-restart (supervisor) | ✅ Now: `ops/watchdog.sh` — auto-restarts both daemons within 15s of crash |
| Survives Mac restart | Login-startup OR remote hosting | ⚠️ Partial: needs Login Items entry OR move repo to enable launchd |
| Receives messages instantly | Slack push (Events API + Socket Mode) | ❌ Today: 5–8s polling delay |
| Survives Mac sleep / closed lid | Hosted, not on your laptop | ❌ Today: dies when Mac sleeps |

The first one we just shipped. The other three involve real infrastructure decisions.

## What we shipped today

**1. Watchdog supervisor** — `ops/watchdog.sh`
- Restarts the slack agent + reply daemon if either crashes
- 15-second supervision interval
- Crash-loop throttle (won't restart faster than every 30s)
- One log file per daemon under `logs/`

Run with:
```bash
nohup ./ops/watchdog.sh > logs/watchdog.log 2>&1 &
```

**2. Heartbeat** — Slack agent posts a `:heartbeat: alive · uptime Xh:YYm · pulse #N` line every 30 minutes so you can SEE that it's alive and listening. Knob: `CE2_HEARTBEAT_SECONDS` env var (set to 0 to disable).

**3. Health check** — `ops/status.sh` shows daemon state + recent logs in one screen.

**4. (Stretch — not enabled yet) launchd plists** — `ops/com.aiintegraterz.cold-email.*.plist` would make this fully OS-supervised (auto-start at login, OS handles supervision). They're in the repo and `ops/install.sh` works, *but* macOS's TCC (Full Disk Access) gates launchd-spawned processes from `~/Desktop` paths. Two ways to enable it:

   - **Move the repo out of `~/Desktop`** (e.g. `~/cold-email-2.0`) — cleanest, no permission grants needed.
   - **Grant Full Disk Access to `/bin/bash`** in System Settings → Privacy & Security → Full Disk Access — quicker, slightly less clean.

I left both options documented; the watchdog covers the gap until you pick one.

## What we haven't shipped (and why not)

**Slack Socket Mode (push, not poll).** Would drop latency from ~8s to ~instant. Requires:
- Adding a Slack app-level token (`xapp-…`) in OAuth & Permissions
- Adding `slack_bolt` as a Python dependency
- Refactoring the daemon's main loop from polling to event-driven

A real upgrade, but ~6–8 hours of careful work and the polling shape is fine for a single-operator workflow. Polling is also more robust to weird network conditions — push systems hide failures more easily than polling does.

**Hosted backend (e.g. small VPS, Cloudflare Worker, or Anthropic-hosted agent).** Would survive your Mac sleeping or restarting. Tradeoffs:
- Costs $5–10/mo
- Adds a deployment surface (CI, secrets management, log access)
- Makes "I want to debug locally" harder

For a single-operator shop, running on your Mac with the watchdog + Login Items is genuinely fine. The pattern most ops bots run on production-grade is the watchdog pattern; the upgrade to a hosted backend is something you do when you have ≥3 operators or sensitive on-call requirements.

## What to do right now

To get the strongest "always-on feel" without infrastructure changes:

```bash
# 1. Start the watchdog inside a tmux session so it survives terminal close
tmux new-session -d -s cold-email './ops/watchdog.sh'

# 2. Add to System Settings → General → Login Items:
#    `tmux new-session -d -s cold-email '/full/path/to/ops/watchdog.sh'`
#    OR put a launchctl-load command in your .zprofile.

# 3. Optional: prevent Mac sleep while watchdog is alive
caffeinate -is &
```

Reattach to inspect:
```bash
tmux attach -t cold-email   # detach with CTRL-b then d
```

That gets you: auto-restart on crash + survives terminal close + survives lid open/close (with caffeinate). The only failure mode left is your Mac fully shutting down, which a one-line login item fixes.

## The harder, deeper truth

The framing *"AGI on the forge should be making this always on, intelligent, self-improving"* breaks into three layers — and Justin was right that one of them was missing:

- **Capability** — what the agent can do per turn. This is Claude + registered tools + prompts. Improved a lot this session (voice rules, validators, preview pack, update_brief / update_voice_rules tools).
- **Availability** — whether a process is running and listening. Plumbing. Now solid (watchdog) for crashes, one Login Items click from being solid for reboots.
- **Self-improvement** — does the system learn from each fix and apply lessons next time without you re-prompting? *This is what AGI-1 is for, and it's not yet wired into this repo.* See `AGI1_INTEGRATION.md` for the bootstrap path.

Forge is fine. Claude is fine. Watchdog is fine. The next real upgrade is bootstrapping AGI-1 on this repo so the bug-fix cycles compound into system memory.
