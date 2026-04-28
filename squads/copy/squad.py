"""Copy Squad — generates the 3-email sequence per prospect.

Three specialists, sequential per prospect (not parallel — the body needs the hook):
1. Hook Writer (Sonnet, the model spend that earns the open)
2. Body + CTA Writer (Haiku, constrained by the chosen hook)
3. AI-Slop Critic (Haiku, regex + LLM gate, hard rejects on AI tells)

Per-prospect output: data/emails/<lead_id>.json with
  { lead_id, sequence: [ {step, subject, body}, x3 ], picked_hook, slop_pass }
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from forge import Consensus, Spawner, SwarmSpec, ToolRegistry, Topology

from .._base import REPO_ROOT, load_routing
from ..research.squad import Lead

HOOK_INSTRUCTIONS = """You are the Hook Writer. The first 1-2 lines of a cold email \
are where 80% of replies are won or lost. Your only job is to draft 3 candidate \
hook lines for ONE prospect, given a campaign brief and a per-prospect signal.

Rules:
- Each hook ≤ 2 sentences.
- Reference the signal SPECIFICALLY (the post, the hire, the launch). Generic \
hooks ("noticed your great work") are forbidden.
- No "I hope this finds you well", no flattery, no em-dashes.
- Different angle on each candidate (one direct, one specific-observation, \
one curiosity-question).

Output format (JSON only):
{ "candidates": [ "hook 1", "hook 2", "hook 3" ], "pick": <0|1|2>, "why": "..." }"""

BODY_INSTRUCTIONS = """You are the Body + CTA Writer. Given a chosen hook, the \
campaign brief (which includes positioning, offer, voice rules, and the \
canonical sequences.md template), and a prospect signal, write the full 3-email \
sequence.

# THE FUNDAMENTAL FRAME (read this first — every other rule serves it)

A cold email is the START of a conversation, NOT a pitch.

You are not selling. You are opening a door. You are an operator like them \
who has something interesting and wants to know if they want to hear about it. \
That's the whole frame. If your draft reads like marketing, like a program, \
like a "free offer" — rewrite it. If it reads like an operator messaging another \
operator with a curious observation and a low-friction question — ship it.

# HARD RULES

- *No links in emails 1, 2, or 3*. Not the first email, not the second, not \
the third. Calendar links, website links, one-pagers — NONE. Links go in the \
*reply* after the prospect has answered AND asked. (See "Why no links" below.)
- *Emails 2 and 3 MUST reuse Email 1's subject line verbatim* so they thread \
together in the prospect's inbox. If the subject changes, the thread breaks \
and the follow-up reads like a separate cold email — fatal. Use the SAME string.
- *Don't sell*. Don't say "free", don't pitch a "program", don't promise \
specific timeframes ("14-day sprint", "6-10 weeks") UNLESS the campaign brief \
explicitly approves the number for this audience. Promised timeframes feel \
like fake-urgency salsa.
- *No "AI sourcing agent" framing* unless the brief says otherwise. The default \
language across recruiter campaigns is "AI built solutions" / "an AI [role]" / \
"AI techs, VAs, chief officers" — match the brief's voice.
- *Email 1: ≤125 words*, no links, no bold, no HTML, reply-based CTA only \
("worth a 12-min Loom?", "want me to send the breakdown?", "useful?").
- *Email 2: ≤150 words*, no links. Reference the first email lightly without \
"just following up" or "circling back" (slop). New angle, same subject line.
- *Email 3: ≤125 words*, no links. Soft permission close — "ok to assume this \
isn't a fit?" / "want me to drop it?" — never demanding.
- Mobile-formatted (short paragraphs, blank lines between).
- Sign off "-- Justin"
- Subject lines (Email 1 only): target ≤33 chars, max 60. Lowercase or sentence \
case, no Title Case, no emoji.

# Why no links

