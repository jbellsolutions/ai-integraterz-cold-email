# Write Rules — Direct Value Sequence

This file replaces `sequences.md` — there are no pre-written templates for direct-value angles. The agent generates fresh per send. These are the constraints.

## Hard rules (Approver auto-rejects on any miss)

1. **Word count under 130.** First-touch ≤110. Email 2 ≤130. Email 3 ≤110.
2. **Exactly one ask, in the form of a question.**
3. **At least one specific public_signal or trigger_event referenced.** Generic emails do not send.
4. **Subject line:** lowercase, 3-6 words, no punctuation except commas, target ≤33 chars, industry-specific noun if at all possible.
5. **No banned hype words** (see voice.md).
6. **No exclamation points, no emojis.**
7. **Microsoft-certified oversight referenced exactly once across the sequence** — never in the first email.
8. **At least one specific number per email** (AICO hourly $10-20, sprint length 30 days, productivity gain percent, dollars, hours).
9. **Industry-specific noun** present in body (and ideally subject).
10. **Sign-off:** `-- Justin` or `-- Justin Bellware`. No corporate footer in body.
11. **Audit booking link** (https://aiintegraterz.com/audit) must appear somewhere across the 3-email sequence.
12. **Do not quote price in cold.** If a reply asks, the response is "we'll scope it on the audit call."

## Sequence shape (3 emails)

The agent decides emails 1/2/3 per prospect. Emotional arc:

- **Email 1 — Curiosity.** Lead with the public_signal or trigger_event. Land the specific workflow_pain. Name what an AICO is + the 30-day sprint in plain terms. The ask is the audit call (or a 15-min strategy call for Expert Series, see sub-offer doc).
- **Email 2 — Conviction.** New information, not restatement. Reference Microsoft-certified oversight ONCE here OR in Email 3. Add the 30-Day Adoption Guarantee (refund if no daily AI use on day 30) sparingly — only when the email needs to address "what if it doesn't stick" objection.
- **Email 3 — Walk-away with grace.** Acknowledge silence. Recap the offer in one paragraph. Include the audit link if not already used. No artificial urgency.

## CTA shapes by sub-offer

| Sub-offer | CTA shape | Booking link |
|---|---|---|
| AICO (flagship) | Free Business Integration Audit Call (60-90 min, run personally by Justin) | https://aiintegraterz.com/audit |
| Expert Series | 15-min Free Strategy Call (lower friction — these prospects are time-poor) | https://aiintegraterz.com/audit |
| Sovereign Blueprint | Free 30-min Business Integration Audit (Justin routes to Sovereign Blueprint on the call if GTM-stack pattern matches) | https://aiintegraterz.com/audit |

The CTA must be **a question.** "Worth a 15-min call?" / "Want me to send the audit link?"

## Reply-handling guidance for Reply squad (Drafter + Approver)

When a prospect asks about price in a reply, the response is **always**: "We'll scope it on the audit call. AICO hourly is $10-20 once placed, mastermind is $500/mo cancel-anytime — placement + sprint fee depends on team size."

When a prospect asks "how is this different from a VA," reference: pre-trained AI-native talent (Claude/Codex/n8n/Make/Zapier stack), pre-built role kits per industry, Microsoft-certified oversight from Justin, 30-day adoption guarantee.

When a prospect raises the "what if my team doesn't use it" objection, the 30-Day Adoption Guarantee is the answer: "If the team is not actively using AI in daily workflows on Day 30, you do not pay. Full refund. No partial credits. No excuses."

## Self-check before send

```
[ ] under 130 words (110 for Email 1)
[ ] exactly one ask, phrased as a question
[ ] specific public_signal OR trigger_event referenced
[ ] subject line lowercase, 3-6 words, ≤33 chars
[ ] industry-specific noun in body
[ ] one specific number in body
[ ] no banned hype words
[ ] no "I hope this finds you well"
[ ] no em-dash rhythm (≤1, sign-off only)
[ ] audit link present somewhere in the sequence
[ ] Microsoft-certified oversight in this sequence exactly once (never first email)
[ ] no price quoted in cold
[ ] sign-off: -- Justin
```
