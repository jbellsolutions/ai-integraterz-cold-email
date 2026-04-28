"""Slack notification + interactive approval for reply handling.

Posts every drafted reply (after Approver PASS or FLAG) to a Slack channel as
a Block Kit message. Approval is via **thread-reply protocol** (the only
transport currently wired): user replies in the message thread with `send`,
`skip`, or any other text (treated as the edited body to send).

We previously rendered Send/Edit/Skip buttons too, but Slack rejects clicks
unless the app has a configured interactivity URL — which we don't have and
don't want to stand up a public webhook for. Buttons stripped; thread-reply
guidance is now the primary instruction.

Two transports, picked by what's configured:

  WEBHOOK + ACTIONS  Full interactive Block Kit. Requires a Slack app with
                     interactivity enabled and SLACK_BOT_TOKEN +
                     SLACK_SIGNING_SECRET in env. orchestrator/reply_loop.py
                     runs the action endpoint.

  MCP_THREAD         Posts a message via the connected Slack MCP server (the
                     one in user's tool list). User replies in thread with
                     'send' / 'skip' / '<edited body>'. reply_loop.py polls
                     the thread for new messages.

Defaults to MCP_THREAD when SLACK_BOT_TOKEN is unset (no app config required).

Mock mode: CE2_MOCK_SLACK=1 prints to stdout instead.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Literal

import httpx


def _mock_enabled() -> bool:
    return os.environ.get("CE2_MOCK_SLACK", "").strip() not in ("", "0", "false")


@dataclass
class SlackPing:
    channel: str
    ts: str           # message timestamp — used to thread replies
    transport: Literal["webhook", "mcp", "mock"]


class SlackClient:
    """Generic Slack Web API client used by the orchestrator agent.

    Three concerns:
      - read_channel(channel, oldest)         conversations.history
      - download_file(url_private)            files.url_private fetch
      - post(channel, text, thread_ts=None)   chat.postMessage

    All Web API calls. Auth via SLACK_BOT_TOKEN. Raises on Slack error rather
    than silently swallowing (the agent loop reports back to the user).
    """

    def __init__(self, bot_token: str | None = None):
        self.bot_token = bot_token or os.environ.get("SLACK_BOT_TOKEN", "")
        if not self.bot_token and not _mock_enabled():
            raise RuntimeError("SLACK_BOT_TOKEN is required for SlackClient")
        self.client = httpx.Client(timeout=30.0)
        self._self_user_id: str | None = None

    def _get(self, url: str, **kwargs):
        r = self.client.get(url, headers={"Authorization": f"Bearer {self.bot_token}"}, **kwargs)
        r.raise_for_status()
        return r

    def _post_json(self, url: str, payload: dict):
        r = self.client.post(url, headers={
            "Authorization": f"Bearer {self.bot_token}",
            "Content-Type": "application/json; charset=utf-8",
        }, json=payload)
        r.raise_for_status()
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"Slack API {url} failed: {data.get('error')} — {data}")
        return data

    def auth_test(self) -> dict:
        r = self._get("https://slack.com/api/auth.test")
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"Slack auth.test failed: {data.get('error')}")
        self._self_user_id = data.get("user_id")
        return data

    def whoami(self) -> str:
        if self._self_user_id:
            return self._self_user_id
        return self.auth_test().get("user_id", "")

    def read_channel(self, channel: str, oldest: str | None = None,
                       limit: int = 50) -> list[dict]:
        """Return list of messages newer than `oldest` (Slack ts). Bot's own
        messages are filtered out so the daemon doesn't reply to itself."""
        me = self.whoami()
        params = {"channel": channel, "limit": limit}
        if oldest:
            params["oldest"] = oldest
        r = self._get("https://slack.com/api/conversations.history", params=params)
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"conversations.history failed: {data.get('error')}")
        msgs = data.get("messages", [])
        # filter: drop bot's own + drop messages without text (e.g. join events)
        out = []
        for m in msgs:
            if m.get("user") == me or m.get("bot_id"):
                continue
            if m.get("subtype") in ("bot_message", "channel_join", "channel_leave"):
                continue
            out.append(m)
        # newest first from API → reverse to chronological
        return list(reversed(out))

    def download_file(self, url_private: str, dest_path) -> "Path":
        """Download a Slack-uploaded file using bot-token Authorization."""
        from pathlib import Path
        dest_path = Path(dest_path)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        r = self._get(url_private)
        dest_path.write_bytes(r.content)
        return dest_path

    def file_info(self, file_id: str) -> dict:
        """files.info → {url_private, name, mimetype, ...}."""
        r = self._get("https://slack.com/api/files.info", params={"file": file_id})
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"files.info failed: {data.get('error')}")
        return data.get("file", {})

    def post(self, channel: str, text: str, thread_ts: str | None = None,
              blocks: list | None = None) -> dict:
        payload = {"channel": channel, "text": text}
        if thread_ts:
            payload["thread_ts"] = thread_ts
        if blocks:
            payload["blocks"] = blocks
        return self._post_json("https://slack.com/api/chat.postMessage", payload)

    def add_reaction(self, channel: str, ts: str, name: str) -> dict | None:
        """Add an emoji reaction. Returns None on best-effort failure
        (missing scope, already_reacted, etc.) — reactions are UX polish,
        we never want them to break the main flow."""
        try:
            return self._post_json("https://slack.com/api/reactions.add",
                                     {"channel": channel, "timestamp": ts, "name": name})
        except Exception as e:
            err = str(e)
            if "already_reacted" in err:
                return None  # benign
            print(f"[slack] add_reaction({name}) failed: {e}", flush=True)
            return None

    def remove_reaction(self, channel: str, ts: str, name: str) -> dict | None:
        """Remove an emoji reaction. Best-effort like add_reaction."""
        try:
            return self._post_json("https://slack.com/api/reactions.remove",
                                     {"channel": channel, "timestamp": ts, "name": name})
        except Exception as e:
            err = str(e)
            if "no_reaction" in err:
                return None
            print(f"[slack] remove_reaction({name}) failed: {e}", flush=True)
            return None


