"""Claude Code on a host: read-only investigation, Proceed / "go ahead", follow-ups (CLI faked)."""

import json
import subprocess

import pytest

from ringrouter.handlers import Context, run, run_confirmed
from ringrouter.handlers.claude_code import NEVER_WHEN_READING, command
from ringrouter.notify import ConsoleNotifier
from ringrouter.router import route
from ringrouter.store import Store


class FakeClaude:
    def __init__(self):
        self.calls, self.replies = [], []

    def __call__(self, argv, cwd, timeout_s):
        self.calls.append(argv)
        out = self.replies.pop(0) if self.replies else {"result": "All fine.", "session_id": "S1"}
        if isinstance(out, Exception):
            raise out
        body = {"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 0.02, **out}
        return subprocess.CompletedProcess(argv, 0, "some log line\n" + json.dumps(body) + "\n", "")


@pytest.fixture
def ctx(cfg, fake_fm):
    cfg.raw["server"]["dry_run"] = False
    cfg.raw["server"]["dry_run_routes"] = []
    cfg.raw["claude_code"] = {"ssh_identity": "~/.ssh/rr_key", "followup_window_s": 900}
    c = Context(cfg=cfg, notifier=ConsoleNotifier(echo=False), store=Store(":memory:"), fm=fake_fm,
                claude_runner=FakeClaude(), public_url="https://x.example")
    return c


def say(ctx, text):
    return run(route(text, ctx.cfg), ctx)


def args_of(argv):
    """The claude arguments, whether run locally or inside the ssh 'bash -lc' string."""
    if argv[0].endswith("ssh"):
        import shlex
        inner = shlex.split(shlex.split(argv[-1])[2])     # bash -lc '<cd && claude ...>'
        return inner[inner.index("&&") + 1:]
    return argv


def test_investigation_is_read_only_over_ssh(ctx):
    ctx.claude_runner.replies = [{"result": "Plex container is stopped.\nDetails...", "session_id": "S1"}]
    res = say(ctx, "Ask Claude on the home server why Plex is down")
    assert res.state == "done", res.summary
    argv = ctx.claude_runner.calls[0]
    assert argv[:2] == ["/usr/bin/ssh", "-o"] and "IdentityAgent=none" in argv and "truenas" in argv
    a = args_of(argv)
    assert a[a.index("--permission-mode") + 1] == "default"
    assert all(t in a for t in NEVER_WHEN_READING) and "Bash" not in a      # only Bash(<diagnostic>:*)
    assert "--resume" not in a
    note = ctx.notifier.sent[-1]
    assert note.title == "Claude Code on TrueNAS" and note.text == "Plex container is stopped."
    assert [b.label for b in note.buttons] == ["Full answer"]


def test_local_host_runs_without_ssh(ctx):
    say(ctx, "Ask Claude Code on the Mac mini why calsync stopped")
    assert ctx.claude_runner.calls[0][0] == "claude"


def test_proposal_gets_a_proceed_button_and_tap_resumes_with_changes(ctx):
    ctx.claude_runner.replies = [
        {"result": "Plex is stopped after an update.\nPROPOSED FIX: restart the plex container", "session_id": "S1"},
        {"result": "Plex is running again.", "session_id": "S1"}]
    res = say(ctx, "Ask Claude on the NAS why Plex is down")
    assert res.state == "needs_confirm"
    note = ctx.notifier.sent[-1]
    assert note.buttons[0].label == "Proceed" and "Proposed: restart the plex container" in note.text
    pid = note.buttons[0].url.split("/confirm/")[1].split("/")[0]
    kind, payload = ctx.store.take_pending(pid)
    res = run_confirmed(kind, payload, ctx)
    assert res.state == "done" and ctx.notifier.sent[-1].text == "Plex is running again."
    a = args_of(ctx.claude_runner.calls[1])
    assert a[a.index("--resume") + 1] == "S1" and a[a.index("--permission-mode") + 1] == "acceptEdits"
    assert "Bash" in a


def test_spoken_go_ahead_applies_the_waiting_proposal(ctx):
    ctx.claude_runner.replies = [
        {"result": "Disk full.\nPROPOSED FIX: prune old docker images", "session_id": "S9"},
        {"result": "Freed 40 GB.", "session_id": "S9"}]
    say(ctx, "Ask Claude on VPS 2 why the site is down")
    res = say(ctx, "Go ahead")
    assert res.state == "done" and ctx.notifier.sent[-1].text == "Freed 40 GB."
    assert say(ctx, "Go ahead").state == "refused"            # used up: nothing waiting any more


def test_go_ahead_with_nothing_waiting_is_refused(ctx):
    res = say(ctx, "Go ahead")
    assert res.state == "refused" and not ctx.claude_runner.calls


def test_follow_up_resumes_the_same_session(ctx):
    say(ctx, "Ask Claude on the old VPS why the websites are down")
    say(ctx, "Ask Claude on the old VPS what changed last night")
    a = args_of(ctx.claude_runner.calls[1])
    assert a[a.index("--resume") + 1] == "S1"
    assert "--resume" not in args_of(say(ctx, "Ask Claude on the NAS how full the pool is") and ctx.claude_runner.calls[2])


def test_host_without_changes_gets_no_proceed(ctx):
    ctx.cfg.raw["hosts"]["truenas"]["allow_changes"] = False
    ctx.claude_runner.replies = [{"result": "Stopped.\nPROPOSED FIX: restart it", "session_id": "S1"}]
    assert say(ctx, "Ask Claude on the NAS why Plex is down").state == "done"
    assert [b.label for b in ctx.notifier.sent[-1].buttons] == ["Full answer"]


def test_no_json_means_failed(ctx):
    ctx.claude_runner = lambda argv, cwd, t: subprocess.CompletedProcess(argv, 255, "", "Permission denied (publickey).")
    res = say(ctx, "Ask Claude on the NAS why Plex is down")
    assert res.state == "failed" and "Permission denied" in ctx.notifier.sent[-1].text


def test_dry_run_runs_nothing(ctx):
    ctx.cfg.raw["server"]["dry_run"] = True
    assert say(ctx, "Ask Claude on the NAS why Plex is down").state == "dry_run" and not ctx.claude_runner.calls


def test_workdir_is_quoted_for_the_remote_shell(ctx):
    ctx.cfg.raw["hosts"]["truenas"]["workdir"] = "~/my apps"
    argv, cwd = command(ctx, "truenas", ["-p", "hi; rm -rf /"])
    assert cwd is None and argv[-1].startswith("bash -lc ")
    assert "'my apps'" in argv[-1] and "'hi; rm -rf /'" in argv[-1]


def test_host_not_set_up_says_so(ctx):
    ctx.cfg.raw["hosts"]["contabo"]["not_ready"] = "its SSH address isn't configured yet"
    res = say(ctx, "Ask Claude on the new VPS why the site is down")
    assert res.state == "refused" and not ctx.claude_runner.calls
    assert ctx.notifier.sent[-1].text == "Claude Code on Contabo VPS isn't set up yet: its SSH address isn't configured yet"
