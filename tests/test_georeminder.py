"""'When I get home' reminders: parsing, My Card address, helper invocation (helper faked)."""

import json
import stat

import pytest

from ringrouter.handlers import Context, h_reminder, parse_geo
from ringrouter.notify import ConsoleNotifier
from ringrouter.router import route
from ringrouter.store import Store


@pytest.mark.parametrize("body,expect", [
    ("when I get home to take out the trash", ("Take out the trash", "home", "enter")),
    ("to call mom when I leave", ("Call mom", "home", "leave")),
    ("to grab the mail when I get back home", ("Grab the mail", "home", "enter")),
    ("when I arrive home to start the laundry", ("Start the laundry", "home", "enter")),
])
def test_parse_geo(body, expect):
    assert parse_geo(body) == expect


@pytest.fixture
def ctx(cfg, fake_fm, tmp_path):
    cfg.raw["server"]["dry_run"] = False
    cfg.raw["server"]["dry_run_routes"] = []
    calls = tmp_path / "calls.json"
    helper = tmp_path / "georeminder"
    helper.write_text(f"""#!/usr/bin/env python3
import json, sys
open({str(calls)!r}, "w").write(json.dumps(sys.argv[1:]))
print(json.dumps({{"ok": True, "id": "X1", "lat": 1.0, "lon": 2.0, "list": "Reminders", "proximity": "enter"}}))
""")
    helper.chmod(helper.stat().st_mode | stat.S_IEXEC)
    cfg.raw["reminders"] = {"georeminder": str(helper), "geofence_radius_m": 120}
    c = Context(cfg=cfg, notifier=ConsoleNotifier(echo=False), store=Store(":memory:"), fm=fake_fm,
                address_lookup=lambda: "7 Example Ct, Fairmont, WV 26554")
    c.calls_file = calls
    return c


def test_home_reminder_calls_helper(ctx):
    res = h_reminder(route("Remind me when I get home to take out the trash", ctx.cfg), ctx)
    assert res.state == "done", res.summary
    argv = json.loads(ctx.calls_file.read_text())
    a = dict(zip(argv[::2], argv[1::2]))
    assert a == {"--title": "Take out the trash", "--address": "7 Example Ct, Fairmont, WV 26554",
                 "--proximity": "enter", "--place": "Home", "--radius": "120"}
    assert ctx.notifier.sent[-1].title == "Reminder set"


def test_leave_reminder(ctx):
    h_reminder(route("Remind me to lock up when I leave", ctx.cfg), ctx)
    a = json.loads(ctx.calls_file.read_text())
    assert a[a.index("--proximity") + 1] == "leave" and a[a.index("--title") + 1] == "Lock up"


def test_other_places_refused(ctx):
    res = h_reminder(route("Remind me when I get to work to call Bob", ctx.cfg), ctx)
    assert res.state == "refused" and not ctx.calls_file.exists()


def test_missing_my_card_address(ctx):
    def boom():
        raise RuntimeError("no address on your My Card in Contacts")
    ctx.address_lookup = boom
    res = h_reminder(route("Remind me when I get home to feed the cat", ctx.cfg), ctx)
    assert res.state == "failed" and "My Card" in ctx.notifier.sent[-1].text


def test_dry_run_does_not_call_helper(ctx):
    ctx.cfg.raw["server"]["dry_run"] = True
    res = h_reminder(route("Remind me when I get home to feed the cat", ctx.cfg), ctx)
    assert res.state == "dry_run" and not ctx.calls_file.exists()
