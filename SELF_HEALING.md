# Self-Healing & Skill Acquisition — What's Real, What Isn't

You asked: *"if you're using the SDK properly, the agent should be auto-analyzing the skills it needs and creating new ones automatically — self-healing, self-learning. If I'm correct, this stuff should be fixing itself over time."*

Here's the honest engineering answer.

## What's true

- **Tool-use loops** (what the Slack agent runs every turn) are a form of self-direction. The agent gets a list of tools, picks which to call, sees results, decides what to do next. That's already happening — every message you send hits a tool-use loop.
- **Skills** in Claude Code are real, but they are **author-time context files**, not runtime self-mutation. A skill at `~/.claude/skills/<name>/SKILL.md` is a markdown file Claude loads when relevant. They don't write themselves; humans (or other Claude sessions) author them.
- **Anthropic Agent SDK** gives the orchestrator capability to spawn sub-agents, register tools, persist memory across sessions. None of that is "creates new abilities on demand" — it's the substrate you build a self-improving system on top of.

## What isn't true (yet)

- The framing *"the agent auto-creates skills when I ask for something it doesn't have"* is, today, mostly a marketing-grade promise. Production systems that genuinely self-modify (write new tool code, hot-reload it, prove correctness) exist in research labs and are brittle in the wild. **No public Anthropic system does this generally.**
- "Self-learning" in the sense of *the agent gets better at your specific patterns over time without you re-prompting* requires either fine-tuning (expensive, slow, opaque) or a memory layer that surfaces past patterns into the system prompt. The latter is achievable; we don't have it yet.

## What we *can* build (and partially have)

The honest path to "this stuff fixes itself over time" is a layered set of mechanisms, ordered from cheapest/realest to most ambitious:

| Layer | What it does | Status today |
|---|---|---|
| **1. Visible failures** | Every error becomes a message in Slack so you see it instantly. No silent crashes. | ✅ Done — `_emit_error` posts every uncaught exception to `#cold-email-control`. |
| **2. Regression tests** | Each bug we hit becomes a test. The bug can't come back without breaking CI. | ✅ Started — `tests/test_slack_agent.py` covers the JSON-serialization bug we just fixed. Add one per bug going forward. |
| **3. Capability gaps logged** | When the agent gets a request it can't fulfill (no tool matches), log the request + what tool would've helped. | 🟡 Partial — the system prompt instructs the agent to say so. We don't yet write to a `data/skill_gaps.jsonl` file. Easy to add. |
| **4. Memory across sessions** | Past requests + outcomes feed into the system prompt so the agent remembers your patterns ("Justin always splits leads 50/50 across A and B"). | ❌ Not built. Would use `claude-mem` or a small Postgres + retrieval. |
| **5. Tool synthesis** | When a logged gap recurs, the agent (or you) drafts a new tool, you review, it gets added to the registry. **Human-in-the-loop**, not autonomous. | ❌ Not built. Achievable, but needs a code-review gate to avoid the agent writing destructive tools to itself. |
| **6. Fully autonomous skill creation** | Agent writes, tests, deploys new tools without human review. | ❌ Not building this, on purpose. Too much risk of an LLM writing a `delete_all_campaigns` tool with a typo. |

## What I'm wiring in now (concrete commitments)

To move us up that ladder without overpromising:

1. **Layer 1 (visible failures) is live as of this commit.** Every uncaught exception in `slack_agent` posts a `:warning:` to Slack with the error type + message. The bug pattern that hit you twice today (silent crash, no reply) cannot recur without you seeing it.

2. **Layer 2 (regression tests) is started.** `tests/test_slack_agent.py` has three tests: JSON serialization, content-block round-tripping, tool-registry consistency. Run with `python tests/test_slack_agent.py`. Whenever we fix a bug, we add a test for it.

3. **Layer 3 (capability gap log) — adding now.** When the agent sees a request it can't fulfill, it'll write a row to `data/skill_gaps.jsonl` with the timestamp, the user message, and the agent's best guess at what tool would've helped. Reviewable by you weekly; recurring gaps become my next-priority tools.

4. **Layer 4 (cross-session memory) — designing for next iteration.** The current per-thread state (`data/slack/threads/<ts>.json`) gets us in-conversation memory. Cross-session memory is the next layer — would surface "Justin's preferred niches", "his confirmation phrasing patterns", "campaigns with above-average reply rates and what's in their briefs" into every new system prompt.

## What you should expect, realistically

- **Bugs we've already seen:** stay fixed. Every one we patch gets a test.
- **Bugs we haven't seen:** still possible, but you'll see them in Slack within seconds, and we'll fix + test them.
- **Capabilities you ask for that don't exist:** the agent will say *"I don't have a tool for that — I can do X and Y, but not Z directly"*. The gap gets logged. I add the tool when you confirm it's worth building.

This is roughly the same shape as "self-healing software" in any honest engineering org: **invariants enforced by tests + observability + a fast feedback loop with the operator.** The "AI that writes its own code on demand" framing oversells what's actually shipping. What's actually shipping — when done right — is closer to *"a system where every failure is a one-shot education and the operator never sees the same problem twice."*
