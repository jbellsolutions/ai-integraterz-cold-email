---
name: cold-email-launch-pilot
preamble-tier: 2
version: 1.0.0
description: |
  Run the full Strategy → Research → Copy → Smartlead pipeline against a
  set of leads and load them into a pre-existing campaign in Smartlead
  DRAFTED state. Use when an operator says "launch" / "load these leads
  into <campaign>" / "run the pilot for X" after they have explicitly
  confirmed in the same Slack thread.
  For previewing copy before launch, use cold-email-preview-pack instead.
allowed-tools:
  - Bash
  - Read
  - Write
  - Edit
---

# Launch Pilot — Operator-Confirmed Lead Load

## Iron Law

**NEVER LAUNCH WITHOUT EXPLICIT OPERATOR CONFIRMATION IN THE SAME SLACK THREAD.**
"Launch all 6" / "go" / "yes proceed" must appear in the most recent operator message.
A previous turn's confirmation does not carry forward — re-confirm if any time has passed
or any other tool has fired in between.

This iron law is also enforced programmatically by `orchestrator/slack_agent.py:tool_launch_pilot`
via the system prompt's "destructive-action gate" rule, but the SKILL.md here is the
documented contract that cannot be regressed by a model update.

---

## Phase 1: Preconditions

Before invoking the pipeline, verify:

1. **Lead handle exists**: the operator has either uploaded a CSV (resulting in a
   `lead_handle` from `ingest_csv_from_slack`), pulled from Smartlead Prospector
   (`prospect_fetch_confirmed`), or pulled leads from an existing campaign
   (`load_leads_from_smartlead`). The handle must be in `LEAD_HANDLES`.

2. **Target campaign exists**: the `<niche>-<offer>-<VARIANT>` is one of the
   pre-created campaigns (see `data/campaigns/*/brief.md`). If the brief is
   absent, refer the operator to `precreate_campaigns` first.

3. **Operator confirmation**: the most recent operator message contains an
   explicit affirmative ("yes" / "go" / "launch" / "proceed" / "confirmed").
   Implicit consent ("ok let's see what happens") is NOT enough.

If any precondition fails, STOP. Do not invoke `launch_pilot`. Tell the operator
what's missing.

---

## Phase 2: Execute

Call `launch_pilot(niche, offer, variant, lead_handle)` via the Slack agent's
tool registry. The function:

1. Spawns a background `asyncio.Task`
2. Looks up cached brief at `data/campaigns/<niche>-<offer>-<VARIANT>/brief.md`
3. Runs Research squad in parallel across all leads (max 10 concurrent)
4. Runs Copy squad sequentially (semaphore=8) with 4 validators + 3 retries
5. Calls `SmartleadSquad.build_campaign(...)` which:
   - Looks up the Smartlead campaign by name
   - Creates it if missing (saves sequence template)
   - Adds leads with per-prospect `custom_fields` carrying their copy
6. Posts ✅ to the originating Slack thread when done (or :x: with error)

The whole pipeline is idempotent: re-running with the same `lead_handle`
into the same `<niche>-<offer>-<VARIANT>` will append leads to the existing
Smartlead campaign without duplicating it or clobbering the sequence.

---

## Phase 3: Verify

After completion, the SKILL caller (or the operator) should:

1. Wait for the ✅ confirmation in the originating thread
2. Call `preview_emails(campaign_name, n=3)` to read 3 sample drafts
3. Spot-check at least one lead in the Smartlead UI:
   - Lead is present
   - `custom_fields.email_1_subject` and `custom_fields.email_1_body` are populated
   - Sequence template references `{{email_1_subject}}` / `{{email_1_body}}`

Only after this verification should the operator move to `schedule_campaign`.

---

## Stop Conditions

Stop and report (do NOT proceed) if:

- No explicit confirmation in the most recent operator message
- `lead_handle` is missing or empty
- Cached brief is missing (offer the operator the option to `precreate_campaigns` for the missing variant)
- A previous `launch_pilot` is still in flight for the same campaign (avoid race; check `RUNNING_PILOTS`)
- Smartlead CLI returns a non-2xx on `campaigns list` during the lookup phase (likely auth/network — fix root cause first)

---

## Why this is a Skill, not just a function

Three reasons:

1. The iron law (operator confirmation) is the ONE rule that cannot be regressed
   by a prompt update or a model change. Documenting it as a skill makes the
   contract auditable across the whole AGI-1 ecosystem.
2. The skill can be invoked by AGI-1 agents in OTHER repos that also handle
   "launch destructive batch operation behind operator confirmation" — the
   pattern is reusable.
3. The skill's stop conditions become checks `agi-heal` can match against
   when this skill fails, so failures get auto-classified and learned from.
