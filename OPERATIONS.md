# Operations — How You Run This Every Day

This is the operator's manual. The architecture lives in [README.md](README.md); this document answers the only questions you should need day-to-day:

- **Who do I talk to?**
- **What do I do when I have new leads?**
- **What do I do when someone replies?**
- **How do I know it's working?**
- **What do I do when something breaks?**

---

## The mental model

You don't run code. You run **two Slack channels** and an **orchestrator agent** that lives in one of them.

```
┌─────────────────────────────────────────────────────────────────────┐
│                         YOU (in Slack)                              │
└──────────────────────┬───────────────────────┬──────────────────────┘
                       │                       │
       ┌───────────────▼─────────────┐   ┌────▼──────────────────────┐
       │   #cold-email-control       │   │   #cold-email-replies     │
       │   (you talk to the agent)   │   │   (agent talks to you)    │
       │                             │   │                           │
       │   • Drop CSVs               │   │   • Reply drafts to       │
       │   • "Find me 50 X leads"    │   │     approve / edit / skip │
       │   • "Stats"                 │   │   • Auto-handles OOO,     │
       │   • "Launch into <camp>"    │   │     unsubscribe silently  │
       └─────────────┬───────────────┘   └────────────┬──────────────┘
                     │                                │
       ┌─────────────▼───────────────────────────────▼──────────────┐
       │               Two long-running daemons                     │
       │                                                            │
       │  slack_agent.py    →  reads #cold-email-control,           │
       │                       runs Strategy → Research → Copy →    │
       │                       drafts campaigns into Smartlead      │
       │                                                            │
       │  reply_loop.py     →  polls Smartlead inbox every 60s,     │
       │                       drafts responses to every reply,     │
       │                       posts to #cold-email-replies         │
       └────────────────────────────────────────────────────────────┘
                     │
                     ▼
       ┌─────────────────────────────────────────────────────────────┐
       │  SMARTLEAD                                                  │
       │  • DRAFTED campaigns waiting for leads                      │
       │  • ACTIVE campaigns sending email                           │
       │  • Inbox where replies arrive                               │
       └─────────────────────────────────────────────────────────────┘
```

**Two daemons, two channels. Everything else is automatic.**

---

## Daily routine

### Morning (under 5 min)

1. **Open `#cold-email-replies`.** Any pinned threads that don't have your `send` / `skip` / edited body are pending. Clear the queue.
2. **Open `#cold-email-control`** and type `stats`. The agent posts a table: campaigns by `(niche, offer, variant)`, lead counts, replies pending, replies sent today.

That's it for the daily check. If both channels are clean, the system is healthy.

### When you have new leads (1–10 min depending on volume)

You have three ways to feed leads in. Pick whichever is convenient:

#### Path A — CSV upload (fastest if you have a file)

In `#cold-email-control`:
1. Drag the CSV into the chat box.
2. In the caption, say what to do: *"Split these evenly across `recruiters-power-partner-A`, `-B`, and `recruiters-direct-value-A`, `-B`. Confirm before launching."*
3. The agent ingests, validates the CSV, and replies with a count + a sample. It will ask: **"Launching N leads into <campaigns>. Confirm?"**
4. You say `yes` (or `proceed`, `go`, `confirm`).
5. The agent runs the pipeline in the background (~2 min/campaign because briefs are cached) and posts ✅ when done.

CSV columns required: `name, email, company, title`. `linkedin_url` optional. Extra columns are ignored.

#### Path B — Smartlead Prospector via natural language

In `#cold-email-control`, just describe who you want:

> *"Find me 50 recruiting agency owners in the US, 11–50 employees, founder/owner seniority."*

The agent runs a **free** Smartlead Prospector search, replies with the count, and asks before fetching (which spends Smartlead credits).

You then say:

> *"Yes, fetch all 50 and load into recruiters-power-partner-A and recruiters-power-partner-B 50/50."*

It fetches, finds emails, splits, and asks one more time before launching the pilot.

#### Path C — Smartlead saved search

If you've built a saved search in the Smartlead UI:

> *"Pull 30 leads from saved search 12345 into recruiters-direct-value-A."*

Same flow: the agent confirms count first, asks before fetching, asks before launching.

### When a campaign finishes drafting

