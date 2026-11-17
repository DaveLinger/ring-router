"""End-to-end: emulated ring requests against a live listener on a random port."""

import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from ringrouter import protocol
from ringrouter.notify import ConsoleNotifier
from ringrouter.server import App, make_handler
from ringrouter.store import Store


@pytest.fixture
def server(cfg, fake_fm):
    notifier = ConsoleNotifier(echo=False)
    app = App(cfg, store=Store(":memory:"), notifier=notifier, fm=fake_fm, background=False,
              mcp_call=lambda *a: pytest.fail("no MCP calls in dry run"))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    app.public_url = base
    yield base, app, notifier
    httpd.shutdown()


def post(url, headers, body):
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def send(base, text, **kw):
    h, b = protocol.build_request(text, "testsecret", **kw)
    return post(f"{base}/hook/testpath", h, b), (h, b)


def test_message_dry_run(server):
    base, app, notifier = server
    (code, text), _ = send(base, "Tell Terri I'm on my way.")
    assert code == 200 and "dry_run" in text and "I'm on my way." in text
    assert "Would send to Terri" in notifier.sent[-1].text


def test_ask_uses_rewrite(server, fake_fm):
    base, app, notifier = server
    (code, text), _ = send(base, "Ask Terri when she'll be home")
    assert code == 200 and "When will you be home?" in text and "rewritten" in text


def test_wrong_path_is_404(server):
    base, _, _ = server
    h, b = protocol.build_request("Tell Terri hi", "testsecret")
    assert post(f"{base}/hook/nope", h, b)[0] == 404


def test_bad_signature_401(server):
    base, _, _ = server
    (code, text), _ = send(base, "Tell Terri hi", bad_signature=True)
    assert code == 401 and "signature" in text


def test_expired_timestamp_401(server):
    base, _, _ = server
    (code, text), _ = send(base, "Tell Terri hi", ts=int(time.time()) - 3600)
    assert code == 401 and "expired" in text


def test_replay_is_duplicate(server):
    base, _, notifier = server
    (code, _), (h, b) = send(base, "Tell Terri hi")
    n = len(notifier.sent)
    code2, text2 = post(f"{base}/hook/testpath", h, b)
    assert code2 == 200 and "duplicate" in text2 and len(notifier.sent) == n


def test_test_event_runs_nothing(server):
    base, _, notifier = server
    h, b = protocol.build_request(None, "testsecret", trigger="test-event", is_test=True)
    assert post(f"{base}/hook/testpath", h, b) == (200, "test ok\n") and not notifier.sent


def test_no_transcription_discarded(server):
    base, _, _ = server
    h, b = protocol.build_request(None, "testsecret")
    code, text = post(f"{base}/hook/testpath", h, b)
    assert code == 200 and "discarded" in text


def test_audio_part_ignored(server):
    base, _, _ = server
    (code, text), _ = send(base, "Tell Terri hi", include_audio=True)
    assert code == 200 and "Terri" in text


def test_cancel(server):
    base, _, notifier = server
    (code, text), _ = send(base, "Tell Terri I'll be late. Scrap that.")
    assert code == 200 and text.startswith("cancelled") and notifier.sent[-1].title == "Cancelled"


def test_weak_match_needs_confirm_then_confirm(server):
    base, app, notifier = server
    (code, text), _ = send(base, "Tell Sherry I'm on my way")
    assert code == 200 and "needs_confirm" in text
    yes = notifier.sent[-1].buttons[0].url
    assert yes.endswith("/yes")
    code, text = post(yes, {}, b"")
    assert code == 200 and "dry_run" in text and "Terri" in text
    code, _ = post(yes, {}, b"")      # one-time
    assert code == 410


def test_unknown_recipient_refused(server):
    base, _, notifier = server
    (code, text), _ = send(base, "Tell Harry I'm on my way")
    assert code == 200 and text.startswith("refused") and "harry" in notifier.sent[-1].text


def test_timer_refused(server):
    base, _, notifier = server
    (code, text), _ = send(base, "Set a timer for 10 minutes")
    assert text.startswith("refused") and "Timers" in notifier.sent[-1].text


