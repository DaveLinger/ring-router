"""Fixes from the November 2026 review, one test (or a few) per issue, so none of them comes back.

To record a new known issue without fixing it yet, add its test here marked
`@pytest.mark.xfail(strict=True, reason=...)`: the suite stays green while it is open, and the day
it is fixed the test "unexpectedly passes" and fails the run until the marker is removed.
"""

import pytest
from test_failures import FakeMac, live_app, live_server, mac  # noqa: F401  (fixtures)
from test_server import post

from ringrouter import protocol
from ringrouter.handlers import Context, h_home, h_reminder
from ringrouter.homekit import Home
from ringrouter.notify import ConsoleNotifier
from ringrouter.router import route
from ringrouter.store import Store


def test_retry_after_a_failure_runs_the_command_again(live_server, mac):  # noqa: F811
    base, app = live_server
    attempts = []
    mac.fail["reminders_tasks"] = lambda args: attempts.append(1) or (RuntimeError("Reminders not responding")
                                                                      if len(attempts) == 1 else None)   # first try fails
    h, b = protocol.build_request("Remind me at 5 to call the vet", "testsecret")
    assert post(f"{base}/hook/testpath", h, b)[0] == 422                    # nothing was created
    code, text = post(f"{base}/hook/testpath", h, b)                        # "Retry" resends the same delivery
    assert "duplicate" not in text and text.startswith("done") and len(attempts) == 2


def test_state_after_a_confirmed_send(live_app, mac):  # noqa: F811
    live_app.process("d1", "Tell Sherry I'm on my way")
    pid = live_app.notifier.sent[-1].buttons[0].url.split("/")[-2]
    assert live_app.confirm(pid, "yes")[0] == 200 and len(mac.calls) == 1
    assert live_app.store.get_state("d1") == "done"
    assert [row[0] for row in live_app.store.recent() if row[1] == "sending"] == []
    assert live_app.store.recover_interrupted() == []                        # no false "mid-send" at next start


def test_partial_list_failure_reports_what_was_added(live_app, mac):  # noqa: F811
    mac.fail["reminders_tasks"] = lambda args: RuntimeError("Reminders not responding") if args["title"] == "Eggs" else None
    live_app.process("d1", "Add milk, eggs and bread to my shopping list")
    n = live_app.notifier.sent[-1]
    assert mac.titles() == ["Milk", "Eggs", "Bread"]                         # the third is still tried
    assert "Milk" in n.text and "Bread" in n.text and "Eggs" in n.text       # added vs failed, by name


def test_backend_down_is_not_reported_as_no_such_device(cfg, fake_fm):
    class DownHomeKit:
        def devices(self, refresh=False):
            raise RuntimeError("HomeMCPBridge: no reply within 60s")

    cfg.raw["server"]["dry_run"] = False
    home = Home(None, DownHomeKit())
    ctx = Context(cfg=cfg, notifier=ConsoleNotifier(echo=False), store=Store(":memory:"), fm=fake_fm,
                  hubitat_factory=lambda c: home)
    res = h_home(route("Turn off the porch light", cfg), ctx)
    n = ctx.notifier.sent[-1]
    assert "No device matching" not in n.text and "HomeKit" in n.text and res.state == "failed"


@pytest.mark.parametrize("say", [
    "Remind me when I leave work to call Bob",
    "Remind me when I arrive at the office to email Sam",
    "Remind me to call mom when I get back from vacation",
])
def test_location_reminders_for_other_places_are_refused(cfg, fake_fm, say):
    ctx = Context(cfg=cfg, notifier=ConsoleNotifier(echo=False), store=Store(":memory:"), fm=fake_fm,
                  address_lookup=lambda: "7 Example Ct, Fairmont, WV 26554")
    res = h_reminder(route(say, cfg), ctx)                                   # example config: dry run
    assert res.state == "refused", res.summary


