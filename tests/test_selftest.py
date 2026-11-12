"""The daily self-test: alert when a check fails, once more when everything passes again."""

from ringrouter.notify import ConsoleNotifier
from ringrouter.selftest import default_probes, run_once


def boom():
    raise RuntimeError("Not authorized to send Apple events to Calendar")


def run(cfg, tmp_path, probes, notifier):
    return run_once(cfg, notifier=notifier, probes=probes, state_path=tmp_path / "selftest.json")


def test_quiet_when_everything_passes(cfg, tmp_path):
    n = ConsoleNotifier(echo=False)
    report = run(cfg, tmp_path, [("Calendar", lambda: "3 calendars"), ("Notes", lambda: "ok")], n)
    assert n.sent == [] and report.count("OK") == 2 and "FAIL" not in report


def test_failure_is_reported_each_day_and_recovery_once(cfg, tmp_path):
    n = ConsoleNotifier(echo=False)
    bad = [("Calendar", boom), ("Notes", lambda: "ok")]
    good = [("Calendar", lambda: "ok"), ("Notes", lambda: "ok")]
    report = run(cfg, tmp_path, bad, n)
    run(cfg, tmp_path, bad, n)
    assert "FAIL Calendar: Not authorized" in report
    assert [x.title for x in n.sent] == ["Ring router self-test: 1 of 2 failed"] * 2 and "Calendar: Not authorized" in n.sent[0].text
    run(cfg, tmp_path, good, n)
    run(cfg, tmp_path, good, n)
    assert [x.title for x in n.sent][2:] == ["Ring router self-test: all passing again"]


def test_the_default_checks_only_read(cfg):
    names = [name for name, _ in default_probes(cfg)]
    assert names == ["Listener", "Calendar", "Reminders", "Notes", "Contacts", "On-device model", "Home devices",
                     "Location-reminder helper"]
