"""Reply Squad — three-agent gated pipeline for inbound reply handling.

The pipeline (per inbound reply):

  1. AUTO-HANDLE      Deterministic regex pre-filter for OOO + unsubscribe.
                       These bypass LLMs entirely and post NO Slack ping
                       (silent handling — no notification noise).

  2. TRIAGE  (Haiku)   Classifies into one of:
                         positive   → real interest, wants to engage
                         objection  → not interested but engaged
                         question   → asking something specific
                         soft_no    → bad timing, maybe later
                         spam       → broken, irrelevant, or auto-bounce
                       Returns intent_score 0-10 and the key signal.

  3. DRAFTER (Sonnet)  Reads the original outbound, the inbound reply, the
                       campaign brief, and the offer doc. Writes a personalized
                       response that fits voice.md (no slop). Single draft, not
                       a council — speed matters more than diversity here.

  4. APPROVER (Haiku)  Gate. Reads the draft and scores it 0-10 on:
                         - voice match
                         - factual accuracy (no fabrication)
                         - conversion likelihood
                         - safety (no spam-trigger phrases, compliant)
                       Outputs PASS / FAIL / FLAG. Only PASS drafts go to Slack
                       for human approval. FAIL/FLAG queue for human review
                       with the reason exposed.

  5. NOTIFY            Slack DM with: original outbound, inbound reply, draft,
                       triage class + score, approver verdict + reason.
                       User taps Send / Edit / Skip in Slack — that triggers
                       smartlead.reply_to_thread() for Send/Edit, drops to
                       data/replies/<id>.skipped.json for Skip.

The full pipeline + Slack ping target is <30s from inbound landing in
Smartlead. Polling cadence is 60s by default (configurable). Webhook upgrade
path documented in tools/smartlead.py:upsert_webhook.
"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from forge import Consensus, Spawner, SwarmSpec, ToolRegistry, Topology

from .._base import REPO_ROOT, load_routing


# ---- types ---------------------------------------------------------------

class ReplyClass(str, Enum):
    POSITIVE = "positive"
    OBJECTION = "objection"
    QUESTION = "question"
    SOFT_NO = "soft_no"
    OOO = "ooo"
    UNSUBSCRIBE = "unsubscribe"
    SPAM = "spam"
    UNKNOWN = "unknown"


@dataclass
class TriagedReply:
    reply_class: ReplyClass
    intent_score: int          # 0-10, how likely a real conversation
    key_signal: str            # one-line summary of what they said
    auto_handled: bool = False # True when the deterministic pre-filter caught it


@dataclass
class DraftedReply:
    subject: str
    body: str
    rationale: str             # why this draft, for the approver and the human


@dataclass
class ApprovalVerdict:
    verdict: str               # "PASS" | "FAIL" | "FLAG"
    score: int                 # 0-10
    reason: str
    flagged: list[str] = field(default_factory=list)  # specific concerns


# ---- deterministic pre-filter --------------------------------------------

_OOO_PATTERNS = [
    "out of office", "out-of-office", "ooo until", "on vacation",
    "on annual leave", "on parental leave", "automatic reply",
    "auto-reply", "automated reply", "currently away",
]
_UNSUB_PATTERNS = [
    "unsubscribe me", "remove me from", "stop emailing", "take me off",
    "do not contact", "please remove", "opt out", "opt-out",
    "unsubscribe please", "please unsubscribe",
]


def auto_filter(body: str) -> ReplyClass | None:
    """Cheap regex check. Returns OOO/UNSUBSCRIBE if matched, else None.

    These two classes are auto-handled silently:
      - OOO       → drop a snooze, no reply, no Slack ping
      - UNSUB     → call leads unsubscribe via CLI, no reply, no Slack ping
    """
    lower = body.lower().strip()

    # Bare-word unsubscribe — Outlook etc. often prefix the reply with just "unsubscribe"
    # before the quoted thread. After HTML strip, the dominant content can be that one word.
    # Heuristic: if the first non-empty line is essentially "unsubscribe" / "remove" / "stop",
    # treat as unsub regardless of what follows (quoted thread, sig, etc.).
    first_line = next((l.strip() for l in lower.splitlines() if l.strip()), "")
    bare_unsub_tokens = {"unsubscribe", "unsub", "remove", "stop", "no", "quit"}
    if first_line in bare_unsub_tokens:
        return ReplyClass.UNSUBSCRIBE

    if any(p in lower for p in _UNSUB_PATTERNS):
        return ReplyClass.UNSUBSCRIBE
    if any(p in lower for p in _OOO_PATTERNS):
        return ReplyClass.OOO
    return None


# ---- agents --------------------------------------------------------------

TRIAGE_INSTRUCTIONS = """You are the Reply Triage agent.

