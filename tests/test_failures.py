"""When something behind the router breaks: timeouts, a backend error, partial failures, a model that's down.

Everything here is faked; the point is what the owner is told and what state is recorded.
"""

import threading
from http.server import ThreadingHTTPServer

import pytest

from ringrouter.handlers import Context, h_home, run
from ringrouter.hubitat import Hubitat
from ringrouter.mcp import MCPError
from ringrouter.notify import ConsoleNotifier, Notice, PushcutNotifier
from ringrouter.router import route
from ringrouter.server import App, make_handler
from ringrouter.store import Store


class FakeMac:
    """Stands in for mcp-macos. `fail[tool]` is an exception to raise, or a function of the call's
    args that returns one (or None to let the call through)."""

    def __init__(self):
        self.calls, self.fail = [], {}

    def __call__(self, name, args):
        self.calls.append((name, args))
        f = self.fail.get(name)
        err = f(args) if callable(f) and not isinstance(f, BaseException) else f
        if err:
            raise err
        return "ok"

    def titles(self, tool="reminders_tasks"):
        return [a.get("title") for n, a in self.calls if n == tool]


@pytest.fixture
def mac():
    return FakeMac()


@pytest.fixture
def live_app(cfg, fake_fm, mac):
    """A live-mode App (no dry run) over the fake Mac; Terri has a pinned number."""
    cfg.raw["server"]["dry_run"] = False
    cfg.raw["server"]["dry_run_routes"] = []
    cfg.raw["recipients"]["terri"]["handle"] = "+15550100123"
    return App(cfg, store=Store(":memory:"), notifier=ConsoleNotifier(echo=False), fm=fake_fm, mcp_call=mac,
               background=False)


@pytest.fixture
def live_server(live_app):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(live_app))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    live_app.public_url = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield live_app.public_url, live_app
    httpd.shutdown()


# ------------------------------------------------------------- messages ----

def test_send_that_times_out_is_unknown_and_never_resent(live_app, mac):
    mac.fail["messages_chat"] = TimeoutError("messages_chat: no reply within 60s")
    code, text = live_app.process("d1", "Tell Terri I'm on my way")
    n = live_app.notifier.sent[-1]
    assert code == 422 and text.startswith("unknown") and live_app.store.get_state("d1") == "unknown"
    assert n.title == "Send status unknown" and "not retrying" in n.text
    assert len(mac.calls) == 1


def test_send_error_is_reported_as_not_sent(live_app, mac):
    mac.fail["messages_chat"] = MCPError("Messages got an error: Not authorized to send Apple events")
    code, text = live_app.process("d1", "Tell Terri I'm on my way")
    assert code == 422 and text.startswith("failed") and live_app.store.get_state("d1") == "failed"
    assert live_app.notifier.sent[-1].title == "Not sent" and "Not authorized" in live_app.notifier.sent[-1].text


def test_send_in_flight_at_shutdown_is_marked_unknown_on_restart(cfg, fake_fm, mac):
    store = Store(":memory:")
    store.set_state("d1", "sending", transcript="Tell Terri I'm on my way")
    App(cfg, store=store, notifier=ConsoleNotifier(echo=False), fm=fake_fm, mcp_call=mac, background=False)
    assert store.get_state("d1") == "unknown" and mac.calls == []          # never sent again


def test_rewrite_failure_sends_what_was_said(live_app, mac, fake_fm):
    fake_fm.rewrite_ask = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("fm timed out"))
    code, text = live_app.process("d1", "Ask Terri when she'll be home")
    assert code == 200 and mac.calls[-1][1]["text"] == "when she'll be home" and "rewritten" not in text


# ----------------------------------------------------------- Apple apps ----

def test_hung_reminders_call_fails_the_command_and_says_so(live_app, mac):
    mac.fail["reminders_tasks"] = TimeoutError("reminders_tasks: no reply within 60s")
    code, text = live_app.process("d1", "Remind me at 5 to call the vet")
    n = live_app.notifier.sent[-1]
    assert code == 422 and live_app.store.get_state("d1") == "failed"
    assert n.title == "Error" and "no reply within 60s" in n.text and "Remind me at 5" in n.text


def test_item_split_failure_adds_the_request_as_one_item(live_app, mac, fake_fm):
    fake_fm.split_items = lambda s: (_ for _ in ()).throw(RuntimeError("fm down"))
    live_app.process("d1", "Add milk and eggs to my shopping list")
    assert mac.titles() == ["Milk and eggs"]


def test_title_failure_uses_the_first_words(live_app, mac, fake_fm):
    fake_fm.title = lambda b: (_ for _ in ()).throw(RuntimeError("fm down"))
    live_app.process("d1", "Make a note: the boat trailer needs new bearings before spring")
    assert mac.calls[-1][1]["title"] == "the boat trailer needs new bearings"


