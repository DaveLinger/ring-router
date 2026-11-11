"""Activity page + per-delivery trace."""

import urllib.error
import urllib.request

from test_server import post, send, server  # noqa: F401  (reuse the live-listener fixture)


def get(url):
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def test_trace_records_every_phase(server):  # noqa: F811
    base, app, _ = server
    (code, _), (h, _b) = send(base, "Ask Terri when she'll be home")
    did = h["X-Index-Delivery"]
    phases = [s["phase"] for s in app.store.get_trace(did)]
    for p in ("received", "transcript", "cleaned", "route", "on_device_model", "message_composed",
              "notification", "result", "response"):
        assert p in phases, (p, phases)
    composed = next(s for s in app.store.get_trace(did) if s["phase"] == "message_composed")["data"]
    assert composed["spoken"] == "when she'll be home" and composed["text"] == "When will you be home?"
    route = next(s for s in app.store.get_trace(did) if s["phase"] == "route")["data"]
    assert route["route"] == "imessage" and route["params"]["rewrite"] == "ask_question"
    received = next(s for s in app.store.get_trace(did) if s["phase"] == "received")["data"]
    assert received["trigger"] == "single-click-hold" and received["signature"] == "valid"


def test_activity_pages(server):  # noqa: F811
    base, app, _ = server
    (_, _), (h, _b) = send(base, "Tell Terri I'm on my way")
    did = h["X-Index-Delivery"]
    code, page = get(f"{base}/activity/acttoken")
    assert code == 200 and "Tell Terri I&#x27;m on my way" in page and did in page
    code, page = get(f"{base}/activity/acttoken/{did}")
    assert code == 200 and "Route decision" in page and "Message composed" in page and "Response to the Pebble app" in page


def test_activity_needs_token(server):  # noqa: F811
    base, _, _ = server
    assert get(f"{base}/activity/wrong")[0] == 404
    assert get(f"{base}/activity/")[0] == 404