@pytest.mark.parametrize("say,title,when", [
    ("Remind me when I get home to take out the trash", "Take out the trash", "arrive home"),
    ("Remind me to lock up when I leave", "Lock up", "leave home"),
    ("Remind me when I leave the house to grab the mail", "Grab the mail", "leave home"),
    ("Remind me when I get back to water the plants", "Water the plants", "arrive home"),
])
def test_home_location_reminders_still_work(cfg, fake_fm, say, title, when):
    ctx = Context(cfg=cfg, notifier=ConsoleNotifier(echo=False), store=Store(":memory:"), fm=fake_fm,
                  address_lookup=lambda: "7 Example Ct, Fairmont, WV 26554")
    res = h_reminder(route(say, cfg), ctx)
    assert res.state == "dry_run" and f"“{title}” when you {when}" in ctx.notifier.sent[-1].text


# ----------------------------------------------- retries: only when nothing happened ----

def resend(base, text):
    h, b = protocol.build_request(text, "testsecret")
    return post(f"{base}/hook/testpath", h, b), post(f"{base}/hook/testpath", h, b)


def test_a_timed_out_command_is_never_run_again(live_server, mac):  # noqa: F811
    base, app = live_server
    mac.fail["reminders_tasks"] = TimeoutError("reminders_tasks: no reply within 60s")
    first, second = resend(base, "Remind me at 5 to call the vet")
    assert first[0] == 422 and first[1].startswith("unknown") and second == (200, "duplicate: unknown\n")
    assert len(mac.calls) == 1


def test_a_partly_done_command_is_never_run_again(live_server, mac):  # noqa: F811
    base, app = live_server
    mac.fail["reminders_tasks"] = lambda args: RuntimeError("Reminders not responding") if args["title"] == "Eggs" else None
    first, second = resend(base, "Add milk, eggs and bread to my shopping list")
    assert first[0] == 422 and first[1].startswith("partial") and second == (200, "duplicate: partial\n")
    assert mac.titles() == ["Milk", "Eggs", "Bread"]                         # milk was not added twice


def test_a_list_where_nothing_was_added_can_be_retried(live_app, mac):  # noqa: F811
    mac.fail["reminders_tasks"] = RuntimeError("Reminders not responding")
    code, text = live_app.process("d1", "Add milk and eggs to my shopping list")
    assert code == 422 and text.startswith("failed") and live_app.notifier.sent[-1].title == "Shopping list: not added"


def test_a_list_that_stops_answering_is_not_hammered(live_app, mac):  # noqa: F811
    mac.fail["reminders_tasks"] = lambda args: TimeoutError("no reply within 60s") if args["title"] == "Eggs" else None
    code, text = live_app.process("d1", "Add milk, eggs and bread to my shopping list")
    n = live_app.notifier.sent[-1]
    assert mac.titles() == ["Milk", "Eggs"] and text.startswith("unknown")   # bread not tried: no minute-long waits
    assert "Added: Milk." in n.text and "may have been added" in n.text and "Not tried: Bread." in n.text


# -------------------------------------------------- restarts and confirmations ----

def test_jobs_cut_off_by_a_restart_are_reported(cfg, fake_fm, mac):  # noqa: F811
    from ringrouter.server import App
    store = Store(":memory:")
    store.set_state("d-agent", "accepted", transcript="Move my dentist appointment to next week")
    store.set_state("d-old", "received", transcript="Something from last week")
    with store.lock:
        store.conn.execute("UPDATE deliveries SET updated = updated - 7 * 86400 WHERE delivery_id = 'd-old'")
    notifier = ConsoleNotifier(echo=False)
    App(cfg, store=store, notifier=notifier, fm=fake_fm, mcp_call=mac, background=False)
    assert store.get_state("d-agent") == "unknown" and store.get_state("d-old") == "unknown"
    assert [n.title for n in notifier.sent] == ["Interrupted"] and "dentist appointment" in notifier.sent[0].text
    assert mac.calls == []                                                   # reported, never run again


