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


def test_unlock_runs_without_confirm_by_default(server):
    base, _, notifier = server
    (code, text), _ = send(base, "Unlock the front door")
    assert "needs_confirm" not in text and not notifier.sent[-1].buttons


def test_unlock_confirm_when_configured(server, cfg):
    base, _, notifier = server
    cfg.raw.setdefault("home", {})["confirm_actions"] = ["unlock", "open"]
    (code, text), _ = send(base, "Unlock the front door")
    assert "needs_confirm" in text and notifier.sent[-1].buttons
    (code, text), _ = send(base, "Lock the front door")
    assert "needs_confirm" not in text
