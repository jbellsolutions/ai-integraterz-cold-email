# ETHOS — What This Repo Will Always / Never Do

The mission is to land *real human replies* from cold email at scale, not to ship pretty campaigns. Every architectural choice serves that.

## Always

- **Fail loudly.** Every uncaught exception surfaces as a `:warning:` in Slack within seconds. Silent failure is the worst failure mode.
- **Gate destructive actions.** Anything that spends Smartlead credits, writes to a real campaign, or deletes data requires an explicit human "yes" in the same Slack thread.
- **Verify after fixing.** Every patch lands with a regression test. The same bug never gets to bite twice.
- **Voice over volume.** A 50-lead campaign with personalized copy beats a 5,000-lead blast. The slop critic + sales-pattern checks + URL gate + threading check are non-negotiable quality floors.
- **Cache deliberately.** Strategy briefs are cached per `(niche, offer, variant)` so re-runs stay apples-to-apples. Voice rules layer on top of every brief at runtime.
- **Capture capability gaps.** If the agent can't do something the operator asked, log it to `data/skill_gaps.jsonl` BEFORE saying "I can't". Recurring gaps become the next tools.
- **Treat the operator's voice as ground truth.** Justin's feedback on copy goes into `data/voice_rules.md` and propagates everywhere. The validators check for the patterns he banned.

## Never

- **Never sell in cold email.** Cold email opens a conversation. Pitches, programs, "free" hooks, and money/partner talk land only in the reply after the prospect engages.
- **Never put URLs in emails 1, 2, or 3.** Inbox-deliverability heuristics + reply-first protocol — the prospect's reply is the trigger for the link, not the cold open.
- **Never break threading.** Emails 2 and 3 reuse Email 1's subject line verbatim. Otherwise the follow-up reads like a fresh cold message.
- **Never ship without the operator's eye on previews.** `preview_emails` runs before any `schedule_campaign`. No exceptions, no automation that bypasses the human.
- **Never run autonomous tool synthesis.** AGI-1's iron law: *every fix must be verified by re-running the failing command.* The same applies to new tools — proposed by the agent, reviewed by the operator, then merged. We do not let LLMs ship `delete_all_campaigns` without review.
- **Never commit `.env`.** Ever. The `.gitignore` enforces it; if you ever find yourself thinking about it, stop.

## Quality Bar

A campaign is "ready to send" when:

1. The Strategy brief at `data/campaigns/<name>/brief.md` exists and is meaningfully different from sibling variants (A vs B vs C should walk through different doors to the same offer).
2. Per-prospect copy has been generated for every lead and saved to `data/emails/<email>.json`.
3. Each email passes all four validators: `slop_check`, `sales_check`, `url_check`, `threading_check`.
4. The operator has previewed at least 3 sample emails per campaign and explicitly said "launch" / "go" / "schedule".
5. Smartlead UI shows the campaign in DRAFTED state with leads + per-prospect custom fields populated.

If any of those are false, the campaign does not move to ACTIVE.

## What Counts as a Win

Not "100 emails sent today." A win is:

- A real positive reply from a recruiter who thinks the message read like a peer, not a vendor pitch.
- A reply daemon that catches an unsubscribe and silently honors it without bothering the operator.
- A bug that hit once, has a regression test, and never bites again.
- A capability gap logged this week that becomes a working tool next week.
- A heartbeat in `#cold-email-control` every 30 minutes saying *uptime: 24h:00m, pulse #48* — proof that the system was paying attention while the operator slept.

## What Counts as a Loss

- A "free recruiter stack" subject line shipping. (Voice rules failure.)
- A `$1K per placement` pitch in email 2. (Sales tone failure.)
- A daemon that died at 3am and nobody noticed until morning. (Watchdog failure.)
- The same bug fixed twice. (Test discipline failure.)
- An LLM tool-loop that ran 50 turns and burned $40 because there was no circuit breaker. (Risk awareness failure.)

The system's job is to make these losses observable, fixable in one motion, and not to repeat them. AGI-1 is the framework that propagates "fixable in one motion" across repos and across sessions.