def test_declined_and_expired_confirmations_close_the_delivery(live_app, mac):  # noqa: F811
    live_app.process("d1", "Tell Sherry I'm on my way")
    pid = live_app.notifier.sent[-1].buttons[0].url.split("/")[-2]
    live_app.confirm(pid, "no")
    assert live_app.store.get_state("d1") == "cancelled"
    live_app.cfg.raw.setdefault("notify", {})["confirm_ttl_s"] = -1
    live_app.process("d2", "Tell Sherry I'm on my way")
    pid = live_app.notifier.sent[-1].buttons[0].url.split("/")[-2]
    assert live_app.confirm(pid, "yes")[0] == 410 and live_app.store.get_state("d2") == "cancelled"
    assert mac.calls == []


def test_confirmed_action_is_traced_under_its_delivery(live_app, mac):  # noqa: F811
    live_app.process("d1", "Tell Sherry I'm on my way")
    pid = live_app.notifier.sent[-1].buttons[0].url.split("/")[-2]
    live_app.confirm(pid, "yes")
    phases = [s["phase"] for s in live_app.store.get_trace("d1")]
    assert phases.index("confirmed") < phases.index("command") < len(phases) - 1 and phases[-1] == "result"
    assert live_app.store.get_delivery(f"confirm-{pid}") is None


def test_get_never_confirms(live_server, mac):  # noqa: F811
    import urllib.error
    import urllib.request
    base, app = live_server
    app.process("d1", "Tell Sherry I'm on my way")
    yes = app.notifier.sent[-1].buttons[0].url
    with pytest.raises(urllib.error.HTTPError) as e:                         # a link preview, a prefetch, a browser
        urllib.request.urlopen(yes, timeout=10)
    assert e.value.code == 405 and e.value.headers["Allow"] == "POST" and mac.calls == []
    assert post(yes, {}, b"")[0] == 200 and len(mac.calls) == 1              # the button itself still works


# ----------------------------------------------------------------- pages ----

def test_pages_cannot_run_script_or_leak_their_address(live_server):  # noqa: F811
    import urllib.request
    from ringrouter.llm import Reply
    base, app = live_server
    aid = app.llm_store.save_answer("ask", "m", "q", Reply("See https://example.com/a?x=1&y=2", [
        {"title": "ok", "url": "https://example.com/src"}, {"title": "bad", "url": "javascript:alert(1)"}]))
    for url in (f"{base}/answers/{aid}", f"{base}/activity/acttoken"):
        with urllib.request.urlopen(url, timeout=10) as r:
            page, headers = r.read().decode(), r.headers
        assert "default-src 'none'" in headers["Content-Security-Policy"] and "script-src" not in headers["Content-Security-Policy"]
        assert headers["Referrer-Policy"] == "no-referrer" and headers["X-Content-Type-Options"] == "nosniff"
    with urllib.request.urlopen(f"{base}/answers/{aid}", timeout=10) as r:
        page = r.read().decode()
    assert 'href="https://example.com/src"' in page and "javascript:" not in page


def test_markdown_cannot_break_out_of_a_link():
    from ringrouter import md
    out = md.inline('https://example.com/"onmouseover="alert(1)')
    assert out.startswith('<a href="https://example.com/">https://example.com/</a>') and '"onmouseover' not in out
    assert md.inline("[docs](https://example.com/?a=1&b=2)") == '<a href="https://example.com/?a=1&amp;b=2">docs</a>'
    assert md.inline('[x](https://example.com/"onclick="y)') == '<a href="https://example.com/&quot;onclick=&quot;y">x</a>'
    assert md.inline("**bold** and https://example.com/a*b*c") == (
        '<b>bold</b> and <a href="https://example.com/a*b*c">https://example.com/a*b*c</a>')


# ------------------------------------------------------------------ home ----

HOMEKIT_DEVICES = {"devices": [
    {"id": "A1", "name": "Porch Light", "room": "Outside", "type": "light", "manufacturer": "Lutron"},
    {"id": "A2", "name": "Garage Siren", "room": "Garage", "type": "switch", "manufacturer": "Aqara"},
    {"id": "A3", "name": "Aquarium", "room": "Den", "type": "outlet", "manufacturer": "Eve"},
    {"id": "A4", "name": "Coffee Maker", "room": "Kitchen", "type": "outlet", "manufacturer": "Eve"},
]}


