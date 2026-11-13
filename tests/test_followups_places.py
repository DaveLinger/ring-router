"""'Tell her ...' follow-ups and named places for location reminders (helper and sends faked)."""

import json
import stat

import pytest

from ringrouter.handlers import Context, run
from ringrouter.handlers.messages import LAST_RECIPIENT
from ringrouter.notify import ConsoleNotifier
from ringrouter.router import route
from ringrouter.store import Store


@pytest.fixture
def ctx(cfg, fake_fm):                      # example config: dry run, so sends are "Would send to ..."
    return Context(cfg=cfg, notifier=ConsoleNotifier(echo=False), store=Store(":memory:"), fm=fake_fm,
                   address_lookup=lambda label="home": f"{label} address")


def say(ctx, text):
    return run(route(text, ctx.cfg), ctx)


# ------------------------------------------------------------- pronoun follow-ups --

def test_tell_her_goes_to_whoever_was_just_messaged(ctx):
    assert say(ctx, "Tell Terri I'm on my way").state == "dry_run"
    res = say(ctx, "Also tell her I'll grab dinner on the way home")
    assert res.state == "dry_run", res.summary
    assert ctx.notifier.sent[-1].text == "Would send to Terri: “I'll grab dinner on the way home”"


def test_pronoun_with_nobody_messaged_recently_is_refused(ctx):
    res = say(ctx, "Tell her I'll grab dinner")
    assert res.state == "refused" and "Who's “her”" in ctx.notifier.sent[-1].text


def test_pronoun_after_the_window_is_refused(ctx):
    say(ctx, "Tell Terri I'm on my way")
    with ctx.store.lock:
        ctx.store.conn.execute("UPDATE kv SET ts = ts - 3600 WHERE k=?", (LAST_RECIPIENT,))
    assert say(ctx, "Tell her I'll grab dinner").state == "refused"


def test_pronoun_that_does_not_fit_asks_first(ctx):
    say(ctx, "Tell Terri I'm on my way")
    res = say(ctx, "Tell him I'll grab dinner")
    assert res.state == "needs_confirm", res.summary
    assert "Send to Terri" in ctx.notifier.sent[-1].text and "heard “him”" in ctx.notifier.sent[-1].text


def test_recipient_without_a_pronoun_asks_first(ctx):
    ctx.cfg.raw["recipients"]["terri"].pop("pronoun", None)
    say(ctx, "Tell Terri I'm on my way")
    assert say(ctx, "Tell her I'll grab dinner").state == "needs_confirm"


def test_a_group_chat_is_them(ctx):
    say(ctx, "Tell the guys I'm in")
    res = say(ctx, "Tell them I'll be there at 8")
    assert res.state == "dry_run" and "Would send to the guys" in ctx.notifier.sent[-1].text


def test_ask_her_is_rewritten_for_the_last_recipient(ctx):
    say(ctx, "Tell Terri I'm on my way")
    res = say(ctx, "Ask her if she wants Thai tonight")
    assert res.state == "dry_run" and "Would send to Terri" in ctx.notifier.sent[-1].text


def test_a_rerouted_pronoun_message_still_asks(ctx):
    say(ctx, "Tell Terri I'm on my way")
    r = route("tell her I'll grab dinner", ctx.cfg)
    r.params["tier"] = "weak"                   # what the agent's rerouter does with noise around it
    assert run(r, ctx).state == "needs_confirm"


# ------------------------------------------------------------------------ places --