def test_shopping_list_split(server):
    base, _, notifier = server
    (code, text), _ = send(base, "Add milk, eggs and bread to my shopping list")
    assert code == 200 and "['Milk', 'Eggs', 'Bread']" in text


def test_unlock_runs_without_confirm_by_default(server, monkeypatch):
    from ringrouter import handlers
    from ringrouter.hubitat import Hubitat
    devs = [{"id": 67, "label": "Living Room Door", "room": "Living Room", "capabilities": ["Lock"]}]
    hub = Hubitat("x", "1", "t", fetch=lambda p: devs if p == "/devices/all" else {},
                  aliases={"front door": "Living Room Door"})
    monkeypatch.setattr(handlers, "get_hubitat", lambda ctx: hub)
    base, _, notifier = server
    (code, text), _ = send(base, "Unlock the front door")
    assert "needs_confirm" not in text and not notifier.sent[-1].buttons


def test_unlock_confirm_when_configured(server, cfg, monkeypatch):
    from ringrouter import handlers
    from ringrouter.hubitat import Hubitat
    devs = [{"id": 67, "label": "Living Room Door", "room": "Living Room", "capabilities": ["Lock"]}]
    hub = Hubitat("x", "1", "t", fetch=lambda p: devs if p == "/devices/all" else {},
                  aliases={"front door": "Living Room Door"})
    monkeypatch.setattr(handlers, "get_hubitat", lambda ctx: hub)
    base, _, notifier = server
    cfg.raw.setdefault("home", {})["confirm_actions"] = ["unlock", "open"]
    (code, text), _ = send(base, "Unlock the front door")
    assert "needs_confirm" in text and notifier.sent[-1].buttons
    (code, text), _ = send(base, "Lock the front door")
    assert "needs_confirm" not in text


# ------------------------------------------------- answer page: Make this change --

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def _answer_with_proposal(app, ttl=900):
    import json
    from ringrouter.handlers.claude_code import ANSWER_PROPOSAL_KEY
    from ringrouter.llm import Reply
    aid = app.llm_store.save_answer("claude_code", "Claude Code on RackNerd VPS", "can we ignore the 500s",
                                    Reply("Yes. [[PROPOSE: block CONNECT on 8080]]"))
    pid = app.store.add_pending("d1", "claude_code", {"host": "racknerd", "session": "s", "proposal": "x"}, ttl)
    app.store.set_kv(ANSWER_PROPOSAL_KEY.format(aid=aid), json.dumps({"pid": pid, "proposal": "block CONNECT on 8080"}))
    return aid, pid


def _get(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return r.status, r.headers, r.read().decode()


def test_answer_page_offers_make_this_change_while_waiting(server):
    base, app, _ = server
    aid, pid = _answer_with_proposal(app)
    code, headers, page = _get(f"{base}/answers/{aid}")
    assert "Make this change" in page and f'action="/confirm/{pid}/yes?from={aid}"' in page
    assert "block CONNECT on 8080" in page
    assert "form-action 'self'" in headers["Content-Security-Policy"]


def test_answer_page_cancel_goes_back_with_status_and_hides_the_button(server):
    base, app, _ = server
    aid, pid = _answer_with_proposal(app)
    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(f"{base}/confirm/{pid}/no?from={aid}", data=b"", method="POST")
    try:
        opener.open(req, timeout=10)
        assert False, "expected a redirect"
    except urllib.error.HTTPError as e:
        assert e.code == 303 and e.headers["Location"] == f"/answers/{aid}?r=cancelled"
    _, _, page = _get(f"{base}/answers/{aid}?r=cancelled")
    assert "Cancelled. Nothing was changed." in page and "Make this change" not in page


def test_expired_proposal_has_no_button(server):
    base, app, _ = server
    aid, _ = _answer_with_proposal(app, ttl=-1)
    _, _, page = _get(f"{base}/answers/{aid}")
    assert "Make this change" not in page


def test_plain_confirm_still_answers_text(server):
    base, app, _ = server
    _, pid = _answer_with_proposal(app)
    assert post(f"{base}/confirm/{pid}/no", {}, b"") == (200, "cancelled\n")
