"""Reminders and calendar events end to end: the model gives the title, the code settles the time.

"Now" is pinned through the recording time (Thursday 2026-11-12 10:14). The fake model always
answers with the same title and a stale date, which is the point: its dates must not matter when
the words are clear.
"""

from datetime import datetime

import pytest

from ringrouter.handlers import Context, run
from ringrouter.notify import ConsoleNotifier
from ringrouter.router import route
from ringrouter.store import Store

NOW = datetime(2026, 11, 12, 10, 14)


@pytest.fixture
def live(cfg, fake_fm):
    cfg.raw["server"]["dry_run"] = False
    cfg.raw["calendars"]["default"] = "Home"
    calls = []
    ctx = Context(cfg=cfg, notifier=ConsoleNotifier(echo=False), store=Store(":memory:"), fm=fake_fm,
                  recorded_at=NOW.timestamp(), mcp_call=lambda name, args: calls.append((name, args)) or "ok")
    return ctx, calls


def say(ctx, text):
    res = run(route(text, ctx.cfg), ctx)
    return res, ctx.notifier.sent[-1]


def test_reminder_time_comes_from_the_words(live):
    ctx, calls = live
    res, n = say(ctx, "Remind me on Tuesday at 9am to take out the recycling")
    assert calls == [("reminders_tasks", {"action": "create", "title": "Call the vet", "dueDate": "2026-11-17 09:00:00"})]
    assert n.title == "Reminder set" and n.text == "Call the vet: Tue Nov 17, 9:00 AM"


def test_reminder_keeps_its_time_when_the_model_is_down(live, fake_fm):
    """This used to save "at 5 to call the vet" with no due time and call it done."""
    ctx, calls = live
    fake_fm.extract = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("fm timed out"))
    res, n = say(ctx, "Remind me at 5 to call the vet")
    assert calls[0][1] == {"action": "create", "title": "At 5 to call the vet", "dueDate": "2026-11-12 17:00:00"}
    assert n.text.endswith("Thu Nov 12, 5:00 PM")


def test_reminder_with_no_time_gets_none(live):
    ctx, calls = live                                   # the fake model still offers a due time; it is ignored
    res, n = say(ctx, "Remind me to buy stamps")
    assert "dueDate" not in calls[0][1] and n.title == "Reminder set" and n.text == "Call the vet"


def test_reminder_whose_time_cannot_be_read_says_so(live, fake_fm):
    ctx, calls = live
    fake_fm.extract = lambda *a, **k: {"title": "Renew the passport", "due": ""}
    res, n = say(ctx, "Remind me sometime this weekend to renew the passport")
    assert "dueDate" not in calls[0][1]
    assert n.title == "Reminder set, no time" and "couldn't work out when" in n.text


def test_reminder_for_a_day_has_no_alert_time(live):
    ctx, calls = live
    say(ctx, "Remind me on Monday to put the bins out")
    assert calls[0][1]["dueDate"] == "2026-11-16"


def test_a_date_only_the_model_produced_is_flagged(live, fake_fm):
    ctx, calls = live
    fake_fm.extract = lambda *a, **k: {"title": "call grandma", "due": "2026-11-24 00:00:00"}
    res, n = say(ctx, "Remind me on Thanksgiving to call grandma")
    assert calls[0][1]["dueDate"] == "2026-11-24" and n.text == "Call grandma: Tue Nov 24 (check the date)"


def test_model_titles_are_tidied(live, fake_fm):
    ctx, calls = live
    fake_fm.extract = lambda *a, **k: {"title": "to buy stamps", "due": ""}
    say(ctx, "Remind me to buy stamps")
    assert calls[0][1]["title"] == "Buy stamps"


def test_event_on_a_weekday(live):
    ctx, calls = live
    res, n = say(ctx, "Add dentist Tuesday at 3 to my calendar")
    assert calls == [("calendar_events", {"action": "create", "title": "Call the vet", "targetCalendar": "Home",
                                          "startDate": "2026-11-17 15:00:00", "endDate": "2026-11-17 16:00:00"})]
    assert n.title == "Event added" and n.text == "“Call the vet” Tue Nov 17, 3:00 PM (Home)"


def test_event_with_a_spoken_end(live):
    ctx, calls = live
    res, n = say(ctx, "Add dinner with the guys tomorrow from 6 to 8pm to my calendar")
    assert (calls[0][1]["startDate"], calls[0][1]["endDate"]) == ("2026-11-13 18:00:00", "2026-11-13 20:00:00")
    assert "Fri Nov 13, 6:00 PM to 8:00 PM" in n.text


def test_event_with_only_a_date_is_all_day(live):
    ctx, calls = live
    res, n = say(ctx, "Add Terri's birthday on December 3rd to my calendar")
    args = calls[0][1]
    assert (args["startDate"], args["endDate"], args["isAllDay"]) == ("2026-12-03", "2026-12-03", True)
    assert "Thu Dec 3 (Home)" in n.text


def test_event_still_added_when_the_model_is_down(live, fake_fm):
    ctx, calls = live
    fake_fm.extract = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("fm timed out"))
    say(ctx, "Schedule an oil change next Saturday at 9am")
    assert calls[0][1]["title"] == "An oil change next Saturday at 9am" and calls[0][1]["startDate"] == "2026-11-21 09:00:00"


def test_event_with_no_readable_time_is_not_added(live, fake_fm):
    ctx, calls = live
    fake_fm.extract = lambda *a, **k: {"title": "the thing", "start": ""}
    res, n = say(ctx, "Schedule the thing with the people")
    assert res.state == "failed" and calls == [] and n.title == "Not done"


def test_the_trace_shows_what_was_said_heard_and_settled(live):
    ctx, _ = live
    ctx.delivery_id = "d1"
    say(ctx, "Add dentist Tuesday at 3 to my calendar")
    step = next(s["data"] for s in ctx.store.get_trace("d1") if s["phase"] == "when")
    assert step["said"] == "dentist Tuesday at 3" and step["model"] == "2026-11-10 15:00:00"
    assert step["settled"] == "2026-11-17 15:00:00"