After the agent says ✅ in `#cold-email-control`:
1. Open Smartlead UI → the campaign is in **DRAFTED** state with leads + sequences attached.
2. Review a few leads to spot-check the personalization.
3. Set a schedule (e.g. 8:00 AM local), then click "Start" — campaign moves DRAFTED → ACTIVE.

That's the only thing the agent doesn't do automatically. Manual review + manual launch is the safety floor.

### When replies come in

You don't open Smartlead. The reply daemon already polled the inbox.

For every reply, you'll see one of:
- **No notification** — the reply was OOO / unsubscribe / pure spam. Auto-handled. Silent for a reason: it's noise.
- **A new thread in `#cold-email-replies`** — agent drafted a response and wants approval. Read the thread:

```
🟢 Reply from Sarah Chen — positive (intent 8/10)
Lead: sarah@acme.com  •  Company: Acme Growth
Their reply: Sounds interesting, when can we chat?
Proposed draft ✅ PASS (9/10)
Subject: Re: Acme + AICO
[Drafted body]
↪ Reply in this thread: send to send as-is · skip to discard · or paste your edited body.
```

You reply **in the thread** with one of:
- `send` — daemon sends as-is via Smartlead
- `skip` — drop it, no reply sent
- *paste your rewritten body* — daemon sends what you wrote

The daemon handles each reply once. No double-sends, no missed threads.

---

## Who do I talk to?

| Want to do this | Where | What to say |
|---|---|---|
| Add new leads (CSV) | `#cold-email-control` | drop CSV, caption with target campaign(s) |
| Find new leads (Prospector) | `#cold-email-control` | "Find me X leads matching Y" |
| See what's running | `#cold-email-control` | `stats` |
| Read a campaign's brief | `#cold-email-control` | "show me the brief for `recruiters-power-partner-A`" |
| Pre-create new niche × offer combos | `#cold-email-control` | "pre-create campaigns for home-services on direct-value and capstone, A and B" |
| Archive a campaign | `#cold-email-control` | "archive campaign `<id>`" — agent will ask for explicit yes |
| Approve / edit / skip a reply | `#cold-email-replies` | reply in thread: `send`, `skip`, or paste edited body |
| Review draft campaigns before they send | Smartlead UI | review → set schedule → click Start |

The agent gates every credit-spending or destructive action behind explicit confirmation. Read-only commands (`stats`, `list briefs`, `read brief`) don't ask.

---

## How is it working?

### Healthy signals

- `#cold-email-control` agent responds to `stats` within ~10s.
- `#cold-email-replies` shows new threads within ~60s of a real reply landing in Smartlead.
- Smartlead UI shows DRAFTED campaigns with sensible per-prospect copy.

### Quick health check (run any time)

```bash
# Are both daemons alive?
ps aux | grep -E "(slack_agent|reply_loop)" | grep -v grep

# Tail the orchestrator log
tail -30 logs/slack_agent.log

# Tail the reply daemon
tail -30 nohup.out         # or wherever you redirected it
```

You should see two python processes — one per daemon.

### Stats command

In `#cold-email-control` send `stats`. Agent posts:

- **Campaigns table:** every Smartlead campaign by `(niche, offer, variant)`, status, lead count, replies, replies today.
- **Replies table:** total reply records, pending approval (Slack threads waiting for you), sent today.

This is your dashboard. There is no other dashboard.

---

## Pre-created campaigns (current state)

Six recruiter campaigns sit DRAFTED in Smartlead, briefs cached, ready to receive leads:

| Campaign | Offer angle | Hook (variant A) | Hook (variant B) |
|---|---|---|---|
| `recruiters-power-partner-A` / `-B` | Recruiter-stack + AICO partner referral | Recruiter stack first, partner side reveal in email 2 | Math/mechanism — "the same stack you'd 4× sourcing with is what your placed clients want installed" |
| `recruiters-direct-value-A` / `-B` | Direct AICO sale to the agency itself | Custom landing page + custom offer | Alternate framing |
| `recruiters-capstone-A` / `-B` | High-touch single-engagement rebuild | Free-expert-rebuild framing for ≥$1M agencies | Mechanism-not-money framing |

Add more with:

```bash
# In #cold-email-control:
"Pre-create campaigns for home-services on direct-value and expert-series, A and B"
```

