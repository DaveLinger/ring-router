"""Known issues from the November 2026 review that are not fixed yet.

Each test states the behaviour we want and is marked xfail, so the suite stays green while the
issue is open. They are strict: the day one is fixed, its test "unexpectedly passes" and fails the
run until the xfail marker is removed, which keeps this list honest.
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


def known(reason):
    return pytest.mark.xfail(strict=True, reason=reason)


@known("a failed delivery is remembered as a duplicate, so the Pebble app's Retry does nothing")
def test_retry_after_a_failure_runs_the_command_again(live_server, mac):  # noqa: F811
    base, app = live_server
    attempts = []
    mac.fail["reminders_tasks"] = lambda args: attempts.append(1) or (RuntimeError("Reminders not responding")
                                                                      if len(attempts) == 1 else None)
    h, b = protocol.build_request("Remind me at 5 to call the vet", "testsecret")
    assert post(f"{base}/hook/testpath", h, b)[0] == 422                    # nothing was created
    code, text = post(f"{base}/hook/testpath", h, b)                        # "Retry" resends the same delivery
    assert "duplicate" not in text and text.startswith("done") and len(attempts) == 2


@known("a confirmed send runs under a new 'confirm-…' id that stays in 'sending'; the original stays needs_confirm")
def test_state_after_a_confirmed_send(live_app, mac):  # noqa: F811
    live_app.process("d1", "Tell Sherry I'm on my way")
    pid = live_app.notifier.sent[-1].buttons[0].url.split("/")[-2]
    assert live_app.confirm(pid, "yes")[0] == 200 and len(mac.calls) == 1
    assert live_app.store.get_state("d1") == "done"
    assert [row[0] for row in live_app.store.recent() if row[1] == "sending"] == []
    assert live_app.store.recover_interrupted() == []                        # no false "mid-send" at next start


@known("a failure part-way through a list says nothing about what was added and skips the rest")
def test_partial_list_failure_reports_what_was_added(live_app, mac):  # noqa: F811
    mac.fail["reminders_tasks"] = lambda args: RuntimeError("Reminders not responding") if args["title"] == "Eggs" else None
    live_app.process("d1", "Add milk, eggs and bread to my shopping list")
    n = live_app.notifier.sent[-1]
    assert mac.titles() == ["Milk", "Eggs", "Bread"]                         # the third is still tried
    assert "Milk" in n.text and "Bread" in n.text and "Eggs" in n.text       # added vs failed, by name


@known("a home backend that is down is reported as 'No device matching'")
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


@known("'when I leave work' and other non-home places become a home geofence")
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