@pytest.fixture
def live(ctx, tmp_path):
    ctx.cfg.raw["server"]["dry_run"] = False
    ctx.cfg.raw["server"]["dry_run_routes"] = []
    calls = tmp_path / "calls.json"
    helper = tmp_path / "georeminder"
    helper.write_text(f"""#!/usr/bin/env python3
import json, sys
open({str(calls)!r}, "w").write(json.dumps(sys.argv[1:]))
print(json.dumps({{"ok": True, "id": "X1", "list": "Reminders"}}))
""")
    helper.chmod(helper.stat().st_mode | stat.S_IEXEC)
    ctx.cfg.raw["reminders"] = {"georeminder": str(helper), "geofence_radius_m": 100}
    ctx.cfg.raw["places"] = {
        "gym": {"name": "Gym", "spoken": ["the gym", "planet fitness"], "address": "1 Fit Way", "radius_m": 150},
        "work": {"spoken": ["work", "the office"], "contact_label": "work"},
        "cabin": {"lat": 39.5, "lon": -80.1},
    }
    ctx.calls = lambda: (lambda a: dict(zip(a[::2], a[1::2])))(json.loads(calls.read_text()))
    ctx.called = calls.exists
    return ctx


def test_reminder_at_a_configured_address(live):
    res = say(live, "Remind me to buy protein when I get to the gym")
    assert res.state == "done", res.summary
    assert live.calls() == {"--title": "Buy protein", "--address": "1 Fit Way", "--proximity": "enter",
                            "--place": "Gym", "--radius": "150"}
    assert live.notifier.sent[-1].text == "Buy protein: when you arrive at Gym"


def test_reminder_at_a_my_card_address_by_label(live):
    say(live, "Remind me when I leave work to call Bob")
    a = live.calls()
    assert a["--address"] == "work address" and a["--proximity"] == "leave" and a["--place"] == "Work"


def test_reminder_at_coordinates(live):
    say(live, "Remind me to turn the water on when I get to the cabin")
    a = live.calls()
    assert a["--lat"] == "39.5" and a["--lon"] == "-80.1" and "--address" not in a


def test_spoken_alias_for_a_place(live):
    say(live, "Remind me when I'm at Planet Fitness to stretch")
    assert live.calls()["--place"] == "Gym"


def test_unknown_place_is_refused_and_lists_the_known_ones(live):
    res = say(live, "Remind me to deposit the check when I get to the bank")
    assert res.state == "refused" and not live.called()
    text = live.notifier.sent[-1].text
    assert "“the bank”" in text and "Home, Gym, Work, Cabin" in text


@pytest.mark.parametrize("text", ["Remind me to grab my keys when I leave here",
                                  "Remind me when I arrive here to call mom",
                                  "Remind me when I get here to text Bob"])
def test_here_is_refused_for_lack_of_location(live, text):
    res = say(live, text)
    assert res.state == "refused" and not live.called()
    assert "don't have access to your location" in live.notifier.sent[-1].text


def test_home_still_comes_from_my_card(live):
    say(live, "Remind me when I get home to take out the trash")
    assert live.calls()["--address"] == "home address" and live.calls()["--place"] == "Home"


# ----------------------------------------------------------------------- modules --

@pytest.mark.parametrize("module,text", [("calendar", "Add dentist Tuesday at 3 to my calendar"),
                                         ("messages", "Tell Terri I'm on my way"),
                                         ("reminders", "Remind me at 5 to call the vet"),
                                         ("home", "Turn off the office fan"),
                                         ("claude_code", "Ask Claude on the NAS why Plex is down")])
def test_a_module_switched_off_refuses_its_commands(ctx, module, text):
    ctx.cfg.raw["modules"] = {module: False}
    res = say(ctx, text)
    assert res.state == "refused" and f"[modules] {module} = false" in ctx.notifier.sent[-1].text


def test_research_needs_notes_too(ctx):
    ctx.cfg.raw["modules"] = {"notes": False}
    assert say(ctx, "Research the best robot vacuum under 500 dollars").state == "refused"


def test_the_agent_loses_a_switched_off_modules_tools(cfg):
    from ringrouter.agents import spec_for
    cfg.raw["modules"] = {"calendar": False, "notes": False}
    tools = set(spec_for("general", cfg).tools)
    assert not tools & {"calendar_events", "calendar_calendars", "notes_items", "notes_folders"}
    assert "reminders_tasks" in tools