Or from the CLI:
```bash
python -m orchestrator.precreate \
    --niche=home-services \
    --offers=direct-value,capstone \
    --variants=A,B
```

---

## What do I do when something breaks?

### The Slack agent stops responding

```bash
# Is it alive?
ps aux | grep slack_agent | grep -v grep

# If not, restart:
cd "Cold Email 2.0"
set -a && source .env && set +a
nohup .venv/bin/python -m orchestrator.slack_agent > logs/slack_agent.log 2>&1 &

# Check the log:
tail -50 logs/slack_agent.log
```

### Replies aren't appearing in `#cold-email-replies`

```bash
# Reply daemon alive?
ps aux | grep reply_loop | grep -v grep

# Restart if not:
cd "Cold Email 2.0"
set -a && source .env && set +a
nohup .venv/bin/python -m orchestrator.reply_loop > nohup.out 2>&1 &
```

### Agent says `channel_not_found`

The bot isn't in the channel, or your `SLACK_CONTROL_CHANNEL` env var is wrong. Either:
- Invite `@cold_email_20` (the bot) to the channel, **or**
- Use the channel ID (starts with `C` for public, `G` for private) instead of `#name` in `.env`. Get it from the channel's URL: `…/archives/C0123ABCD`.

### A campaign got created with the wrong copy

You can archive it from `#cold-email-control`:

> *"Archive campaign 3243617."* → agent will confirm, you say `yes`, it sets status to STOPPED (reversible).

To regenerate the brief and recreate, delete the cached brief first:

```bash
rm data/campaigns/<campaign-name>/brief.md
python -m orchestrator.precreate --niche=<n> --offers=<o> --variants=<v>
```

### A reply got auto-handled but I wanted to approve it

Auto-handling is silent only for OOO / unsubscribe / spam classes. If something landed in those buckets that shouldn't have, the misclassification is logged in `data/replies/<id>.json`. Look for the file, read `triage.reply_class`, and the original `inbound_reply` text. Adjust the triage prompt in `squads/reply/squad.py` if it's a recurring pattern.

### Smartlead Prospector returns 0 leads

The natural-language → filter translation might've picked filter values that don't match anything. Cache file is at `data/cache/prospect_filters.json`. Either:
- Refine your description (be more specific about industry / location / size)
- Build the saved search in the Smartlead UI yourself and pass the saved-search ID

---

## What I should NOT do

- Edit campaigns directly in Smartlead UI mid-pilot. The orchestrator's idempotent lookup-by-name will work, but if you rename a campaign in Smartlead, the next batch will create a *new* one with the original name.
- Commit `.env` to git. Ever. (Already in `.gitignore`.)
- Delete `data/campaigns/<name>/brief.md` casually. That's the cached strategy brief. Deleting it forces a regeneration ($$ + ~30s) and produces a *different* brief — the variant becomes apples-to-oranges across runs.
- Bypass the reply approval gate. The thread-reply protocol exists because pure-positive-classified replies are still the right place for a human eye.

---

## Cost model (rough)

- Pre-creating a campaign brief: ~$1.50 in Anthropic Opus (one-time, cached forever).
- Per lead through the full pipeline (Research + Copy + slop critic): ~$0.04–0.10 depending on offer / niche.
- Per reply drafted: ~$0.02 (Sonnet drafter + Haiku triage + Haiku approver).
- Smartlead credits: only `prospect fetch` consumes them; everything else (search, find-emails for already-fetched contacts, sending, replies) is included in your Smartlead plan.

For 1,000 outbound emails + 50 replies you're looking at $40–100 in API spend on top of Smartlead.

---

## TL;DR

1. **Every morning:** open `#cold-email-replies`, clear the thread queue. Type `stats` in `#cold-email-control`.
2. **New leads:** drop a CSV into `#cold-email-control` or describe the target audience.
3. **New replies:** auto-handled or land in `#cold-email-replies` for your `send` / `skip` / edit.
4. **New niche/offer:** ask the agent to pre-create campaigns for it.
5. **Something's wrong:** restart the daemon, check the log, ask the agent in Slack.

You should not be in the terminal during normal operation. If you are, something has fallen over and the runbook above tells you how to right it.