class SlackNotifier:
    """Post reply-approval pings to Slack.

    Choose channel via SLACK_REPLY_CHANNEL env (default: #cold-email-replies).
    """

    def __init__(self):
        self.bot_token = os.environ.get("SLACK_BOT_TOKEN", "")
        self.channel = os.environ.get("SLACK_REPLY_CHANNEL", "#cold-email-replies")
        self.client = httpx.Client(timeout=15.0)

    def ping_for_approval(
        self,
        lead: dict,
        inbound_reply: str,
        original_outbound: str,
        triage: dict,
        draft: dict,
        approval: dict,
    ) -> SlackPing:
        """Post a single approval ping. Returns the message ts so reply_loop
        can poll the thread."""
        if _mock_enabled():
            print("\n=== MOCK SLACK PING ===")
            print(f"channel: {self.channel}")
            print(f"lead: {lead.get('name')} <{lead.get('email')}> @ {lead.get('company')}")
            print(f"triage: {triage.get('reply_class')} (intent={triage.get('intent_score')})")
            print(f"approver: {approval.get('verdict')} ({approval.get('score')}/10)")
            print(f"draft subject: {draft.get('subject')}")
            print("draft body:\n" + draft.get("body", ""))
            print("====================\n")
            return SlackPing(channel=self.channel, ts="mock-ts", transport="mock")

        blocks = self._build_blocks(lead, inbound_reply, original_outbound, triage, draft, approval)

        if self.bot_token:
            return self._post_webhook(blocks)
        # fall back to MCP — orchestrator/reply_loop.py drives the MCP call
        return self._post_via_mcp(blocks, lead, draft)

    # ---- transports ----------------------------------------------------

    def _post_webhook(self, blocks: list) -> SlackPing:
        """Post via Slack Web API (chat.postMessage). Requires SLACK_BOT_TOKEN."""
        r = self.client.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {self.bot_token}",
                     "Content-Type": "application/json; charset=utf-8"},
            json={"channel": self.channel, "blocks": blocks, "text": "Reply needs approval"},
        )
        r.raise_for_status()
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"Slack chat.postMessage failed: {data.get('error')}")
        return SlackPing(channel=data["channel"], ts=data["ts"], transport="webhook")

    def _post_via_mcp(self, blocks: list, lead: dict, draft: dict) -> SlackPing:
        """When no SLACK_BOT_TOKEN, the orchestrator dispatches the post via
        the connected Slack MCP server (slack_send_message tool). This method
        just stages the payload — the orchestrator picks it up and fires the
        MCP call.

        We write a queued-message file that orchestrator/reply_loop.py consumes.
        """
        from pathlib import Path

        queue_dir = Path(__file__).resolve().parents[1] / "data" / "replies" / "_slack_queue"
        queue_dir.mkdir(parents=True, exist_ok=True)
        slug = (lead.get("email", "") or "unknown").replace("@", "_").replace(".", "_")
        path = queue_dir / f"{slug}.json"
        payload = {
            "channel": self.channel,
            "blocks": blocks,
            "lead": lead,
            "draft": draft,
            "fallback_text": (
                f"Reply from {lead.get('name', 'unknown')} ({lead.get('email', '')}) "
                f"— see thread to send/edit/skip."
            ),
        }
        path.write_text(json.dumps(payload, indent=2))
        return SlackPing(channel=self.channel, ts=f"queued:{path.name}", transport="mcp")

    # ---- block builders ------------------------------------------------

    @staticmethod
    def _build_blocks(lead, inbound, outbound, triage, draft, approval) -> list:
        """Slack Block Kit. Includes the inbound, the draft, and 3 buttons
        (when interactivity is configured) or thread-reply guidance otherwise.
        """
        intent = triage.get("intent_score", 0)
        cls = triage.get("reply_class", "?")
        emoji = {
            "positive": ":green_circle:",
            "objection": ":yellow_circle:",
            "question": ":large_blue_circle:",
            "soft_no": ":white_circle:",
            "spam": ":black_circle:",
        }.get(cls, ":grey_question:")

        approver_verdict = approval.get("verdict", "?")
        approver_emoji = ":white_check_mark:" if approver_verdict == "PASS" else ":warning:"

        return [
            {"type": "header", "text": {"type": "plain_text",
                "text": f"{emoji} Reply from {lead.get('name', 'unknown')} — {cls} (intent {intent}/10)"}},
            {"type": "section", "fields": [
                {"type": "mrkdwn", "text": f"*Lead:*\n{lead.get('email', '')}"},
                {"type": "mrkdwn", "text": f"*Company:*\n{lead.get('company', '')}"},
            ]},
            {"type": "section", "text": {"type": "mrkdwn",
                "text": f"*Their reply:*\n>{inbound[:600].replace(chr(10), chr(10) + '>')}"}},
            {"type": "divider"},
            {"type": "section", "text": {"type": "mrkdwn",
                "text": f"*Proposed draft* {approver_emoji} {approver_verdict} "
                        f"({approval.get('score', 0)}/10)"}},
            {"type": "section", "text": {"type": "mrkdwn",
                "text": f"*Subject:* `{draft.get('subject', '')}`\n```{draft.get('body', '')}```"}},
            {"type": "context", "elements": [
                {"type": "mrkdwn", "text": f":bulb: {draft.get('rationale', '')}"},
            ]},
            {"type": "context", "elements": [
                {"type": "mrkdwn",
                 "text": ":arrow_right: *Reply in this thread:* `send` to send as-is · "
                          "`skip` to discard · *or paste your edited body* (we'll send what you wrote)."},
            ]},
        ]