def homekit(calls, **kw):
    import json
    from ringrouter.homekit import HomeKit

    def call(tool, args):
        calls.append((tool, args))
        return json.dumps(HOMEKIT_DEVICES if tool == "list_devices" else {"success": True})

    return HomeKit(["bridge"], call=call, **kw)


def test_homekit_devices_can_be_excluded_like_hubitat_ones():
    hk = homekit([], exclude=["Aquarium"])
    assert [d.label for d in hk.devices()] == ["Porch Light", "Coffee Maker"]        # no siren, no aquarium


def test_one_exclude_list_covers_every_backend(cfg, fake_fm, monkeypatch):
    from ringrouter import handlers
    cfg.raw["hubitat"] = {"exclude": ["Aquarium"]}                                   # Hubitat itself is off
    cfg.raw["homekit"] = {"command": ["bridge"], "exclude": ["Freezer"]}
    cfg.raw["home"] = {"exclude": ["Server Rack"]}
    monkeypatch.setattr(handlers.home, "_HUB", None)
    ctx = Context(cfg=cfg, notifier=ConsoleNotifier(echo=False), store=Store(":memory:"), fm=fake_fm)
    assert handlers.get_hubitat(ctx).homekit.exclude == {"aquarium", "freezer", "server rack"}


def test_everything_leaves_outlets_alone_but_a_named_outlet_works(cfg, fake_fm):
    cfg.raw["server"]["dry_run"] = False
    calls = []
    home = Home(None, homekit(calls))
    ctx = Context(cfg=cfg, notifier=ConsoleNotifier(echo=False), store=Store(":memory:"), fm=fake_fm,
                  hubitat_factory=lambda c: home)
    assert h_home(route("Turn off everything", cfg), ctx).state == "done"
    assert [a["name"] for t, a in calls if t == "control_device"] == ["A1"]          # the light only
    assert h_home(route("Turn off the coffee maker", cfg), ctx).state == "done"
    assert [a["name"] for t, a in calls if t == "control_device"] == ["A1", "A4"]
    assert [t for t, _ in calls].count("list_devices") == 1                          # one catalog read for both


def test_scene_that_half_runs_is_partial(cfg, fake_fm):
    from ringrouter.handlers import h_scene
    from ringrouter.hubitat import Hubitat
    cfg.raw["server"]["dry_run"] = False
    devs = [{"id": 1, "label": "Hall Light", "room": "Hall", "capabilities": ["Switch"]},
            {"id": 2, "label": "Front Door", "room": "Hall", "capabilities": ["Lock"]}]

    def fetch(path):
        if path == "/devices/all":
            return devs
        if path == "/devices/2/lock":
            raise RuntimeError("Hubitat HTTP 500")
        return {}

    hub = Hubitat("x", "1", "t", fetch=fetch)
    cfg.raw["scenes"]["goodnight"]["actions"] = [{"device": "hall light", "action": "off"},
                                                 {"device": "front door", "action": "lock"}]
    ctx = Context(cfg=cfg, notifier=ConsoleNotifier(echo=False), store=Store(":memory:"), fm=fake_fm,
                  hubitat_factory=lambda c: hub)
    assert h_scene(route("Goodnight", cfg), ctx).state == "partial"


def test_open_the_door_asks_which_instead_of_opening_the_garage(cfg, fake_fm):
    from ringrouter.hubitat import Hubitat
    cfg.raw["server"]["dry_run"] = False
    calls = []
    devs = [{"id": 70, "label": "Garage Door", "room": "Garage", "capabilities": ["GarageDoorControl"]}]
    hub = Hubitat("x", "1", "t", fetch=lambda p: devs if p == "/devices/all" else calls.append(p) or {})
    ctx = Context(cfg=cfg, notifier=ConsoleNotifier(echo=False), store=Store(":memory:"), fm=fake_fm,
                  hubitat_factory=lambda c: hub)
    assert h_home(route("Open the door", cfg), ctx).state == "refused"
    assert calls == [] and ctx.notifier.sent[-1].title == "Which one?"
    assert h_home(route("Open the garage door", cfg), ctx).state == "done" and calls == ["/devices/70/open"]
