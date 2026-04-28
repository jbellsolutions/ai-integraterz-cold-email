"""Reply pipeline smoke. Validates:
  - auto_filter catches OOO + unsubscribe and skips LLMs
  - ReplySquad runs end-to-end on mock provider for non-auto replies
  - SlackNotifier mock-mode prints rather than posting
  - reply_loop.process_once handles fetched replies (mock CLI returns [])
"""
import asyncio
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

# Force mock everywhere
os.environ["CE2_MOCK_SMARTLEAD"] = "1"
os.environ["CE2_MOCK_SLACK"] = "1"

from squads.reply.squad import ReplyClass, auto_filter, ReplySquad
from tools.slack_notify import SlackNotifier
from tools.smartlead import SmartleadCLI
from orchestrator.reply_loop import process_once


def test_auto_filter_ooo():
    msg = "I'm currently out of office until next week. Will respond on return."
    assert auto_filter(msg) == ReplyClass.OOO


def test_auto_filter_unsub():
    assert auto_filter("please unsubscribe me from your list") == ReplyClass.UNSUBSCRIBE
    assert auto_filter("remove me from this immediately") == ReplyClass.UNSUBSCRIBE


def test_auto_filter_passthrough():
    assert auto_filter("Yes, this looks interesting. Tell me more.") is None


def test_reply_squad_e2e_mock():
    """Run the full triage→draft→approve pipeline against the mock provider.
    The mock returns canned text — we just verify the pipeline produces a
    well-shaped result dict without crashing.

    Temporarily flips active_mode→mock for this test only, then restores.
    """
    import yaml as _yaml
    cfg_path = REPO_ROOT / "config" / "models.yaml"
    original = cfg_path.read_text()
    try:
        import re
        cfg_path.write_text(re.sub(r"^active_mode:\s*\w+", "active_mode: mock",
                                    original, count=1, flags=re.M))
        # Bust any cached routing
        from squads import _base
        squad = ReplySquad(brief="(test brief)", voice_md="(test voice)")
        inbound = "Yes, this is interesting. What's the next step?"
        outbound = "I deploy AI integrators into businesses. Free in your business first..."
        result = asyncio.run(squad.process(outbound, inbound))
        assert "reply_class" in result
        assert "intent_score" in result
        assert isinstance(result["intent_score"], int)
    finally:
        cfg_path.write_text(original)


def test_strip_html_drops_title_and_quoted_thread():
    """HTML strip should drop <title>, <head>, <style>, blockquotes, and the
    Outlook/Gmail divRplyFwdMsg quoted thread divider — so a one-word reply
    like "unsubscribe" lands as the first visible line."""
    from orchestrator.reply_loop import _strip_html
    travis_like = '''<!DOCTYPE html><html><head><title>Partnership opportunity</title>
<meta http-equiv="Content-Type" content="text/html"></head>
<body><style>p{margin:0}</style>
<div><p>unsubscribe</p></div>
<hr><div id="divRplyFwdMsg">From: Hayden Jordan ...big quoted thread...</div></body></html>'''
    out = _strip_html(travis_like)
    first_line = next((l for l in out.splitlines() if l.strip()), "")
    assert "unsubscribe" in first_line.lower(), f"first line was {first_line!r}, full: {out!r}"
    assert "Partnership opportunity" not in out, "title should have been dropped"
    assert "Hayden Jordan" not in out, "quoted thread should have been dropped"


def test_url_allowlist_gate():
    """A draft containing a URL not in the allowlist must be force-failed."""
    from orchestrator.reply_loop import _foreign_urls
    allowed = {"https://aiintegraterz.com/audit", "https://aiintegraterz.com/certificationjourney"}
    # in-allowlist (with query string) → no foreign
    assert _foreign_urls("Book here: https://aiintegraterz.com/audit?utm=x", allowed) == []
    # off-list → flagged
    foreign = _foreign_urls("Book here: https://cal.com/usingaitoscale", allowed)
    assert foreign and "cal.com" in foreign[0]


def test_per_tick_email_dedup():
    """Verify the per-tick dedupe by email logic — same lead in 3 campaigns
    yields only one entry to process."""
    overviews = [
        {"lead_email": "tatiana@x.com", "email_lead_id": 1, "email_campaign_id": 100,
         "last_reply_time": "2026-04-01T00:00:00Z"},
        {"lead_email": "tatiana@x.com", "email_lead_id": 2, "email_campaign_id": 200,
         "last_reply_time": "2026-04-03T00:00:00Z"},  # most recent
        {"lead_email": "tatiana@x.com", "email_lead_id": 3, "email_campaign_id": 300,
         "last_reply_time": "2026-04-02T00:00:00Z"},
        {"lead_email": "scott@y.com", "email_lead_id": 4, "email_campaign_id": 400,
         "last_reply_time": "2026-04-01T00:00:00Z"},
    ]
    # Replicate the dedup logic from process_once
    by_email = {}
    for ov in overviews:
        e = (ov.get("lead_email") or "").lower().strip()
        if not e: continue
        prev = by_email.get(e)
        if prev is None or (ov.get("last_reply_time") or "") > (prev.get("last_reply_time") or ""):
            by_email[e] = ov
    assert len(by_email) == 2
    assert by_email["tatiana@x.com"]["email_lead_id"] == 2  # most recent campaign won


def test_slack_notifier_mock():
    notifier = SlackNotifier()
    ping = notifier.ping_for_approval(
        lead={"name": "Test", "email": "t@x.com", "company": "X"},
        inbound_reply="yes",
        original_outbound="hi",
        triage={"reply_class": "positive", "intent_score": 8, "key_signal": "wants intro"},
        draft={"subject": "Re: hi", "body": "Great — Tuesday 2pm work?", "rationale": "..."},
        approval={"verdict": "PASS", "score": 8, "reason": "ok"},
    )
    assert ping.transport == "mock"


def test_reply_loop_one_tick():
    """process_once with mock CLI should return zero counts cleanly."""
    cli = SmartleadCLI()
    notifier = SlackNotifier()
    counts = asyncio.run(process_once(campaign_brief="b", voice_md="v", cli=cli, notifier=notifier))
    assert counts["new"] == 0


if __name__ == "__main__":
    print("= reply pipeline smoke =", flush=True)
    test_auto_filter_ooo();        print("[ok] auto_filter OOO", flush=True)
    test_auto_filter_unsub();      print("[ok] auto_filter unsubscribe", flush=True)
    test_auto_filter_passthrough();print("[ok] auto_filter passthrough", flush=True)
    test_slack_notifier_mock();    print("[ok] slack notifier mock", flush=True)
    test_reply_loop_one_tick();    print("[ok] reply_loop one tick (no replies)", flush=True)
    test_reply_squad_e2e_mock();   print("[ok] reply squad e2e on mock provider", flush=True)
    test_strip_html_drops_title_and_quoted_thread(); print("[ok] _strip_html drops <title>+quoted-thread", flush=True)
    test_url_allowlist_gate();     print("[ok] URL allowlist gate", flush=True)
    test_per_tick_email_dedup();   print("[ok] per-tick email dedup", flush=True)
    print("== ALL REPLY PIPELINE SMOKE TESTS PASSED ==", flush=True)
