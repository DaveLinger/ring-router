"""Recordings delivered long after they were spoken: ask before acting; date maths uses the spoken time."""

import time

import pytest
from test_server import post, send, server  # noqa: F401  (reuse the live-listener fixture)

from ringrouter.server import recorded_time


def ago_ms(seconds):
    return int((time.time() - seconds) * 1000)


@pytest.fixture
def front_door(monkeypatch):
    from ringrouter import handlers
    from ringrouter.hubitat import Hubitat
    devs = [{"id": 67, "label": "Front Door", "room": "Entry", "capabilities": ["Lock"]}]
    calls = []
    hub = Hubitat("x", "1", "t", fetch=lambda p: devs if p == "/devices/all" else calls.append(p) or {})
    monkeypatch.setattr(handlers, "get_hubitat", lambda ctx: hub)
    return calls


def test_old_unlock_is_held_until_run_is_tapped(server, cfg, front_door):  # noqa: F811
    base, app, notifier = server
    cfg.raw["server"]["dry_run"] = False
    (code, text), _ = send(base, "Unlock the front door", recorded_at_ms=ago_ms(3 * 3600))
    n = notifier.sent[-1]
    assert code == 200 and "needs_confirm" in text and "recorded 3 h ago" in text
    assert n.title == "Old recording" and [b.label for b in n.buttons] == ["Run", "Cancel"]
    assert front_door == []
    code, text = post(n.buttons[0].url, {}, b"")
    assert code == 200 and text.startswith("done") and front_door == ["/devices/67/unlock"]
    assert post(n.buttons[0].url, {}, b"")[0] == 410                      # one time only


def test_old_recording_can_be_cancelled(server, cfg, front_door):  # noqa: F811
    base, app, notifier = server
    cfg.raw["server"]["dry_run"] = False
    send(base, "Unlock the front door", recorded_at_ms=ago_ms(900))
    assert post(notifier.sent[-1].buttons[1].url, {}, b"") == (200, "cancelled\n") and front_door == []


def test_old_message_is_held(server):  # noqa: F811
    base, _, notifier = server
    (_, text), _ = send(base, "Tell Terri I'm on my way", recorded_at_ms=ago_ms(1200))
    assert "needs_confirm" in text and "20 min ago" in notifier.sent[-1].text


def test_fresh_and_slightly_late_commands_run(server, cfg, front_door):  # noqa: F811
    base, _, _ = server
    cfg.raw["server"]["dry_run"] = False
    send(base, "Unlock the front door")
    send(base, "Unlock the front door", recorded_at_ms=ago_ms(120))
    assert front_door == ["/devices/67/unlock"] * 2


def test_gate_can_be_turned_off(server, cfg, front_door):  # noqa: F811
    base, _, _ = server
    cfg.raw["server"].update(dry_run=False, max_recording_age_s=0)
    send(base, "Unlock the front door", recorded_at_ms=ago_ms(3 * 3600))
    assert front_door == ["/devices/67/unlock"]


def test_unreadable_recorded_time_is_ignored(server, cfg, front_door):  # noqa: F811
    base, _, _ = server
    cfg.raw["server"]["dry_run"] = False
    send(base, "Unlock the front door", recorded_at_ms=12345)             # 1970: not believable
    assert front_door == ["/devices/67/unlock"]


def test_old_reminder_runs_and_counts_from_when_it_was_spoken(server, fake_fm):  # noqa: F811
    base, _, notifier = server
    (code, text), _ = send(base, "Remind me in 20 minutes to check the oven", recorded_at_ms=ago_ms(3600))
    assert "needs_confirm" not in text and "dry_run" in text
    assert abs(time.time() - 3600 - fake_fm.extract_now.timestamp()) < 5


def test_recorded_time():
    now = 1_800_000_000.0
    assert recorded_time(str(int((now - 60) * 1000)), now) == now - 60     # Unix ms
    assert recorded_time(str(int(now - 60)), now) == now - 60              # seconds are understood too
    assert recorded_time(str(int((now + 30) * 1000)), now) == now          # small clock skew: treat as now
    for bad in (None, "", "yesterday", "0", str(int((now + 3600) * 1000)), str(int((now - 90 * 86400) * 1000))):
        assert recorded_time(bad, now) is None
