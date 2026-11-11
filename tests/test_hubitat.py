"""Hubitat matching + commands against a fake Maker API shaped like the real hub."""

import pytest

from ringrouter.handlers import Context, h_home, h_scene
from ringrouter.hubitat import Hubitat, parse_value, target_of
from ringrouter.notify import ConsoleNotifier
from ringrouter.router import route
from ringrouter.store import Store


def dev(i, label, room, caps, **attrs):
    return {"id": i, "label": label, "room": room, "capabilities": caps, "attributes": attrs}


DEVICES = [
    dev(8, "Master Bedroom Light", "Master Bedroom", ["Switch", "SwitchLevel"], switch="off"),
    dev(9, "Master Bedroom Fan", "Master Bedroom", ["FanControl", "Switch", "SwitchLevel"], speed="off"),
    dev(16, "Living Room Overhead Light", "Living Room", ["Switch"]),
    dev(216, "Living Room Sconces", "Living Room", ["Switch", "SwitchLevel"]),
    dev(218, "Living Room Ceiling Fan", "Living Room", ["FanControl", "Switch", "SwitchLevel"]),
    dev(15, "Living Room Exhaust Fan", "Living Room", ["Switch"]),
    dev(67, "Living Room Door", "Living Room", ["Lock"], lock="locked"),
    dev(68, "Garage Patio Door", "Outside", ["Lock"], lock="locked"),
    dev(210, "Front Door Light", "Outside", ["Switch"]),
    dev(18, "Office Light", "Office", ["Switch", "SwitchLevel"]),
    dev(5, "Office Fan", "Office", ["FanControl", "Switch", "SwitchLevel"]),
    dev(232, "Smoke/CO Detector", "", ["Switch"]),
    dev(230, "Bedroom", "", ["MotionSensor", "TemperatureMeasurement"]),
]


class FakeHub:
    def __init__(self):
        self.calls = []

    def __call__(self, path):
        if path == "/devices/all":
            return DEVICES
        self.calls.append(path)
        return {}


@pytest.fixture
def hub():
    fake = FakeHub()
    h = Hubitat("10.0.0.2", "293", "tok", fetch=fake, aliases={"front door": "Living Room Door"})
    h.fake = fake
    return h


def labels(m):
    return [d.label for d in m.devices]


def test_unsafe_and_sensor_devices_excluded(hub):
    names = [d.label for d in hub.devices()]
    assert "Smoke/CO Detector" not in names and "Bedroom" not in names


@pytest.mark.parametrize("target,action,expect,kind", [
    ("the master bedroom light", "on", ["Master Bedroom Light"], "device"),
    ("office fan", "speed", ["Office Fan"], "device"),
    ("the living room lights", "off", ["Living Room Overhead Light", "Living Room Sconces"], "room"),
    ("the front door", "unlock", ["Living Room Door"], "device"),               # alias
    ("the sconces", "level", ["Living Room Sconces"], "device"),
    ("all the lights", "off", ["Master Bedroom Light", "Living Room Overhead Light", "Living Room Sconces",
                               "Front Door Light", "Office Light"], "all"),
])
def test_match(hub, target, action, expect, kind):
    m = hub.match(target, action)
    assert m.kind == kind and labels(m) == expect


def test_ambiguous_lock_without_alias():
    h = Hubitat("x", "1", "t", fetch=FakeHub())
    m = h.match("the door", "unlock")
    assert m.kind == "ambiguous" and set(labels(m)) == {"Living Room Door", "Garage Patio Door"}


def test_living_room_fan_is_ambiguous(hub):
    m = hub.match("living room fan", "on")
    assert m.kind == "ambiguous" and set(labels(m)) == {"Living Room Ceiling Fan", "Living Room Exhaust Fan"}


def test_no_match(hub):
    assert hub.match("the hot tub", "on").kind == "none"


@pytest.mark.parametrize("action,rest,expect", [
    ("set", "the master bedroom fan to low", ("setSpeed", "low")),
    ("set", "the fan to medium high", ("setSpeed", "medium-high")),
    ("set", "the sconces to 30%", ("setLevel", "30")),
    ("dim", "the sconces", ("setLevel", "30")),
    ("dim", "the sconces to 10 percent", ("setLevel", "10")),
    ("brighten", "the office light", ("setLevel", "100")),
    ("unlock", "the front door", ("unlock", None)),
    ("off", "the porch light", ("off", None)),
])
def test_parse_value(action, rest, expect):
    assert parse_value(action, rest) == expect


def test_target_of():
    assert target_of("the master bedroom fan to low") == "the master bedroom fan"


@pytest.fixture
def ctx(cfg, fake_fm, hub):
    cfg.raw["server"]["dry_run"] = False
    cfg.raw["server"]["dry_run_routes"] = []
    return Context(cfg=cfg, notifier=ConsoleNotifier(echo=False), store=Store(":memory:"), fm=fake_fm,
                   hubitat_factory=lambda c: hub)


@pytest.mark.parametrize("say,calls", [
    ("Turn off the living room lights", ["/devices/16/off", "/devices/216/off"]),
    ("Set the master bedroom fan to low", ["/devices/9/setSpeed/low"]),
    ("Unlock the front door", ["/devices/67/unlock"]),
    ("Turn the office light on", ["/devices/18/on"]),
])
def test_handler_sends_commands(ctx, hub, say, calls):
    res = h_home(route(say, ctx.cfg), ctx)
    assert res.state == "done" and hub.fake.calls == calls, res.summary


def test_handler_ambiguous_changes_nothing(ctx, hub):
    res = h_home(route("Turn on the living room fan", ctx.cfg), ctx)
    assert res.state == "refused" and hub.fake.calls == [] and ctx.notifier.sent[-1].title == "Which one?"


def test_handler_dry_run(ctx, hub):
    ctx.cfg.raw["server"]["dry_run"] = True
    res = h_home(route("Turn off the office light", ctx.cfg), ctx)
    assert res.state == "dry_run" and hub.fake.calls == []


def test_confirm_flow(ctx, hub):
    ctx.cfg.raw.setdefault("home", {})["confirm_actions"] = ["unlock"]
    res = h_home(route("Unlock the front door", ctx.cfg), ctx)
    assert res.state == "needs_confirm" and hub.fake.calls == []
    from ringrouter.handlers import run_confirmed
    pid = ctx.notifier.sent[-1].buttons[0].url.split("/")[-2]
    kind, payload = ctx.store.take_pending(pid)
    run_confirmed(kind, payload, ctx)
    assert hub.fake.calls == ["/devices/67/unlock"]


def test_scene(ctx, hub):
    ctx.cfg.raw["scenes"]["goodnight"]["actions"] = [
        {"device": "all the lights", "action": "off"}, {"device": "front door", "action": "lock"}]
    res = h_scene(route("Goodnight", ctx.cfg), ctx)
    assert res.state == "done"
    assert "/devices/67/lock" in hub.fake.calls and "/devices/8/off" in hub.fake.calls
