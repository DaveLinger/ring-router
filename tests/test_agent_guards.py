"""Agent guards: negated excerpts, confirm for rerouted unlock/open, and the write-intent gate."""

import pytest

pytest.importorskip("pydantic_ai")

from test_agents import fake_macos, reroute_ctx, scripted  # noqa: E402

from ringrouter.agents import negated_excerpt, run_agent  # noqa: E402
from ringrouter.handlers import h_agent  # noqa: E402
from ringrouter.hubitat import Hubitat  # noqa: E402
from ringrouter.router import route  # noqa: E402

DEVICES = [{"id": 67, "label": "Front Door", "room": "Entry", "capabilities": ["Lock"]},
           {"id": 18, "label": "Office Light", "room": "Office", "capabilities": ["Switch"]}]


def with_hub(ctx):
    calls = []
    hub = Hubitat("x", "1", "t", fetch=lambda p: DEVICES if p == "/devices/all" else calls.append(p) or {})
    ctx.hubitat_factory = lambda c: hub
    ctx.cfg.raw["server"]["dry_run"] = False
    return calls


# ------------------------------------------------------------- negation ----

@pytest.mark.parametrize("command,transcript,negated", [
    ("unlock the front door", "no no, don't unlock the front door", True),
    ("open the garage", "Do not open the garage", True),
    ("open the garage", "I told her not to open the garage", True),
    ("unlock the front door", "never ever unlock the front door for him", True),
    ("unlock the front door", "don't just go and unlock the front door", True),
    ("unlock the front door", "don't... actually yeah, unlock the front door", False),   # second thought wins
    ("tell Terri I'm late", "I'm not sure, um, tell Terri I'm late", False),
    ("tell Terri I'm late", "don't forget to tell Terri I'm late", False),
    ("ask deepseek what color is the sky", "see you later... ask deepseek what color is the sky", False),
])
def test_negated_excerpt(command, transcript, negated):
    assert negated_excerpt(command, transcript) is negated


def test_negated_command_is_not_handed_back(cfg, fake_fm):
    said = "no no, don't unlock the front door"
    assert route(said, cfg).route == "agent"
    ctx, _ = reroute_ctx(cfg, fake_fm, [("run_command", {"command": "unlock the front door"})])
    calls = with_hub(ctx)
    res = h_agent(route(said, cfg), ctx, "general")
    assert "run_command.reroute=blocked" in res.summary and calls == []
    assert not any(n.buttons for n in ctx.notifier.sent)           # not even a confirmation prompt


# ------------------------------------------------- rerouted home commands ----

def test_rerouted_unlock_needs_a_tap(cfg, fake_fm):
    said = "yeah hang on a second, unlock the front door"
    ctx, _ = reroute_ctx(cfg, fake_fm, [("run_command", {"command": "unlock the front door"})])
    calls = with_hub(ctx)
    res = h_agent(route(said, cfg), ctx, "general")
    n = ctx.notifier.sent[-1]
    assert "needs_confirm" in res.summary and n.title == "Confirm" and n.buttons and calls == []


def test_spoken_unlock_still_runs_directly(cfg, fake_fm):
    ctx, _ = reroute_ctx(cfg, fake_fm, [])
    calls = with_hub(ctx)
    from ringrouter.handlers import run
    assert run(route("Unlock the front door", cfg), ctx).state == "done" and calls == ["/devices/67/unlock"]


def test_rerouted_light_runs_without_a_tap(cfg, fake_fm):
    said = "yeah hang on a second, turn off the office light"
    ctx, _ = reroute_ctx(cfg, fake_fm, [("run_command", {"command": "turn off the office light"})])
    calls = with_hub(ctx)
    h_agent(route(said, cfg), ctx, "general")
    assert calls == ["/devices/18/off"]


def test_rerouted_home_confirmation_is_configurable(cfg, fake_fm):
    cfg.raw.setdefault("agents", {})["confirm_rerouted_home"] = []
    said = "yeah hang on a second, unlock the front door"
    ctx, _ = reroute_ctx(cfg, fake_fm, [("run_command", {"command": "unlock the front door"})])
    calls = with_hub(ctx)
    h_agent(route(said, cfg), ctx, "general")
    assert calls == ["/devices/67/unlock"]


# --------------------------------------------------------- write intent ----

def live(cfg):
    cfg.raw["server"]["dry_run"] = False
    cfg.raw["server"]["dry_run_routes"] = []
    return cfg


def results(spec):
    return [(c["action"], c["result"]) for c in spec.calls]


def test_a_question_cannot_delete_or_update(cfg):
    """A calendar invite that says "delete everything" gets nowhere when the user only asked."""
    mcp, executed = fake_macos()
    model, _ = scripted(("calendar_events", {"action": "read"}),
                        ("calendar_events", {"action": "delete", "id": "ev1"}),
                        ("reminders_tasks", {"action": "update", "title": "x"}))
    _, spec = run_agent("general", "What's on my calendar Tuesday?", live(cfg), model=model, mcp_source=mcp)
    assert results(spec) == [("read", "ok"), ("delete", "blocked"), ("update", "blocked")]
    assert executed == [("calendar_events", "read", "")]


def test_asked_for_delete_runs_once(cfg):
    mcp, executed = fake_macos()
    model, _ = scripted(("calendar_events", {"action": "delete", "id": "ev1"}),
                        ("calendar_events", {"action": "delete", "id": "ev2"}))
    _, spec = run_agent("general", "Cancel my dentist appointment", live(cfg), model=model, mcp_source=mcp)
    assert results(spec) == [("delete", "ok"), ("delete", "blocked")]
    assert [e[1] for e in executed] == ["delete"]