Read ONE inbound reply (with its original outbound for context) and classify it. Be concise.

Classifications:
  positive   — real interest, wants more info, asks to chat, says yes
  objection  — engaged but skeptical (price, timing, fit, trust)
  question   — asking something specific that needs a real answer
  soft_no    — bad timing, maybe later, polite decline
  spam       — broken, irrelevant, auto-bounce, gibberish

Output format (JSON only, no prose):
{
  "reply_class": "<one of: positive|objection|question|soft_no|spam>",
  "intent_score": <0-10, where 10 = ready to book a call right now>,
  "key_signal": "<one short sentence: what did they actually say>"
}"""

DRAFTER_INSTRUCTIONS = """You are the Reply Drafter for Cold Email 2.0.

You receive: the inbound reply, the original outbound that triggered it, the
triage classification, and the campaign brief (positioning, offer, voice). Your
job is to write ONE response that converts curiosity into a booked call.

Hard rules:
  - Plain text. No HTML, no bold, no emoji.
  - <120 words.
  - Mobile-formatted (short paragraphs, blank lines).
  - No "I hope this finds you well", no "circle back", no flattery, no em-dashes.
  - Sign off "-- Justin"
  - For positive: confirm the next step, propose a specific time, attach calendar link.
  - For objection: acknowledge, address the specific concern with one fact, soft re-ask.
  - For question: answer directly, short, then offer the call.
  - For soft_no: respect, leave the door open with one specific future hook.

Output format (JSON only):
{
  "subject": "<reply subject — usually 'Re: <original>'>",
  "body": "<the reply body>",
  "rationale": "<one sentence: why this draft works for this lead>"
}"""

APPROVER_INSTRUCTIONS = """You are the Reply Approver — the safety gate.

Read the proposed draft + the inbound reply + the campaign voice rules. Score
the draft on four axes (0-10 each):

  voice_match     — does it sound like the brand voice in voice.md?
  factual         — every claim is grounded in the campaign brief, no fabrication
  conversion      — does this draft actually move the lead toward booking?
  safety          — no spam triggers, no over-promises, no compliance risk

Output verdict:
  PASS   — all axes ≥ 7. Send to Slack for human approval.
  FLAG   — any axis 5-6. Send to Slack with the flag exposed for human review.
  FAIL   — any axis < 5. Do NOT send. Queue for human rewrite.