def test_calendar_without_a_usable_date_adds_nothing(live_app, mac, fake_fm):
    fake_fm.extract = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("fm down"))
    code, text = live_app.process("d1", "Add dentist Tuesday at 3 to my calendar")
    assert text.startswith("failed") and mac.calls == [] and live_app.notifier.sent[-1].title == "Not done"


# ----------------------------------------------------------------- home ----

DEVICES = [{"id": 16, "label": "Living Room Overhead Light", "room": "Living Room", "capabilities": ["Switch"]},
           {"id": 216, "label": "Living Room Sconces", "room": "Living Room", "capabilities": ["Switch"]}]


def home_ctx(cfg, fake_fm, broken=()):
    cfg.raw["server"]["dry_run"] = False
    calls = []

    def fetch(path):
        if path == "/devices/all":
            return DEVICES
        calls.append(path)
        if any(f"/devices/{i}/" in path for i in broken):
            raise RuntimeError("Hubitat HTTP 500: device not responding")
        return {}

    hub = Hubitat("x", "1", "t", fetch=fetch)
    ctx = Context(cfg=cfg, notifier=ConsoleNotifier(echo=False), store=Store(":memory:"), fm=fake_fm,
                  hubitat_factory=lambda c: hub)
    return ctx, calls


def test_one_device_failing_does_not_stop_the_others(cfg, fake_fm):
    ctx, calls = home_ctx(cfg, fake_fm, broken=[16])
    res = h_home(route("Turn off the living room lights", cfg), ctx)
    n = ctx.notifier.sent[-1]
    assert calls == ["/devices/16/off", "/devices/216/off"]                 # the second was still tried
    assert res.state == "failed" and res.http_status == 422
    assert n.title == "Home: partly failed" and "Living Room Overhead Light" in n.text and "Sconces" not in n.text


def test_every_device_failing_is_a_plain_failure(cfg, fake_fm):
    ctx, _ = home_ctx(cfg, fake_fm, broken=[16, 216])
    h_home(route("Turn off the living room lights", cfg), ctx)
    assert ctx.notifier.sent[-1].title == "Home: failed"


def test_device_gone_before_a_confirmed_action_runs(cfg, fake_fm):
    from ringrouter.handlers import run_confirmed
    ctx, calls = home_ctx(cfg, fake_fm)
    res = run_confirmed("home", {"device_ids": ["999"], "labels": ["Old Lock"], "cmd": "unlock", "value": None}, ctx)
    assert res.state == "failed" and calls == [] and "no longer available" in ctx.notifier.sent[-1].text


# ------------------------------------------------------- everything else ----

def test_a_crashing_handler_still_answers_and_notifies(live_app, mac, monkeypatch):
    from ringrouter import handlers
    monkeypatch.setitem(handlers.HANDLERS, "note", lambda r, ctx: 1 / 0)
    code, text = live_app.process("d1", "Make a note: anything")
    assert code == 422 and text.startswith("failed") and live_app.notifier.sent[-1].title == "Error"


def test_pushcut_being_down_never_breaks_a_command(monkeypatch):
    def down(*a, **k):
        raise OSError("network is unreachable")
    monkeypatch.setattr("urllib.request.urlopen", down)
    PushcutNotifier("Ring Router", webhook_url="https://example.invalid/hook").send(Notice("t", "x"))   # no raise


def test_unknown_confirmation_kind_is_refused(cfg, fake_fm):
    from ringrouter.handlers import run_confirmed
    ctx = Context(cfg=cfg, notifier=ConsoleNotifier(echo=False), store=Store(":memory:"), fm=fake_fm)
    assert run_confirmed("format_disk", {}, ctx).state == "failed"


def test_cancelled_confirmation_cannot_be_run_later(live_app, mac):
    live_app.process("d1", "Tell Sherry I'm on my way")                     # near-miss name: needs a tap
    pid = live_app.notifier.sent[-1].buttons[0].url.split("/")[-2]
    assert live_app.confirm(pid, "no") == (200, "cancelled")
    assert live_app.confirm(pid, "yes")[0] == 410 and mac.calls == []


def test_expired_confirmation_cannot_be_run(live_app, mac):
    live_app.cfg.raw.setdefault("notify", {})["confirm_ttl_s"] = -1
    live_app.process("d1", "Tell Sherry I'm on my way")
    pid = live_app.notifier.sent[-1].buttons[0].url.split("/")[-2]
    assert live_app.confirm(pid, "yes")[0] == 410 and mac.calls == []


def test_run_never_raises(cfg, fake_fm):
    ctx = Context(cfg=cfg, notifier=ConsoleNotifier(echo=False), store=Store(":memory:"), fm=fake_fm,
                  mcp_call=lambda *a: (_ for _ in ()).throw(MCPError("boom")))
    cfg.raw["server"]["dry_run"] = False
    for say in ("Make a note: x", "Add milk to my shopping list", "Remind me at 5 to call the vet"):
        assert run(route(say, cfg), ctx).state == "failed"
