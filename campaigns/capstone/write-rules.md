# Write Rules — Capstone Sequence

This file replaces `sequences.md` — there are no pre-written templates for the Capstone angle. The agent generates fresh per send. These are the constraints.

## Hard rules (Approver auto-rejects on any miss)

1. **Word count under 130.** First-touch ≤110. Email 2 ≤130. Email 3 ≤110.
2. **Exactly one ask, in the form of a question.**
3. **At least one specific public_signal or trigger_event referenced.** Generic emails (even on-voice) do not send.
4. **Subject line:** lowercase, 3-6 words, no punctuation except commas, target ≤33 chars.
5. **No banned hype words** (see voice.md).
6. **No exclamation points, no emojis.**
7. **Microsoft-certified oversight referenced exactly once across the sequence** — never in the first email.
8. **At least one specific number per email.**
9. **Industry-specific noun present** in body and ideally in subject.
10. **Sign-off:** `-- Justin` or `-- Justin Bellware`. No corporate footer in body.
11. **Capstone-specific:** the certification process URL (https://aiintegraterz.com/certificationjourney) must appear in some form across the 3-email sequence.
12. **No "free trial" language.** This is not a free trial of a paid thing.
13. **No promised follow-up sales.** Do not say "I'll follow up with X" unless the prospect asked.
14. **Honest about the timeline.** The Capstone runs 6-10 weeks, longer than the paid 30-day sprint. Do not bury this.

## Sequence shape

The agent decides emails 1/2/3 per prospect, but the emotional arc must be:

- **Email 1 — Curiosity + Honesty.** Lead with the public_signal or trigger_event. Name what we do (free expert work for the host, certification project for the candidate). The ask is the lowest-friction CTA option that fits this prospect.
- **Email 2 — Conviction.** Add ONE piece of new information (not restatement). Reference Microsoft-certified oversight ONCE here OR in Email 3 (not both). Add the certification process URL if not already in Email 1.
- **Email 3 — Walk-away with grace.** Acknowledge the silence. Recap the offer in one paragraph. Confirm there is no follow-up sales pressure. Honest after-relationship line if there's room.

## CTA options (agent picks per prospect)

- "Reply with the workflow you'd want help on" — lowest friction, default for Email 1
- "15-min scoping call" — when the public signal is strong and warrants synchronous time
- "Share the role you're hiring for and I'll see if it fits" — when active_job_posting is the trigger

Whichever is chosen, it must be a **question**.

## Closing-context line (Email 3 if there's room)

> "If you like the work and want more help, we can help you with that. That's not the goal of the Capstone. The goal is the project, the candidate's certification, and you getting your problem solved."

## Self-check before send

```
[ ] under 130 words
[ ] exactly one ask, phrased as a question
[ ] specific public_signal OR trigger_event referenced (verbatim quote or paraphrase + URL)
[ ] subject line lowercase, 3-6 words, ≤33 chars
[ ] industry-specific noun in body
[ ] one specific number in body
[ ] no banned hype words
[ ] no "I hope this finds you well"
[ ] no em-dash rhythm (≤1 em-dash, sign-off only)
[ ] cert URL present somewhere in the sequence (this email or another)
[ ] Microsoft-certified oversight in this sequence exactly once (this email or another, never first)
[ ] sign-off: -- Justin
```