Output format (JSON only):
{
  "verdict": "PASS|FLAG|FAIL",
  "score": <weighted 0-10>,
  "reason": "<one sentence>",
  "flagged": ["<specific concerns>"]
}"""


# ---- the squad -----------------------------------------------------------

class ReplySquad:
    def __init__(self, brief: str, voice_md: str = ""):
        self.brief = brief
        self.voice_md = voice_md
        self.tools = ToolRegistry()

    async def process(self, original_outbound: str, inbound_reply: str) -> dict[str, Any]:
        """Full pipeline on one reply. Returns a result dict to be logged + sent to Slack."""
        # 1. Deterministic pre-filter
        auto = auto_filter(inbound_reply)
        if auto is not None:
            return {
                "auto_handled": True,
                "reply_class": auto.value,
                "intent_score": 0,
                "key_signal": f"auto-handled: {auto.value}",
                "draft": None,
                "approval": None,
            }

        # 2. Triage
        triage = await self._triage(original_outbound, inbound_reply)

        # spam → log, no draft, no Slack
        if triage.reply_class == ReplyClass.SPAM:
            return {
                "auto_handled": True,   # silent
                "reply_class": triage.reply_class.value,
                "intent_score": triage.intent_score,
                "key_signal": triage.key_signal,
                "draft": None,
                "approval": None,
            }

        # 3. Draft
        draft = await self._draft(original_outbound, inbound_reply, triage)

        # 4. Approve
        approval = await self._approve(draft, inbound_reply)

        return {
            "auto_handled": False,
            "reply_class": triage.reply_class.value,
            "intent_score": triage.intent_score,
            "key_signal": triage.key_signal,
            "draft": asdict(draft),
            "approval": asdict(approval),
        }

    # internals ----------------------------------------------------------

    async def _triage(self, original: str, reply: str) -> TriagedReply:
        routing = load_routing()
        spawner = Spawner(
            tools=self.tools,
            base_instructions=TRIAGE_INSTRUCTIONS + f"\n\nCAMPAIGN BRIEF:\n{self.brief}",
            max_turns=2,
        )
        spec = SwarmSpec(topology=Topology.SOLO, consensus=Consensus.QUEEN,
                          members=[routing["critic"]])  # haiku
        task = f"ORIGINAL OUTBOUND:\n{original}\n\nINBOUND REPLY:\n{reply}"
        result = await spawner.run(task, spec)
        text = result.members[0][1].final_text if result.members else "{}"
        data = _safe_json(text, default={})
        try:
            cls = ReplyClass(data.get("reply_class", "unknown"))
        except ValueError:
            cls = ReplyClass.UNKNOWN
        return TriagedReply(
            reply_class=cls,
            intent_score=int(data.get("intent_score", 0) or 0),
            key_signal=str(data.get("key_signal", "")),
        )

    async def _draft(self, original: str, reply: str, triage: TriagedReply) -> DraftedReply:
        routing = load_routing()
        spawner = Spawner(
            tools=self.tools,
            base_instructions=DRAFTER_INSTRUCTIONS
                + f"\n\nCAMPAIGN BRIEF:\n{self.brief}\n\nVOICE GUIDE:\n{self.voice_md}",
            max_turns=3,
        )
        spec = SwarmSpec(topology=Topology.SOLO, consensus=Consensus.QUEEN,
                          members=[routing["hook"]])  # sonnet — drafter wants quality
        task = (
            f"ORIGINAL OUTBOUND:\n{original}\n\n"
            f"INBOUND REPLY:\n{reply}\n\n"
            f"TRIAGE: class={triage.reply_class.value} intent={triage.intent_score} signal={triage.key_signal}"
        )
        result = await spawner.run(task, spec)
        text = result.members[0][1].final_text if result.members else "{}"
        data = _safe_json(text, default={})
        return DraftedReply(
            subject=str(data.get("subject", "")),
            body=str(data.get("body", "")),
            rationale=str(data.get("rationale", "")),
        )

    async def _approve(self, draft: DraftedReply, inbound: str) -> ApprovalVerdict:
        routing = load_routing()
        spawner = Spawner(
            tools=self.tools,
            base_instructions=APPROVER_INSTRUCTIONS
                + f"\n\nCAMPAIGN BRIEF:\n{self.brief}\n\nVOICE GUIDE:\n{self.voice_md}",
            max_turns=2,
        )
        spec = SwarmSpec(topology=Topology.SOLO, consensus=Consensus.QUEEN,
                          members=[routing["critic"]])
        task = (
            f"INBOUND REPLY:\n{inbound}\n\n"
            f"PROPOSED DRAFT:\nSubject: {draft.subject}\n\n{draft.body}\n\n"
            f"DRAFTER RATIONALE: {draft.rationale}"
        )
        result = await spawner.run(task, spec)
        text = result.members[0][1].final_text if result.members else "{}"
        data = _safe_json(text, default={})
        return ApprovalVerdict(
            verdict=str(data.get("verdict", "FAIL")),
            score=int(data.get("score", 0) or 0),
            reason=str(data.get("reason", "")),
            flagged=list(data.get("flagged", []) or []),
        )


def _safe_json(text: str, default: Any) -> Any:
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return default
    try:
        return json.loads(m.group(0))
    except Exception:
        return default
