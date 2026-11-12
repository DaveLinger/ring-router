"""Dates and times pulled out of reminders and calendar requests by the REAL on-device model.

"Now" is pinned (Thursday 2026-11-12 10:14), and fm runs greedy, so each case gives the same answer
on every run. Cases the model gets wrong today are marked xfail with what it returned; they are the
argument for checking its dates in code before creating anything.

Run only these:  pytest -m live
"""

import os
from datetime import datetime

import pytest

from ringrouter.fm import FM
from ringrouter.handlers import EVENT_SCHEMA, REMINDER_SCHEMA
from ringrouter.prompts import prompt

FM_PATH = "/usr/bin/fm"
NOW = datetime(2026, 11, 12, 10, 14)        # a Thursday

pytestmark = [pytest.mark.live,
              pytest.mark.skipif(not os.access(FM_PATH, os.X_OK), reason="needs /usr/bin/fm (macOS 27)")]


def wrong(got):
    return pytest.mark.xfail(reason=f"on-device model returns {got}")


def when(value):
    return datetime.strptime(value.strip(), "%Y-%m-%d %H:%M:%S")


def reminder(body):
    return FM(FM_PATH, 30).extract(body, prompt("reminder_extract"), REMINDER_SCHEMA, now=NOW)


def event(body):
    return FM(FM_PATH, 30).extract(body, prompt("calendar_extract"), EVENT_SCHEMA, now=NOW)


@pytest.mark.parametrize("body,due", [
    ("tomorrow at 3pm to call the vet", "2026-11-13 15:00:00"),
    ("at 5 to call the vet", "2026-11-12 17:00:00"),
    ("on Friday at noon to pay the electric bill", "2026-11-13 12:00:00"),
    ("tomorrow morning to renew the registration", "2026-11-13 09:00:00"),
])
def test_reminder_due_time(body, due):
    assert reminder(body)["due"].strip() == due


def test_reminder_in_20_minutes():
    minutes = (when(reminder("in 20 minutes to check the oven")["due"]) - NOW).total_seconds() / 60
    assert 19 <= minutes <= 21, f"{minutes:g} minutes"


@wrong("the current time as the due time, so the reminder is due the moment it is created")
def test_reminder_with_no_time_gets_no_due_time():
    assert not (reminder("to buy stamps").get("due") or "").strip()


def test_reminder_title_drops_the_time():
    title = reminder("tomorrow at 3pm to call the vet")["title"].lower()
    assert "vet" in title and "tomorrow" not in title and "3" not in title


@pytest.mark.parametrize("body,start", [
    ("lunch with Mike on Friday at noon", "2026-11-13 12:00:00"),
    ("an oil change this Saturday at 9am", "2026-11-14 09:00:00"),
    pytest.param("dentist next Tuesday at 3pm", "2026-11-17 15:00:00", marks=wrong("2026-11-14, a Saturday")),
    pytest.param("dentist on Tuesday at 3pm", "2026-11-17 15:00:00", marks=wrong("2026-11-14, a Saturday")),
    ("haircut on the 20th at 4:30pm", "2026-11-20 16:30:00"),
])
def test_event_start(body, start):
    assert event(body)["start"].strip() == start


@wrong("no end time, so the event gets the default length instead of ending at 8pm")
def test_event_with_a_spoken_end_time():
    e = event("dinner with the guys tomorrow from 6 to 8pm")
    assert e["start"].strip() == "2026-11-13 18:00:00" and (e.get("end") or "").strip() == "2026-11-13 20:00:00"


@wrong("no all_day flag, so the birthday becomes a one-hour event at midnight")
def test_event_with_only_a_date_is_all_day():
    e = event("Terri's birthday on December 3rd")
    assert e["start"].startswith("2026-12-03") and e.get("all_day") is True
