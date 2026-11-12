"""Dates and times worked out in code from what was said (no model involved)."""

from datetime import datetime

import pytest

from ringrouter.when import hints_at_when, human, settle

NOW = datetime(2026, 11, 12, 10, 14)        # a Thursday morning


def stamp(text, **kw):
    w = settle(text, NOW, **kw)
    return w.stamp() if w.start else None


@pytest.mark.parametrize("said,due", [
    # a named day + a clock time
    ("tomorrow at 3pm to call the vet", "2026-11-13 15:00:00"),
    ("on Friday at noon to pay the electric bill", "2026-11-13 12:00:00"),
    ("on Tuesday at 9am to take out the recycling", "2026-11-17 09:00:00"),      # the model said Saturday
    ("next Wednesday at 2pm to call the dentist", "2026-11-18 14:00:00"),        # Wednesday of next week
    ("next Saturday at 7pm to pick up the cake", "2026-11-21 19:00:00"),         # not the day after tomorrow
    ("this Saturday at 9am to wash the car", "2026-11-14 09:00:00"),
    ("the day after tomorrow at 8am to pack", "2026-11-14 08:00:00"),
    ("on the 20th at 4:30pm to pick up the suit", "2026-11-20 16:30:00"),
    ("on December 3rd at 10 am to call Terri", "2026-12-03 10:00:00"),
    ("on the 5th of December at 10 to review", "2026-12-05 10:00:00"),
    ("on January 5th at 6:15am to leave for the airport", "2027-01-05 06:15:00"),   # already passed this year
    ("a week from Friday at noon to follow up", "2026-11-20 12:00:00"),
    # a time with no day: today, or tomorrow if that time has gone
    ("at 5 to call the vet", "2026-11-12 17:00:00"),
    ("at 9 to call Bob", "2026-11-13 09:00:00"),
    ("at five thirty to leave", "2026-11-12 17:30:00"),
    ("at seven pm to watch the game", "2026-11-12 19:00:00"),
    ("by 4 to submit the form", "2026-11-12 16:00:00"),
    ("at half past three to call Sam", "2026-11-12 15:30:00"),
    ("at quarter to five to leave", "2026-11-12 16:45:00"),
    ("at 12:30 to eat", "2026-11-12 12:30:00"),
    # parts of the day
    ("tomorrow morning to renew the registration", "2026-11-13 09:00:00"),
    ("Monday morning to email Sam", "2026-11-16 09:00:00"),
    ("tonight at 8 to take my pills", "2026-11-12 20:00:00"),
    ("tomorrow night to put the bins out", "2026-11-13 21:00:00"),
    # the same weekday as today, time already gone: next week
    ("Thursday at 9am to stretch", "2026-11-19 09:00:00"),
    ("Thursday at 3pm to stretch", "2026-11-12 15:00:00"),
    # so long from now
    ("in 20 minutes to check the oven", "2026-11-12 10:34:00"),                  # the model said 29 minutes
    ("in two hours to move the car", "2026-11-12 12:14:00"),
    ("in an hour and a half to flip the roast", "2026-11-12 11:44:00"),
    ("in half an hour to call back", "2026-11-12 10:44:00"),
    # a day with no time of day: due that day, no alert time
    ("today to call mom", "2026-11-12"),
    ("on Monday to put the bins out", "2026-11-16"),
    ("in 3 days to water the plants", "2026-11-15"),
    ("two weeks from today to renew the parking permit", "2026-11-26"),
    ("on the 3rd to pay rent", "2026-12-03"),
    # nothing said about when
    ("to buy stamps", None),
    ("to call mom", None),
    ("about 5 things I need from the store", None),
    ("to add 2 to the list", None),
    ("to move the level 3 boxes", None),
])
def test_reminder_time(said, due):
    assert stamp(said) == due


