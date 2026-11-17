"""[server] test_mode: every command is worked out (route, rewrite, recipient, times) and then waits
for a Proceed tap. Nothing is sent, changed or asked of a model before the tap."""

import time

import pytest
from test_late_recordings import ago_ms, front_door  # noqa: F401  (reuse the fake Hubitat lock)
from test_server import post, send, server  # noqa: F401  (reuse the live-listener fixture)


@pytest.fixture
def testing(server, cfg):  # noqa: F811
    cfg.raw["server"]["test_mode"] = True
    return server


def labels(n):
    return [b.label for b in n.buttons]


def test_unlock_waits_for_proceed(testing, cfg, front_door):  # noqa: F811
    base, _, notifier = testing
    cfg.dry_run = False
    (code, text), _ = send(base, "Unlock the front door")
    n = notifier.sent[-1]
    assert code == 200 and "needs_confirm" in text and front_door == []
    assert len(notifier.sent) == 1 and n.title == "Test mode" and labels(n) == ["Proceed", "Cancel"]
    assert "Unlock the front door" in n.text and "Front Door" in n.text
    code, text = post(n.buttons[0].url, {}, b"")
    assert code == 200 and text.startswith("done") and front_door == ["/devices/67/unlock"]
    assert post(n.buttons[0].url, {}, b"")[0] == 410                      # one time only


def test_cancel_does_nothing(testing, cfg, front_door):  # noqa: F811
    base, _, notifier = testing
    cfg.dry_run = False
    send(base, "Unlock the front door")
    assert post(notifier.sent[-1].buttons[1].url, {}, b"") == (200, "cancelled\n") and front_door == []


def test_message_preview_shows_the_text_and_proceed_runs_it(testing):
    base, app, notifier = testing
    (_, text), _ = send(base, "Tell Terri I'm on my way")
    n = notifier.sent[-1]
    assert "needs_confirm" in text and "Would send to Terri" in n.text and "I'm on my way" in n.text
    assert "Route: imessage" in n.text and len(notifier.sent) == 1        # the dry-run notice was held back
    code, text = post(n.buttons[0].url, {}, b"")                          # the tests' dry run still applies
    assert code == 200 and text.startswith("dry_run") and notifier.sent[-1].title == "Dry run"


def test_models_are_not_asked_before_proceed(testing):
    base, app, notifier = testing

    class NoModel:
        def chat(self, *a, **kw):
            pytest.fail("a model was asked before Proceed")

    app.llm = NoModel()
    (_, text), _ = send(base, "Ask ChatGPT what the capital of Australia is")
    n = notifier.sent[-1]
    assert "needs_confirm" in text and n.title == "Test mode" and "capital of Australia" in n.text


@pytest.mark.parametrize("say", ["Tell Terri I'll be late. Scrap that.", "Set a timer for 5 minutes"])
def test_commands_that_do_nothing_are_not_held(testing, say):
    base, _, notifier = testing
    (_, text), _ = send(base, say)
    assert "needs_confirm" not in text and all(n.title != "Test mode" for n in notifier.sent)


def test_old_recording_is_held_once(testing, cfg, front_door):  # noqa: F811
    base, _, notifier = testing
    cfg.dry_run = False
    send(base, "Unlock the front door", recorded_at_ms=ago_ms(3 * 3600))
    assert notifier.sent[-1].title == "Test mode"
    code, text = post(notifier.sent[-1].buttons[0].url, {}, b"")          # no second "Old recording" hold
    assert text.startswith("done") and front_door == ["/devices/67/unlock"]


def test_proceed_counts_from_when_it_was_spoken(testing, fake_fm):
    base, _, notifier = testing
    spoken = ago_ms(120)
    send(base, "Remind me in 20 minutes to check the oven", recorded_at_ms=spoken)
    time.sleep(1)
    post(notifier.sent[-1].buttons[0].url, {}, b"")
    assert abs(fake_fm.extract_now.timestamp() - spoken / 1000) < 0.01


@pytest.mark.parametrize("old, test_mode", [("dry_run = true", True), ('dry_run_routes = ["home"]', True),
                                            ("dry_run = false\ndry_run_routes = []", False)])
def test_old_dry_run_settings_never_go_live_quietly(tmp_path, old, test_mode):
    from ringrouter.config import load_config
    p = tmp_path / "config.toml"
    p.write_text(f"[server]\n{old}\n")
    cfg = load_config(p, load_secrets=False)
    assert bool(cfg.server.get("test_mode")) is test_mode and not cfg.is_dry_run("home")
