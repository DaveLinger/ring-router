"""Reminder and calendar times with the REAL on-device model in the loop.

Two layers are checked, with "now" pinned to Thursday 2026-11-12 10:14 (fm runs greedy, so each
case gives the same answer on every run):

  * what comes out of the pipeline the handlers use (model, then `when.settle`): must be right;
  * what the model says on its own: recorded as xfail where it is wrong, which is why its dates
    are no longer trusted when the words can be read in code.

Run only these:  pytest -m live
"""

import os
from datetime import datetime

import pytest

from ringrouter.fm import FM
from ringrouter.handlers import EVENT_SCHEMA, REMINDER_SCHEMA
from ringrouter.prompts import prompt
from ringrouter.when import settle

FM_PATH = "/usr/bin/fm"
NOW = datetime(2026, 11, 12, 10, 14)        # a Thursday

pytestmark = [pytest.mark.live,
              pytest.mark.skipif(not os.access(FM_PATH, os.X_OK), reason="needs /usr/bin/fm (macOS 27)")]


def model_alone(got):
    return pytest.mark.xfail(reason=f"the on-device model alone returns {got}")


def reminder(body):
    return FM(FM_PATH, 30).extract(body, prompt("reminder_extract"), REMINDER_SCHEMA, now=NOW)


def event(body):
    return FM(FM_PATH, 30).extract(body, prompt("calendar_extract"), EVENT_SCHEMA, now=NOW)


REMINDERS = [
    ("tomorrow at 3pm to call the vet", "2026-11-13 15:00:00"),
    ("at 5 to call the vet", "2026-11-12 17:00:00"),
    ("on Friday at noon to pay the electric bill", "2026-11-13 12:00:00"),
    ("tomorrow morning to renew the registration", "2026-11-13 09:00:00"),
    ("on Tuesday at 9am to take out the recycling", "2026-11-17 09:00:00"),
    ("next Wednesday at 2pm to call the dentist", "2026-11-18 14:00:00"),
    ("in 20 minutes to check the oven", "2026-11-12 10:34:00"),
    ("tonight at 8 to take my pills", "2026-11-12 20:00:00"),
    ("at quarter to five to leave for the airport", "2026-11-12 16:45:00"),
    ("to buy stamps", None),
    ("to call mom", None),
]
EVENTS = [    # (said, start, end or None, all day)
    ("lunch with Mike on Friday at noon", "2026-11-13 12:00:00", None, False),
    ("an oil change this Saturday at 9am", "2026-11-14 09:00:00", None, False),
    ("dentist next Tuesday at 3pm", "2026-11-17 15:00:00", None, False),
    ("dentist on Tuesday at 3pm", "2026-11-17 15:00:00", None, False),
    ("vet appointment Wednesday at 10:30", "2026-11-18 10:30:00", None, False),
    ("haircut on the 20th at 4:30pm", "2026-11-20 16:30:00", None, False),
    ("dinner with the guys tomorrow from 6 to 8pm", "2026-11-13 18:00:00", "2026-11-13 20:00:00", False),
    ("team offsite Monday from 9am until 1pm", "2026-11-16 09:00:00", "2026-11-16 13:00:00", False),
    ("Terri's birthday on December 3rd", "2026-12-03", None, True),
    ("trash day on Monday", "2026-11-16", None, True),
]


# ------------------------------------------------ the pipeline: must be right ----

@pytest.mark.parametrize("body,due", REMINDERS, ids=[r[0] for r in REMINDERS])
def test_reminder_due(body, due):
    w = settle(body, NOW, model_start=reminder(body).get("due") or "")
    assert (w.stamp() if w.start else None) == due and not w.notes


@pytest.mark.parametrize("body,start,end,all_day", EVENTS, ids=[e[0] for e in EVENTS])
def test_event_time(body, start, end, all_day):
    out = event(body)
    w = settle(body, NOW, model_start=out.get("start") or "", model_end=out.get("end") or "", event=True)
    assert (w.stamp(), w.stamp(w.end) if w.end else None, w.all_day) == (start, end, all_day) and not w.notes


@pytest.mark.parametrize("body,word", [("tomorrow at 3pm to call the vet", "vet"), ("to buy stamps", "stamps"),
                                       ("on Tuesday at 9am to take out the recycling", "recycling")])
def test_reminder_title_is_the_task_not_the_time(body, word):
    title = reminder(body)["title"].lower()
    assert word in title and not any(t in title for t in ("tomorrow", "tuesday", "3pm", "9am"))


# ---------------------------------- the model on its own: why it isn't trusted ----

@model_alone("2026-11-14, a Saturday")
def test_model_alone_weekday():
    assert event("dentist on Tuesday at 3pm")["start"].strip() == "2026-11-17 15:00:00"


@model_alone("the current time as the due time")
def test_model_alone_no_time_said():
    assert not (reminder("to buy stamps").get("due") or "").strip()


@model_alone("no end time")
def test_model_alone_spoken_end():
    assert (event("dinner with the guys tomorrow from 6 to 8pm").get("end") or "").strip() == "2026-11-13 20:00:00"


@model_alone("no all_day flag (a one-hour event at midnight)")
def test_model_alone_all_day():
    assert event("Terri's birthday on December 3rd").get("all_day") is True


@model_alone("2026-11-12 14:45:00")
def test_model_alone_quarter_to_five():
    assert reminder("at quarter to five to leave for the airport")["due"].strip() == "2026-11-12 16:45:00"
