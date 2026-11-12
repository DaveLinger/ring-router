"""Device state, read-back after lock/door commands, and the agent's home tools."""

import pytest

pytest.importorskip("pydantic_ai")

from test_agents import reroute_ctx  # noqa: E402

from ringrouter import handlers  # noqa: E402
from ringrouter.handlers import Context, h_agent, h_home, home_status  # noqa: E402
from ringrouter.hubitat import Hubitat, describe_state  # noqa: E402
from ringrouter.notify import ConsoleNotifier  # noqa: E402
from ringrouter.router import route  # noqa: E402
from ringrouter.store import Store  # noqa: E402

DEVICES = [{"id": 67, "label": "Front Door", "room": "Entry", "capabilities": ["Lock"]},
           {"id": 70, "label": "Garage Door", "room": "Garage", "capabilities": ["GarageDoorControl"]},
           {"id": 18, "label": "Kitchen Ceiling Light", "room": "Kitchen", "capabilities": ["Switch", "SwitchLevel"]},
           {"id": 19, "label": "Kitchen Pendants", "room": "Kitchen", "capabilities": ["Switch"]}]


class FakeHub:
    """A Maker API whose devices have state; `stuck` devices ignore commands."""

    def __init__(self, stuck=()):
        self.calls, self.stuck = [], set(stuck)
        self.attrs = {67: {"lock": "locked"}, 70: {"door": "closed"}, 18: {"switch": "on", "level": 40},
                      19: {"switch": "off"}}

    def __call__(self, path):
        if path == "/devices/all":
            return DEVICES
        parts = path.strip("/").split("/")
        dev = int(parts[1])
        if len(parts) == 2:                                       # /devices/<id>: its state
            return {"attributes": [{"name": k, "currentValue": v} for k, v in self.attrs[dev].items()]}
        self.calls.append(path)
        if dev not in self.stuck:
            cmd = parts[2]
            self.attrs[dev].update({"lock": {"lock": "locked"}, "unlock": {"lock": "unlocked"}, "open": {"door": "open"},
                                    "close": {"door": "closed"}, "on": {"switch": "on"}, "off": {"switch": "off"}}[cmd])
        return {}


def live_ctx(cfg, fake_fm, fake, **home_cfg):
    cfg.raw["server"]["dry_run"] = False
    cfg.raw["home"] = {"confirm_actions": [], "read_back_every_s": 0, **home_cfg}
    hub = Hubitat("x", "1", "t", fetch=fake)
    return Context(cfg=cfg, notifier=ConsoleNotifier(echo=False), store=Store(":memory:"), fm=fake_fm,
                   hubitat_factory=lambda c: hub)


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(handlers.home, "_sleep", lambda s: None)


# -------------------------------------------------------------- read-back ----

def test_read_back_is_off_unless_asked_for(cfg, fake_fm):
    fake = FakeHub(stuck=[67])
    ctx = live_ctx(cfg, fake_fm, fake)
    res = h_home(route("Unlock the front door", cfg), ctx)
    assert res.state == "done" and ctx.notifier.sent[-1].text == "Front Door unlocked"      # as before: taken on trust


def test_read_back_confirms_a_lock(cfg, fake_fm):
    ctx = live_ctx(cfg, fake_fm, FakeHub(), read_back=True)
    res = h_home(route("Unlock the front door", cfg), ctx)
    assert res.state == "done" and ctx.notifier.sent[-1].text == "Front Door unlocked (confirmed)"


def test_read_back_reports_a_lock_that_did_not_move(cfg, fake_fm):
    ctx = live_ctx(cfg, fake_fm, FakeHub(stuck=[67]), read_back=True, read_back_timeout_s=0)
    fake_state_before = "locked"
    res = h_home(route("Unlock the front door", cfg), ctx)
    n = ctx.notifier.sent[-1]
    assert res.state == "partial" and res.http_status == 422 and n.title == "Home: check it"
    assert n.text == f"Front Door was told to unlock but reports {fake_state_before}"


def test_read_back_waits_for_a_slow_garage_door(cfg, fake_fm, monkeypatch):
    fake = FakeHub(stuck=[70])
    ctx = live_ctx(cfg, fake_fm, fake, read_back=True, read_back_timeout_s=60)
    polls = []

    def tick(seconds):                       # the door finishes opening on the third look
        polls.append(seconds)
        fake.attrs[70]["door"] = "opening" if len(polls) < 3 else "open"

    monkeypatch.setattr(handlers.home, "_sleep", tick)
    res = h_home(route("Open the garage door", cfg), ctx)
    assert res.state == "done" and len(polls) == 3 and "(confirmed)" in ctx.notifier.sent[-1].text


def test_read_back_that_cannot_read_says_so(cfg, fake_fm):
    fake = FakeHub()
    ctx = live_ctx(cfg, fake_fm, fake, read_back=True, read_back_timeout_s=0)
    real = fake.__call__
    hub = ctx.hubitat_factory(ctx)
    hub._fetch = lambda p: (_ for _ in ()).throw(RuntimeError("hub unreachable")) if p == "/devices/67" else real(p)
    res = h_home(route("Lock the front door", cfg), ctx)
    assert res.state == "partial" and "couldn't read its state (hub unreachable)" in ctx.notifier.sent[-1].text