Cold inboxes have heuristics that downrank messages with URLs in the first few \
exchanges. Spam filters, Gmail tabs, and prospects themselves treat link-in-cold \
as marketing/automation. The reply-first protocol is what gets human responses. \
The prospect's reply is the trigger for the link, not your initial outreach.

Output format (JSON only):
{ "sequence": [
    {"step": 1, "subject": "<short subject>", "body": "<email 1 body>", "delay_days": 0},
    {"step": 2, "subject": "<EXACTLY the same subject as step 1>", "body": "<email 2 body>", "delay_days": 3},
    {"step": 3, "subject": "<EXACTLY the same subject as step 1>", "body": "<email 3 body>", "delay_days": 7}
] }"""

# Phrases that prove an LLM didn't think — hard ban.
SLOP_PHRASES = [
    "i hope this email finds you well",
    "i hope this finds you well",
    "i noticed your",
    "your great work",
    "circle back",
    "deep dive",
    "let's connect",
    "let's hop on",
    "synergize", "synergy",
    "leverage", "leveraging",
    "just following up",
    "wanted to reach out",
    "touching base",
    "touch base",
    "game-changing", "game changing",
    "revolutionary",
    "cutting-edge", "cutting edge",
    "unlock the potential",
    "in today's fast-paced",
    "i'd love to learn more about",
    "i came across your profile",
]


def slop_check(text: str) -> tuple[bool, list[str]]:
    """Deterministic slop pre-filter. Returns (passes, list_of_violations)."""
    lower = text.lower()
    hits = [p for p in SLOP_PHRASES if p in lower]
    # em-dash check (— and -- are ok in sign-off; flag — used as ChatGPT rhythm)
    em_count = lower.count("—")
    if em_count > 1:
        hits.append(f"em-dash×{em_count} (ChatGPT rhythm tell)")
    return (len(hits) == 0, hits)


# Patterns that indicate selling-not-conversation tone. Justin's rule:
# cold email starts a conversation; pitches/offers/money-talk land in the
# REPLY after the prospect engages, not in the cold open or follow-ups.
SALES_PATTERNS = [
    # Fake timeframes
    "14-day sprint", "14 day sprint",
    "30-day sprint", "30 day sprint",
    "6-10 weeks", "6 to 10 weeks", "six to ten weeks",
    # "Free" framing — Justin: never in the hook
    "for free", "completely free", "absolutely free",
    "free to install", "free to deploy", "free recruiter",
    "install for free", "give them to you for free",
    # Default product framing he banned
    "ai sourcing agent",
    # Program-talk
    "this program", "our program",
    "limited spots", "limited time",
    "act now", "don't miss",
    # Money/partner talk in cold emails — should be in REPLY only
    "$1k", "$1,000", "$150/mo", "$150 per month",
    "per placement", "rev share", "rev-share",
    "revenue share", "revenue-share",
    "partner deal", "partner thing",
    "monthly deposit", "monthly recurring",
    "commission", "kickback", "referral fee",
]

FREE_WORD_RE = re.compile(r"\bfree\b", re.IGNORECASE)


def sales_check(text: str) -> tuple[bool, list[str]]:
    """Catches selling-not-conversation tone. Returns (passes, violations).
    Banned: fake timeframes, 'free' in hooks/bodies, money/partner talk
    (which belongs in the post-reply conversation, not in cold emails)."""
    lower = text.lower()
    hits = [p for p in SALES_PATTERNS if p in lower]
    # The bare word "free" — Justin: never in a cold email. Period.
    if FREE_WORD_RE.search(text):
        hits.append("bare-word 'free' (use only after their reply)")
    return (len(hits) == 0, hits)


URL_RE = re.compile(r"https?://\S+|www\.\S+|\b[a-z0-9.-]+\.(com|io|ai|co|net|org)\b/\S+",
                       re.IGNORECASE)


def url_check(sequence: list[dict]) -> tuple[bool, list[str]]:
    """Justin's rule: NO links in emails 1, 2, or 3. Returns (passes, violations)."""
    violations: list[str] = []
    for step in sequence:
        si = step.get("step", "?")
        body = step.get("body", "") or ""
        urls = URL_RE.findall(body)
        if urls:
            violations.append(f"email {si} contains URL(s): {urls}")
    return (len(violations) == 0, violations)


