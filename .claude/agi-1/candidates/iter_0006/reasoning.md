# iter_0006 — Research (proactive hypothesis scan)

Iteration 6 of /agi-1's autoresearch is the /agi-research phase: scan
observations, synthesize patterns, propose hypotheses to test, write findings
to observations.json + insights.

## What was done
1. Persisted 9 observations from today's session into .claude/learning/observations.json (with frequency, fix_commit, root_cause, regression_test fields)
2. Synthesized 5 grounded insights, each citing specific obs IDs (iron law: trace_evidence required)
3. Identified 3 candidates for genome promotion (Phase 7 will check the confidence gate)

## Insights generated
- **ins-null-pattern** — LLM JSON null-default crash; affects any .get(k, []) site
- **ins-retry-must-feedback** — retries must inject violations or model replays same output
- **ins-validators-need-bare-word-regex** — phrase lists miss standalone keywords
- **ins-supervision-not-llm-tech** — always-on is plumbing
- **ins-check-installed-before-dismissing** — verify ~/.claude/skills/ before saying X doesn't exist

## Score impact
+5 to Self-Healing/Learning bonus: Learning System now has observations.json
populated (was empty), evolution.json template populated, and one completed
learning cycle (this iteration). Insights already include confidence scores
ready for genome push.

## Hypotheses for next session
1. Audit all `.get(k, default)` patterns across the codebase for the null-bug
2. Apply violation-feedback to Reply Squad drafter
3. Move repo out of ~/Desktop to enable launchd supervision
