# Cold Email Best Practices
> Distilled from 24M cold emails sent, 53K calls booked, 3-5% avg reply rate.
> Source: Jay Feldman, Lead Genius — "8 Cold Email Tactics That Still Work in 2026"

---

## The Mastery Triangle
Cold email lives on three pillars. **Missing any one caps you at 1%.**
1. **Deliverability** — inbox placement >80%. DNS, warmup, plain text, no fingerprints.
2. **List** — right humans, right time, right offer. Decision makers only.
3. **Copy** — get open → get read → get reply. Short, human, non-salesy.

Audit all three before changing a word of copy.

---

## 8 Tactics That Still Work

### 1. Email Decision Makers Only
CEOs, founders, CMOs, owners, presidents. Skip managers, coordinators, specialists.
- Mid-level: forward-to-boss (dies) or delete (no authority) → wasted send + spam risk
- Founder: reply or spam. That's it. **Doubles reply rates.**

### 2. Lead Qualification (P0)
Run every lead through qualification BEFORE importing to sending platform.
- Without it: 1-2% reply. With it: 4-6%.
- Expect to lose ~50% of list — the 20% you remove are the ones that would ghost or spam-report.
- Use Clay AI, N8N, or custom Python (`/opt/data/scripts/qualify_leads.py`).

### 3. Spam Folder Mining
Find people sending you cold email that landed in spam → tell them why → offer help.
- 20-30% reply rate. Highest conversion play in existence.
- Pitch: "Hey, your cold email landed in my spam. Here's why. Want help?"

### 4. Reverse Lead Magnet
Do work FIRST before asking. "Would it be okay if I spent an hour generating this for you?"
- Instead of "download my PDF" → "I built this custom audit for you"
- Perceived value much higher because they think it's unique
- 3-5% reply rate, top campaigns 7%+

### 5. Trojan Horse Framing
Frame outreach as non-sale. Must be genuine — not bait and switch.
- Journalist interview, partnership exploration, research project, case study feature
- 10-20% reply rates when frame is real

### 6. Diversify Sending Infrastructure
- Under 50 mailboxes → all Google (current best mid-2026)
- 500+ mailboxes → split Google / Microsoft / private SMTP
- Google update Nov 2025: overnight placement dropped 80→50%. Diversification protects.

### 7. Triple Tap Copy
Three jobs of every cold email:
1. **Get open** — subject + preview text, 3-5 words, sound human, never salesy
2. **Get read** — short body (4 sentences): why you → poke pain → social proof → ask
3. **Get reply** — CTA answerable with one thumb. "Yes." "Sure." "Worth a chat?"

### 8. No Fingerprints
- ❌ No links in email 1-3
- ❌ No open tracking pixels
- ❌ No link tracking
- ❌ No images or HTML
- ❌ No promotional trigger words (guarantee, 100%, free, act now)
- ✅ Plain text only

---

## Two Tactics to Kill

### ❌ Volume over quality
Bad list amplifies failure. Add qualification step first, then scale.

### ❌ Links + tracking in emails
Calendly links, open pixels, link trackers — all fingerprints that ESPs use to blacklist.

---

## Current Implementation Status

| Tactic | Status |
|---|---|
| Decision makers only | ✅ PipelineLabs filter |
| Lead qualification | ✅ `qualify_leads.py` |
| Triple tap copy | ✅ Approved sequence |
| No fingerprints (links/tracking) | ✅ Plain text, DONT_TRACK_* |
| Short subject (3-5 words) | ✅ "your AI person" |
| Simple CTA | ✅ "Worth learning how it works?" |
| Diversified infra | 📋 Justin's call |
| Email verification | ⚠️ Run before upload |
| Spam word audit | ✅ Copy passes |
| Trojan horse framing | 🔮 Test later |
| Reverse lead magnet | 🔮 Test later |
| Spam folder mining | 🔮 Test later |
