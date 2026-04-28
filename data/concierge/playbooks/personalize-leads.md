# Playbook: Personalize Leads (default delivery path)

When Justin asks for personalized emails, the default is `personalize_to_csv`. Smartlead writes are the legacy path — only used when he explicitly asks.

## Triggers

- "personalize these leads"
- "give me the CSV"
- "I'll upload to Smartlead myself"
- "fan it across all 6 briefs"
- "do it" / "go" / "fire it" — when there's a lead source on the table

## Inputs

- **Lead handle** — must exist in `LEAD_HANDLES` or have been pre-loaded:
  - `ingest_csv_from_slack` after a Slack file upload
  - `load_leads_from_smartlead` from a source campaign id (his ~894 unique recruiter leads live in the TenXVA campaigns: 3103961, 3103959, 3085916, 3085917, 3080680)
  - `prospect_fetch_confirmed` after a Prospector search
- **Brief** — `<niche>-<offer>-<VARIANT>`. Six recruiter briefs already cached: `recruiters-{power-partner,direct-value,capstone}-{A,B}`.

## Decision tree

1. Lead source not specified + intent says "the leads" / "those leads" / "use the existing"?
   → Default source: union of TenXVA campaigns (recruiter leads). Pull, dedupe.

2. Brief not specified?
   → Ask once: which of the 6 briefs (or "all 6")? If user said "all" or "fan it out", call `spawn_subagent` with `children=[...]` (one personalize per brief).

3. Lead count and confirm?
   → Post one-line plan. "Personalizing 894 unique leads → `<brief>` CSV. ~30 min. Confirm?" Wait for yes.

4. On confirm → call `personalize_to_csv` (or `spawn_subagent` for fan-out). Returns task_id immediately.

5. Tell Justin the task_id and that the supervisor will post progress + the CSV when done.

## What "fan it across all 6" looks like

```python
spawn_subagent({
  "intent": "personalize 894 leads × 6 briefs in parallel → 6 CSVs",
  "children": [
    {"role": "p-pp-A",  "intent": "personalize → power-partner-A CSV",
     "tool_call": {"name": "personalize_to_csv",
                    "args": {"lead_handle": "<h>", "niche": "recruiters",
                              "offer": "power-partner", "variant": "A"}}},
    {"role": "p-pp-B",  "intent": "personalize → power-partner-B CSV",
     "tool_call": {"name": "personalize_to_csv",
                    "args": {"lead_handle": "<h>", "niche": "recruiters",
                              "offer": "power-partner", "variant": "B"}}},
    {"role": "p-dv-A",  "intent": "personalize → direct-value-A CSV", ...},
    {"role": "p-dv-B",  ...},
    {"role": "p-cs-A",  ...},
    {"role": "p-cs-B",  ...}
  ],
  "max_depth": 3,
  "timeout_seconds": 7200
})
```

The concierge's job is the spawn call, not the work. The supervisor + 6 worker subprocesses do the actual personalization in parallel. Each child posts its CSV to the same Slack thread when done.

## Failure modes (and what to say)

- **Lead handle missing.** Concierge says: "no lead handle on the table. Loading from your TenXVA campaigns now." Then calls `load_leads_from_smartlead`.
- **Brief missing.** Concierge says: "no cached brief for `<name>`. Running `precreate_campaigns` for that variant first." Then chains.
- **Worker stalled past 5 min.** Supervisor posts `:warning: still working ... last update <stage>`. Concierge does nothing — supervisor handles it.
- **Worker stalled past 15 min.** Supervisor retries once automatically. Tells Justin in-thread.
- **Two failures in a row.** Supervisor escalates with `<@JUSTIN>` ping — concierge surfaces in current thread + reads the worker log to summarize what broke.

## Quality gate (optional but recommended)

Before posting a CSV that contains hundreds of leads, queue a `council_review`:
```python
council_review({
  "criteria": [
    "voice rules compliance (data/voice_rules.md)",
    "no urls in any of email_1_body, email_2_body, email_3_body",
    "subject of email_2 and email_3 matches email_1 (threading)",
    "no banned phrases: 'free', '14-day sprint', 'AI sourcing agent', '$1k', '$150/mo'"
  ],
  "target": "data/exports/<csv_path>"
})
```
Council blocks → concierge posts the verdict + reasoning to Justin instead of celebrating.
