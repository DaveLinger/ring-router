"""Spoken calendar names map to real calendars (uses an explicit alias table, like config.toml)."""

import pytest

from ringrouter.router import route

ALIASES = {"our": "Dave/Terri", "shared": "Dave/Terri", "dave and terri": "Dave/Terri",
           "dave and terri's": "Dave/Terri", "terri's": "Dave/Terri", "work": "Work", "my work": "Work"}


@pytest.mark.parametrize("say,calendar,extracted", [
    ("Add dentist Tuesday at 3 to my calendar", "Dave", "dentist Tuesday at 3"),
    ("Add date night Friday at 7 to our calendar", "Dave/Terri", "date night Friday at 7"),
    ("Put the vet appointment Monday at 9 on the shared calendar", "Dave/Terri", "the vet appointment Monday at 9"),
    ("Add dinner with the Smiths Saturday at 6 to the Dave and Terri calendar", "Dave/Terri", "dinner with the Smiths Saturday at 6"),
    ("Add the plumber Thursday at 10 to Dave and Terri's calendar", "Dave/Terri", "the plumber Thursday at 10"),
    ("Put standup tomorrow at 9 on my work calendar", "Work", "standup tomorrow at 9"),
    ("Schedule an oil change next Saturday at 9am", "Dave", "an oil change next Saturday at 9am"),
])
def test_calendar_alias(cfg, say, calendar, extracted):
    cfg.raw["calendars"] = {"default": "Dave", "aliases": ALIASES}
    r = route(say, cfg)
    assert r.route == "calendar" and r.params["calendar"] == calendar and r.body == extracted, r.describe()