def threading_check(sequence: list[dict]) -> tuple[bool, list[str]]:
    """Emails 2 and 3 must reuse email 1's subject so they thread."""
    if len(sequence) < 2:
        return True, []
    subjects = [(s.get("step", i + 1), (s.get("subject") or "").strip())
                  for i, s in enumerate(sequence)]
    s1 = subjects[0][1]
    violations = [f"email {i} subject differs from email 1 ('{s}' ≠ '{s1}')"
                   for i, s in subjects[1:] if s != s1]
    return (len(violations) == 0, violations)


def _load_voice_rules() -> str:
    """Load Justin's global voice rules. Applied on TOP of every campaign brief.
    Updates here propagate to the next preview / launch — no per-campaign edits
    needed when the rules change."""
    p = REPO_ROOT / "data" / "voice_rules.md"
    if p.exists():
        return p.read_text()
    return ""


class CopySquad:
    def __init__(self, brief: str):
        # Voice rules go on top of brief — they override the brief if conflicts.
        voice = _load_voice_rules()
        self.brief = (
            (f"# JUSTIN'S VOICE RULES (override the campaign brief if conflicts)\n\n{voice}\n\n---\n\n"
              if voice else "")
            + brief
        )
        self.tools = ToolRegistry()

    async def write_one(self, lead: Lead, signal: dict, max_retries: int = 3) -> dict:
        """Hook → body → all checks (slop + sales + URLs + threading), with
        retry that injects the failed violations into the next prompt so the
        model knows specifically what to fix."""
        hook = await self._draft_hook(lead, signal)
        all_violations: list[str] = []
        for attempt in range(max_retries + 1):
            # On retry, feed violations to the body squad. If the violations
            # mention the hook (sales-tone in the subject or hook patterns),
            # also re-roll the hook with the violation feedback.
            sequence = await self._draft_body(lead, signal, hook,
                                                previous_violations=all_violations or None)
            seq_list = _safe_list(sequence.get("sequence"))
            full_text = "\n".join((e.get("subject") or "") + "\n" + (e.get("body") or "") for e in seq_list)

            slop_pass, slop_hits = slop_check(full_text)
            sales_pass, sales_hits = sales_check(full_text)
            url_pass, url_hits = url_check(seq_list)
            thread_pass, thread_hits = threading_check(seq_list)

            all_passed = slop_pass and sales_pass and url_pass and thread_pass
            all_violations = (
                [f"slop: {h}" for h in slop_hits]
                + [f"sales-tone: {h}" for h in sales_hits]
                + [f"url-rule: {h}" for h in url_hits]
                + [f"threading: {h}" for h in thread_hits]
            )

            # Re-roll hook if the failure looks hook-driven (sales/slop in
            # the subject or first body line). Cheap insurance.
            if not all_passed and attempt < max_retries:
                first_subj_body = ((seq_list[0].get("subject") or "") + " "
                                     + (seq_list[0].get("body") or "")[:200]) if seq_list else ""
                hook_implicated = any(
                    p in first_subj_body.lower() for p in
                    ("free", "14-day", "ai sourcing agent", "$1k", "rev share")
                )
                if hook_implicated:
                    hook = await self._draft_hook(lead, signal,
                                                     previous_violations=all_violations)

            if all_passed:
                out = {
                    "lead_id": lead.email,
                    "sequence": seq_list,
                    "picked_hook": _pick_candidate(hook),
                    "slop_pass": True,
                    "slop_attempts": attempt + 1,
                }
                self._save(lead, out)
                return out
        # Final attempt failed — still save with flag and full violation list
        out = {
            "lead_id": lead.email,
            "sequence": seq_list,
            "picked_hook": _pick_candidate(hook),
            "slop_pass": False,
            "slop_violations": all_violations,
            "slop_attempts": max_retries + 1,
        }
        self._save(lead, out)
        return out

    async def _draft_hook(self, lead: Lead, signal: dict,
                            previous_violations: list[str] | None = None) -> dict:
        routing = load_routing()
        spawner = Spawner(
            tools=self.tools,
            base_instructions=HOOK_INSTRUCTIONS + f"\n\nCAMPAIGN BRIEF:\n{self.brief}",
            max_turns=3,
        )
        spec = SwarmSpec(topology=Topology.SOLO, consensus=Consensus.QUEEN, members=[routing["hook"]])
        task = f"PROSPECT: {lead.name} ({lead.title}) at {lead.company}\nSIGNAL: {json.dumps(signal)}"
        if previous_violations:
            task += (
                "\n\nPREVIOUS ATTEMPT FAILED VALIDATION. The earlier hook produced "
                "downstream copy with these violations — your hook is contributing. "
                "Pick a meaningfully different angle this time, especially avoiding "
                "the patterns called out below:\n- "
                + "\n- ".join(previous_violations[:8])
            )
        result = await spawner.run(task, spec)
        text = result.members[0][1].final_text if result.members else "{}"
        return _safe_json(text, default={"candidates": [""], "pick": 0})

    async def _draft_body(self, lead: Lead, signal: dict, hook: dict,
                            previous_violations: list[str] | None = None) -> dict:
        routing = load_routing()
        spawner = Spawner(
            tools=self.tools,
            base_instructions=BODY_INSTRUCTIONS + f"\n\nCAMPAIGN BRIEF:\n{self.brief}",
            max_turns=4,
        )
        spec = SwarmSpec(topology=Topology.SOLO, consensus=Consensus.QUEEN, members=[routing["body"]])
        chosen = _pick_candidate(hook)
        task = (
            f"PROSPECT: {lead.name} ({lead.title}) at {lead.company}\n"
            f"SIGNAL: {json.dumps(signal)}\n"
            f"CHOSEN HOOK (must open Email 1 with this or a close variant): {chosen}"
        )
        if previous_violations:
            task += (
                "\n\nPREVIOUS ATTEMPT FAILED VALIDATION. Fix EVERY violation below; "
                "do not produce a draft that contains any of these patterns:\n- "
                + "\n- ".join(previous_violations[:12])
                + "\n\nIn particular: re-read the voice rules at the top of the brief. "
                "If you used 'free', '$1K', 'rev share', or any sales/program/timeframe "
                "language, REMOVE it. Cold emails open conversations, money/partner-talk "
                "lands only in the reply after they engage."
            )
        result = await spawner.run(task, spec)
        text = result.members[0][1].final_text if result.members else "{}"
        return _safe_json(text, default={"sequence": []})

    @staticmethod
    def _save(lead: Lead, out: dict) -> None:
        out_dir = REPO_ROOT / "data" / "emails"
        out_dir.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^a-zA-Z0-9]+", "_", lead.email)
        (out_dir / f"{safe}.json").write_text(json.dumps(out, indent=2))


def _safe_list(v) -> list:
    """Coerce LLM output to a list. Handles None, dict, scalar."""
    if isinstance(v, list):
        return v
    return []


def _pick_candidate(hook: dict) -> str:
    """Pick the chosen hook candidate. Defends against {"pick": null} or
    out-of-range indices (the LLM emits both occasionally)."""
    candidates = _safe_list(hook.get("candidates")) or [""]
    pick = hook.get("pick")
    if not isinstance(pick, int):
        pick = 0
    pick = max(0, min(pick, len(candidates) - 1))
    return candidates[pick] if candidates else ""


def _safe_json(text: str, default: dict) -> dict:
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return default
    try:
        return json.loads(m.group(0))
    except Exception:
        return default
