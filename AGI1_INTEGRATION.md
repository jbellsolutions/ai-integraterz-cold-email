# AGI-1 Integration — What's Installed, What's Missing, How to Bootstrap

## What AGI-1 is (correctly this time)

AGI-1 is a self-healing, self-learning, autorecursive AI development framework. Justin has it installed globally on this machine at `~/Desktop/Rethinking Repo's/agi-1/` (v2.2.1). It exposes 17 specialized skills via symlinks at `~/.claude/skills/agi-1/`:

| Skill | Purpose |
|---|---|
| `agi-main` | System-wide orchestrator. Builds a live state brief and routes to sub-commands. |
| `agi-heal` | Self-healing error fixer. Detects errors, matches against pattern DB, applies fixes, **verifies** them. Iron law: *never apply a fix without verifying it worked*. |
| `agi-learn` | Self-learning analysis engine. Reviews observations, extracts patterns, generates insights, applies self-modifications. Iron law: *every self-modification logged with before/after evidence*. |
| `agi-council` | 3 agents × 8 dimensions × 3 rounds of council critique. |
| `agi-auditor` | Dual scoring: G-Stack (0–100) + AI-Readiness (0–100) + Self-Healing (0–50) = 0–250. |
| `agi-genome-sync` | Cross-repo learning propagation. Patterns proven on one repo flow to the genome and into future repos. |
| `agi-tdd`, `agi-verifier`, `agi-debugger`, `agi-cleaner`, `agi-spec-reviewer`, `agi-walkthrough`, `agi-researcher`, `agi-skill-master`, `agi-dreamer`, `agi-orchestrator`, `agi-upgrade-l2` | The rest of the kit. |

Three-layer architecture (from `~/Desktop/Rethinking Repo's/agi-1/ARCHITECTURE.md`):

| Layer | Mode | Timescale | Trigger |
|---|---|---|---|
| **Healing** | Reactive | Seconds | Error occurs |
| **Learning** | Reflective | Sessions | Every N sessions |
| **Genome** | Propagative | Repos | Cross-repo sync |

Auto-learning fires on every failed Bash command (logged to `.claude/learning/observations.json`); every 10 sessions, a nudge writes to suggest `/agi-learn`.

## What's installed on THIS repo (cold-email-2.0)

Nothing. AGI-1 has not been bootstrapped here.

```
.claude/                        ← missing
.claude/agents/main-agent.md    ← missing
.claude/learning/observations.json  ← missing
.agent/agent.py                 ← missing (Level 2 standalone Python agent)
```

That's the bug. AGI-1 is installed globally but each repo opts in by running `/agi-1` once. The cold-email-2.0 repo has never had that run.

## What that explains about today

Today's bug-fix cycle ran *without* the AGI-1 healer/learner skills engaged:

- **Iteration 1** — Reactions wired but `reactions:write` scope missing → handled manually
- **Iteration 2** — JSON serialization crash on `TextBlock` → I noticed, fixed manually, added regression test manually
- **Iteration 3** — `--all` flag broken in Smartlead CLI → noticed manually, added pagination manually
- **Iteration 4** — Title nested in `custom_fields` → noticed manually
- **Iteration 5** — `{"pick": null}` crash → noticed manually, fixed, tested manually
- **Iteration 6** — `{"sequence": null}` crash → noticed manually, generalized helpers manually
- **Iteration 7** — Sales-tone violations not enforced → manually expanded `SALES_PATTERNS`
- **Iteration 8** — Retries didn't tell the model what failed → manually added violation injection

Eight iterations of manual whack-a-mole. With AGI-1's healer engaged, each error would have:
1. Been captured by a hook on the failed Bash/test
2. Matched against the pattern DB
3. Applied the fix
4. Re-run the failing command to verify
5. Logged the learning into `observations.json`

And the next session would inherit the patterns. That's the missing piece.

## How to bootstrap AGI-1 on this repo

**Option A — full pipeline (recommended):** open this repo in Claude Code, then run

```
/agi-1
```

That kicks off the 8-phase pipeline: dual audit → genome pull → council critique → implement gaps → autoresearch → re-score → genome push → final report. Cost-wise it's substantial — multiple Opus/Sonnet calls per phase — but it lands a baseline AI-Readiness + Self-Healing score and writes the `.claude/` scaffolding the other skills depend on. One-time investment, ongoing payoff.

**Option B — incremental:** if a full audit feels too heavy, run only the bootstrap-shaped skills first:

```
/agi-main          # one-shot system overview, no mutations
/agi-auditor       # just the scoring, no implementation
/agi-upgrade-l2    # install the persistent Python agent (.agent/agent.py)
```

Then `/agi-heal` and `/agi-learn` become available for incremental use as bugs come up.

**Option C — minimal:** just install the hook that captures Bash errors into observations:

```
/agi-skill-master install agi-heal
```

Subsequent failed commands feed the pattern DB, even without running the full pipeline.

## What the ideal integration looks like (proposed for after bootstrap)

Once `.claude/` exists on this repo:

1. **The Slack agent's `_emit_error` helper** (already shipped) writes Python tracebacks as observations into `.claude/learning/observations.json`. That feeds `agi-learn`.
2. **The capability-gap log** (`data/skill_gaps.jsonl`, already shipped) gets surfaced into `agi-learn` runs as candidates for new tools.
3. **The validators** (`slop_check`, `sales_check`, `url_check`, `threading_check`) become an example AGI-1 patterns the genome propagates to other LLM-driven repos.
4. **The watchdog log** (`logs/watchdog.log`) is the substrate for AGI-1's auto-restart counting and crash-loop pattern detection.

That's the connected version. The system you described.

## What I got wrong

In `ALWAYS_ON.md` I wrote *"AGI isn't real here"* — sloppy and wrong. AGI-1 is real, you set it up, I should have looked in `~/.claude/skills/` before writing. The corrected wording is now in `ALWAYS_ON.md`. Apologies — every honest engineer overshoots into "it's just plumbing" sometimes; doesn't excuse the laziness of not checking.