@pytest.mark.parametrize("said,start,end", [
    ("lunch with Mike on Friday at noon", "2026-11-13 12:00:00", None),
    ("dentist next Tuesday at 3pm", "2026-11-17 15:00:00", None),
    ("vet appointment Wednesday at 10:30", "2026-11-18 10:30:00", None),
    ("meeting at 3", "2026-11-12 15:00:00", None),
    ("dinner with the guys tomorrow from 6 to 8pm", "2026-11-13 18:00:00", "2026-11-13 20:00:00"),
    ("team offsite Monday from 9am until 1pm", "2026-11-16 09:00:00", "2026-11-16 13:00:00"),
    ("workshop Friday from 11 to 1pm", "2026-11-13 11:00:00", "2026-11-13 13:00:00"),
    ("standup tomorrow 9-9:30am", "2026-11-13 09:00:00", "2026-11-13 09:30:00"),
    ("brunch Sunday from noon to 2", "2026-11-15 12:00:00", "2026-11-15 14:00:00"),
    ("movie tonight from 10 to 1", "2026-11-12 22:00:00", "2026-11-13 01:00:00"),          # past midnight
    ("call with Sam tomorrow at 2 for 90 minutes", "2026-11-13 14:00:00", "2026-11-13 15:30:00"),
    ("review tomorrow at 10am for an hour and a half", "2026-11-13 10:00:00", "2026-11-13 11:30:00"),
    ("Terri's birthday on December 3rd", "2026-12-03", None),                                 # all day
    ("trash day on Monday", "2026-11-16", None),
])
def test_event_time(said, start, end):
    w = settle(said, NOW, event=True)
    assert (w.stamp(), w.stamp(w.end) if w.end else None) == (start, end)
    assert w.all_day is (len(start) == 10)


def test_the_model_is_overruled_where_the_words_are_clear():
    assert stamp("dentist on Tuesday at 3pm", model_start="2026-11-14 15:00:00", event=True) == "2026-11-17 15:00:00"
    assert stamp("to buy stamps", model_start="2026-11-12 10:14:00") is None            # it echoed "now"
    assert stamp("to buy stamps", model_start="2026-11-12 17:00:00") is None            # or made a time up
    w = settle("haircut on the 20th at 4:30pm", NOW, model_start="2026-11-20 16:30:00", model_end="2026-11-20 18:30:00",
               event=True)
    assert w.end is None                                                                 # an end nobody said


def test_the_model_settles_am_or_pm_when_the_words_do_not():
    assert stamp("at 7 to call Sam", model_start="2026-11-12 19:00:00") == "2026-11-12 19:00:00"
    assert stamp("at 7 to call Sam") == "2026-11-13 07:00:00"                            # no model: habit, then tomorrow


def test_the_model_is_used_and_flagged_for_what_the_code_cannot_read():
    w = settle("on Thanksgiving to call grandma", NOW, model_start="2026-11-26 00:00:00")
    assert (w.stamp(), w.all_day, w.notes) == ("2026-11-26", True, ["check the date"])
    w = settle("the day after Tuesday to call the plumber", NOW, model_start="2026-11-18 09:00:00")
    assert (w.stamp(), w.notes) == ("2026-11-18", ["check the date"])                    # not "Tuesday"


def test_said_but_unreadable_is_reported_not_guessed():
    w = settle("next week to renew the passport", NOW)
    assert w.start is None and w.notes == ["couldn't work out when"]
    assert hints_at_when("this weekend to mow the lawn") and not hints_at_when("to buy stamps")


def test_a_time_already_gone_today_is_flagged():
    w = settle("today at 9am to stretch", NOW)
    assert w.stamp() == "2026-11-12 09:00:00" and w.notes == ["that time has already passed"]


def test_human_dates_show_the_weekday():
    assert human(datetime(2026, 11, 17, 15, 0), now=NOW) == "Tue Nov 17, 3:00 PM"
    assert human(datetime(2026, 12, 3), all_day=True, now=NOW) == "Thu Dec 3"
    assert human(datetime(2027, 1, 5, 6, 15), now=NOW) == "Tue Jan 5 2027, 6:15 AM"
