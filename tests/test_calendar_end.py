from ringrouter.handlers import default_end


def test_default_end_one_hour():
    assert default_end("2026-11-11 15:00:00", False) == "2026-11-11 16:00:00"


def test_default_end_custom_and_rollover():
    assert default_end("2026-11-11 23:30:00", False, 90) == "2026-11-12 01:00:00"


def test_default_end_all_day():
    assert default_end("2026-11-11", False) == "2026-11-11"
    assert default_end("2026-11-11 00:00:00", True) == "2026-11-11"


def test_default_end_unparseable():
    assert default_end("next tuesday", False) == ""
