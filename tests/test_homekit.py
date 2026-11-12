"""HomeKit backend (HomeMCPBridge faked) and the combined Hubitat + HomeKit catalog."""

import json

import pytest

from ringrouter.handlers import Context, h_home
from ringrouter.homekit import Home, HomeKit
from ringrouter.hubitat import Hubitat
from ringrouter.notify import ConsoleNotifier
from ringrouter.router import route
from ringrouter.store import Store

HK_DEVICES = [
    {"id": "FD-1", "name": "Front Door Lock", "room": "Front Door", "type": "lock", "source": "HomeKit",
     "manufacturer": "Schlage", "reachable": True},
    {"id": "LR-1", "name": "Laundry Room Door", "room": "Laundry Room", "type": "lock", "source": "HomeKit",
     "manufacturer": "Schlage", "reachable": True},
    {"id": "GD-1", "name": "Front Garage Door", "room": "Garage", "type": "garage_door", "source": "HomeKit",
     "manufacturer": "Meross", "reachable": True},
    {"id": "FP-1", "name": "Living Room Fireplace", "room": "Living Room", "type": "switch", "source": "HomeKit",
     "manufacturer": "Shelly", "reachable": True},
    {"id": "HB-1", "name": "Master Bedroom Light", "room": "Master Bedroom", "type": "light", "source": "HomeKit",
     "manufacturer": "Hubitat C-7", "reachable": True},                      # bridged duplicate
    {"id": "MOM-1", "name": "MOM Backyard", "room": "Mom’s House", "type": "switch", "source": "HomeKit",
     "manufacturer": "Ring", "reachable": True},                             # other house
    {"id": "CAM-1", "name": "Aqara Camera", "room": "Garage", "type": "unknown", "source": "HomeKit",
     "manufacturer": "Aqara", "reachable": True},
]
HUB_DEVICES = [
    {"id": 8, "label": "Master Bedroom Light", "room": "Master Bedroom", "capabilities": ["Switch", "SwitchLevel"]},
    {"id": 68, "label": "Garage Patio Door", "room": "Outside", "capabilities": ["Lock"]},
]


class FakeBridge:
    def __init__(self):
        self.controls = []

    def __call__(self, tool, args):
        if tool == "list_devices":
            return json.dumps({"count": len(HK_DEVICES), "devices": HK_DEVICES})
        self.controls.append(args)
        return json.dumps({"success": True})


@pytest.fixture
def home():
    bridge, hub_calls = FakeBridge(), []

    def fetch(path):
        if path == "/devices/all":
            return HUB_DEVICES
        hub_calls.append(path)
        return {}

    hk = HomeKit(["bridge"], exclude_rooms=["Mom's House"], call=bridge)
    h = Home(Hubitat("x", "1", "t", fetch=fetch), hk, prefer=["ceiling fan"])
    h.bridge, h.hub_calls = bridge, hub_calls
    return h


def test_hubitat_disabled_by_default_homekit_takes_bridged(cfg, monkeypatch):
    from ringrouter import handlers
    cfg.raw["hubitat"] = {"host": "x", "app_id": "1"}                  # no enabled flag
    cfg.secrets["hubitat_token"] = "t"
    cfg.raw["homekit"] = {"command": ["bridge"], "homes": ["Home"]}
    monkeypatch.setattr(handlers, "_HUB", None)
    ctx = Context(cfg=cfg, notifier=ConsoleNotifier(echo=False), store=Store(":memory:"), fm=None)
    home = handlers.get_hubitat(ctx)
    assert home.hubitat is None and home.homekit.skip_manufacturers == []
    cfg.raw["hubitat"]["enabled"] = True
    home = handlers.get_hubitat(ctx)
    assert home.hubitat is not None and home.homekit.skip_manufacturers == ["hubitat"]


def test_homekit_filters(home):
    names = sorted(d.label for d in home.homekit.devices())
    assert names == ["Front Door Lock", "Front Garage Door", "Laundry Room Door", "Living Room Fireplace"]


def test_combined_catalog_prefers_hubitat_for_shared_devices(home):
    m = home.match("the master bedroom light", "on")
    assert [(d.label, d.backend) for d in m.devices] == [("Master Bedroom Light", "hubitat")]


def test_front_door_resolves_to_homekit_lock(home):
    m = home.match("the front door", "unlock")
    assert [(d.label, d.backend) for d in m.devices] == [("Front Door Lock", "homekit")]


@pytest.fixture
def ctx(cfg, fake_fm, home):
    cfg.raw["server"]["dry_run"] = False
    cfg.raw["server"]["dry_run_routes"] = []
    return Context(cfg=cfg, notifier=ConsoleNotifier(echo=False), store=Store(":memory:"), fm=fake_fm,
                   hubitat_factory=lambda c: home)


@pytest.mark.parametrize("say,control", [
    ("Unlock the front door", {"name": "FD-1", "action": "unlock"}),
    ("Lock the laundry room door", {"name": "LR-1", "action": "lock"}),
    ("Open the front garage door", {"name": "GD-1", "action": "open"}),
    ("Turn on the living room fireplace", {"name": "FP-1", "action": "on"}),
])
def test_homekit_commands_by_id(ctx, home, say, control):
    res = h_home(route(say, ctx.cfg), ctx)
    assert res.state == "done", res.summary
    assert home.bridge.controls == [control] and home.hub_calls == []


def test_hubitat_device_still_goes_to_hubitat(ctx, home):
    res = h_home(route("Lock the garage patio door", ctx.cfg), ctx)
    assert res.state == "done" and home.hub_calls == ["/devices/68/lock"] and home.bridge.controls == []


def test_homekit_down_does_not_break_hubitat(ctx, home):
    def broken(tool, args):
        raise RuntimeError("bridge crashed")
    home.homekit._call = broken
    home.homekit._cache = None
    res = h_home(route("Turn off the master bedroom light", ctx.cfg), ctx)
    assert res.state == "done" and home.hub_calls == ["/devices/8/off"]