def test_read_back_leaves_lights_alone(cfg, fake_fm):
    fake = FakeHub()
    ctx = live_ctx(cfg, fake_fm, fake, read_back=True)
    h_home(route("Turn off the kitchen ceiling light", cfg), ctx)
    assert ctx.notifier.sent[-1].text == "Kitchen Ceiling Light off"


# ----------------------------------------------------------------- status ----

def test_describe_state():
    assert describe_state({"locked": True}) == "locked" and describe_state({"door": "opening"}) == "opening"
    assert describe_state({"on": True, "level": 40}) == "on, 40%" and describe_state({"on": False, "level": 40}) == "off"
    assert describe_state({"reachable": False, "on": True}) == "not responding" and describe_state({}) == "state unknown"


def test_home_status_by_device_and_by_room(cfg, fake_fm):
    ctx = live_ctx(cfg, fake_fm, FakeHub())
    assert home_status(ctx, "the garage door") == "Garage Door (Garage): closed"
    assert home_status(ctx, "kitchen").splitlines() == ["Kitchen Ceiling Light (Kitchen): on, 40%", "Kitchen Pendants (Kitchen): off"]
    assert home_status(ctx, "the sauna") == "No device matching 'the sauna'."


# ------------------------------------------------------------ agent tools ----

def agent_ctx(cfg, fake_fm, steps, fake=None, **home_cfg):
    ctx, _ = reroute_ctx(cfg, fake_fm, steps)
    fake = fake or FakeHub()
    cfg.raw["server"]["dry_run"] = False
    cfg.raw["home"] = {"confirm_actions": [], "read_back_every_s": 0, **home_cfg}
    hub = Hubitat("x", "1", "t", fetch=fake)
    ctx.hubitat_factory = lambda c: hub
    return ctx, fake


def test_agent_answers_a_question_about_a_door(cfg, fake_fm):
    said = "Is the garage door closed?"
    assert route(said, cfg).route == "agent"
    ctx, fake = agent_ctx(cfg, fake_fm, [("home_status", {"target": "garage door"})])
    res = h_agent(route(said, cfg), ctx, "general")
    assert "Garage Door (Garage): closed" in ctx.notifier.sent[-1].text and fake.calls == []      # read only


def test_agent_handles_phrasing_the_router_misses(cfg, fake_fm):
    said = "Kitchen lights off"
    assert route(said, cfg).route == "agent"
    ctx, fake = agent_ctx(cfg, fake_fm, [("home_control", {"action": "off", "target": "kitchen lights"})])
    res = h_agent(route(said, cfg), ctx, "general")
    assert fake.calls == ["/devices/18/off", "/devices/19/off"] and res.state == "done"
    assert [n.title for n in ctx.notifier.sent] == ["Home"]                   # one notification, from the home route


def test_agent_unlock_and_open_need_a_tap(cfg, fake_fm):
    said = "Could you get the front door unlocked for me"
    ctx, fake = agent_ctx(cfg, fake_fm, [("home_control", {"action": "unlock", "target": "front door"})])
    res = h_agent(route(said, cfg), ctx, "general")
    n = ctx.notifier.sent[-1]
    assert res.state == "needs_confirm" and n.title == "Confirm" and n.buttons and fake.calls == []


def test_agent_cannot_switch_things_the_user_did_not_ask_about(cfg, fake_fm):
    """A web page or an invite that says "turn everything off" gets nowhere."""
    said = "What's the weather tomorrow?"
    ctx, fake = agent_ctx(cfg, fake_fm, [("home_control", {"action": "off", "target": "everything"})])
    res = h_agent(route(said, cfg), ctx, "general")
    assert fake.calls == [] and "home_control.off=blocked" in res.summary


def test_agent_device_actions_are_capped_and_use_the_matcher(cfg, fake_fm):
    cfg.raw.setdefault("agents", {})["max_home_actions"] = 1
    said = "Kitchen ceiling light off and also the sauna"
    ctx, fake = agent_ctx(cfg, fake_fm, [("home_control", {"action": "off", "target": "sauna"}),
                                         ("home_control", {"action": "off", "target": "kitchen ceiling light"})])
    res = h_agent(route(said, cfg), ctx, "general")
    assert fake.calls == [] and res.state == "refused"                        # no such device; the second was over the cap


def test_agent_home_tools_respect_dry_run_and_can_be_turned_off(cfg, fake_fm):
    ctx, fake = agent_ctx(cfg, fake_fm, [("home_control", {"action": "off", "target": "kitchen lights"})])
    cfg.raw["server"]["dry_run"] = True
    assert h_agent(route("Kitchen lights off", cfg), ctx, "general").state == "dry_run" and fake.calls == []
    cfg.raw.setdefault("agents", {})["home_tools"] = False
    offered = []

    def runner(kind, req, c, h, rr):
        offered.append(getattr(rr, "home", None))
        raise RuntimeError("stop here")

    ctx.agent_runner = runner
    h_agent(route("Kitchen lights off", cfg), ctx, "general")
    assert offered == [None]
