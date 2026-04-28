# Direct Value Audience

## Universal personalization inputs

Before any cold email is generated, the Research squad mines for these fields. The email must reference at least ONE specific public_signal or trigger_event — generic emails (even on-voice) do not send.

| Field | What it is | Where to find it |
|---|---|---|
| `first_name` | Recipient's first name | Enrichment, LinkedIn, public bio |
| `company` | Business name | Domain, LinkedIn company page |
| `audience_noun` | Specific noun for the recipient's industry (DSO, recruiter, multi-provider practice, brokerage, agency) | AI Integraterz niche map |
| `workflow_pain` | Concrete operational workflow we know they likely struggle with | Industry pain map; site copy; recent posts |
| `public_signal` | Real, specific public signal — a hire, a posting, a launch, a recent post | LinkedIn, X, blog, press releases |
| `trigger_event` | Change of state worth referencing — expansion, new hire, new product, rebrand | News, LinkedIn announcements, careers page |
| `active_job_posting` | Live job posting an AICO could substitute for or augment | LinkedIn Jobs, careers page |
| `recent_content` | Recent post or piece of content from the prospect that invites a relevant comment | LinkedIn, Substack, podcast feed |
| `tech_signal` | Tools they've adopted (Instantly, Smartlead, HubSpot) — useful for Sovereign Blueprint targeting | BuiltWith, public stack mentions |

**Quality bar:** the email must reference at least one specific `public_signal` or `trigger_event`. Generic emails do not send.

## Universal disqualifying signals (do not email)

The Research/Strategy stage suppresses any prospect that hits one or more:

- Pre-revenue startups with no team or workflow to integrate
- Solo individuals with no business entity
- Anyone who has previously unsubscribed from any AI Integraterz list
- Industries on the AI Integraterz "do not pitch" list (gambling, adult, restricted cannabis, weapons)
- Domains with active manual penalties or visible deliverability issues
- Junior individual contributors with no buying authority
- **Sovereign Blueprint specifically**: companies under 10 employees — wrong sizing
- **Expert Series specifically**: companies over 50 employees — wrong sizing

## Per-sub-offer triggers

See [offers/aico.md](offers/aico.md), [offers/expert-series.md](offers/expert-series.md), [offers/sovereign-blueprint.md](offers/sovereign-blueprint.md) for sub-offer-specific pain points and triggers. The Strategy squad uses those to confirm the picked sub-offer fits the lead.