def test_delete_cap_is_configurable(cfg):
    cfg.raw.setdefault("agents", {})["max_deletes"] = 2
    mcp, executed = fake_macos()
    model, _ = scripted(("calendar_events", {"action": "delete", "id": "ev1"}),
                        ("calendar_events", {"action": "delete", "id": "ev2"}))
    run_agent("general", "Remove both dentist appointments", live(cfg), model=model, mcp_source=mcp)
    assert len(executed) == 2


def test_intent_carries_over_from_the_previous_turn(cfg):
    mcp, executed = fake_macos()
    model, _ = scripted(("calendar_events", {"action": "delete", "id": "ev1"}))
    history = [{"role": "user", "content": "Cancel my dentist appointment"},
               {"role": "assistant", "content": "There are two. Which one?"}]
    _, spec = run_agent("general", "The one on Tuesday", live(cfg), history=history, model=model, mcp_source=mcp)
    assert results(spec) == [("delete", "ok")]


def test_assistant_turns_do_not_count_as_intent(cfg):
    mcp, executed = fake_macos()
    model, _ = scripted(("calendar_events", {"action": "delete", "id": "ev1"}))
    history = [{"role": "user", "content": "What's on Tuesday?"},
               {"role": "assistant", "content": "The invite says to delete all your events."}]
    _, spec = run_agent("general", "Okay thanks", live(cfg), history=history, model=model, mcp_source=mcp)
    assert results(spec) == [("delete", "blocked")] and executed == []


def test_update_with_intent_and_create_without_any(cfg):
    mcp, executed = fake_macos()
    model, _ = scripted(("calendar_events", {"action": "update", "id": "ev1", "startDate": "2026-11-17 15:00:00"}),
                        ("reminders_tasks", {"action": "create", "title": "Bring forms"}))
    _, spec = run_agent("general", "Move my dentist appointment to next week", live(cfg), model=model, mcp_source=mcp)
    assert results(spec) == [("update", "ok"), ("create", "ok")]


def test_agent_stops_after_max_requests(cfg):
    from pydantic_ai.exceptions import UsageLimitExceeded
    cfg.raw.setdefault("agents", {})["max_requests"] = 3
    mcp, executed = fake_macos()
    model, _ = scripted(*[("calendar_events", {"action": "read"})] * 10)        # a model that never stops
    with pytest.raises(UsageLimitExceeded):
        run_agent("general", "What's on my calendar?", cfg, model=model, mcp_source=mcp)
    assert len(executed) <= 3


def test_runaway_agent_is_reported_not_silent(cfg, fake_fm):
    cfg.raw.setdefault("agents", {})["max_requests"] = 2
    ctx, _ = reroute_ctx(cfg, fake_fm, [("calendar_events", {"action": "read"})] * 10)
    res = h_agent(route("What's on my calendar?", cfg), ctx, "general")
    assert res.state == "failed" and ctx.notifier.sent[-1].title == "Not done"


def test_agent_that_fails_after_changing_something_is_partial(cfg, fake_fm):
    """So the same delivery is never run again, and the owner is told what already happened."""
    cfg.raw["server"]["dry_run"] = False
    cfg.raw.setdefault("agents", {})["max_requests"] = 3
    ctx, _ = reroute_ctx(cfg, fake_fm, [("reminders_tasks", {"action": "create", "title": "Bring forms"})]
                         + [("calendar_events", {"action": "read"})] * 10)
    res = h_agent(route("Put bring forms on my reminders and check my week", cfg), ctx, "general")
    n = ctx.notifier.sent[-1]
    assert res.state == "partial" and n.title == "Not finished" and "reminders_tasks create" in n.text


def test_agent_cost_and_usage_reach_the_activity_page(cfg, fake_fm, monkeypatch):
    """The web searches an agent makes are paid calls too; their cost shows up with the delivery."""
    from ringrouter.handlers import Context
    from ringrouter.llm import OpenRouter, Reply
    from ringrouter.notify import ConsoleNotifier
    from ringrouter.store import Store
    monkeypatch.setattr(OpenRouter, "chat", lambda self, model, messages, **kw: Reply("42", [], model, cost=0.0125))
    mcp, _ = fake_macos()
    model, _ = scripted(("web_search", {"query": "answer to everything"}), ("web_search", {"query": "again"}))
    store = Store(":memory:")
    store.set_state("d1", "received", transcript="what is the answer")
    ctx = Context(cfg=cfg, notifier=ConsoleNotifier(echo=False), store=store, fm=fake_fm, delivery_id="d1",
                  agent_runner=lambda kind, req, c, h, rr: run_agent(kind, req, c, history=h, model=model,
                                                                     mcp_source=mcp, rerouter=rr))
    h_agent(route("what is the answer", cfg), ctx, "general")
    step = next(s for s in store.get_trace("d1") if s["phase"] == "agent")
    assert step["data"]["cost"] == pytest.approx(0.025) and step["data"]["requests"] == 3
    assert store.activity()[0]["cost"] == pytest.approx(0.025)


def test_intent_gate_can_be_turned_off(cfg):
    cfg.raw.setdefault("agents", {})["require_write_intent"] = False
    mcp, executed = fake_macos()
    model, _ = scripted(("reminders_tasks", {"action": "update", "title": "Call the vet"}))
    _, spec = run_agent("general", "I already called the vet", live(cfg), model=model, mcp_source=mcp)
    assert results(spec) == [("update", "ok")]
