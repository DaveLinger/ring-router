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
    cfg.dry_run = False
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
    ctx.cfg.dry_run = True
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


def test_subscription_runs_show_no_cost(ctx):
    res = say(ctx, "Ask Claude on the NAS why Plex is down")
    assert "subscription (API-equivalent ~$0.02)" in res.summary
    ctx.cfg.raw["claude_code"]["billing"] = "api"
    assert say(ctx, "Ask Claude on the old VPS why it's slow").summary.endswith(", $0.020")


def test_claude_inside_a_container(ctx):
    ctx.cfg.raw["hosts"]["truenas"]["claude"] = "sudo docker exec -i -w /root claude-code claude"
    argv, _ = command(ctx, "truenas", ["-p", "why is plex down"])
    import shlex
    remote = shlex.split(argv[-1])[2]                      # bash -lc '<this>'
    assert remote.endswith("&& sudo docker exec -i -w /root claude-code claude -p 'why is plex down'")


# ------------------------------------------------ sudo only during Proceed --

class SwitchFake(FakeClaude):
    """FakeClaude that also answers the proceed-sudo switch (ssh rr-sudo@… "on <id>" / local sudo)."""
    def __init__(self, switch_ok=True):
        super().__init__()
        self.switch, self.switch_ok = [], switch_ok

    def __call__(self, argv, cwd, timeout_s):
        last = argv[-1]
        if "rr-sudo@" in " ".join(argv) or "/usr/local/sbin/ring-router-sudo" in argv:
            act = last.split()[0] if " " in last else argv[-2]
            self.switch.append(act)
            if not self.switch_ok:
                return subprocess.CompletedProcess(argv, 255, "", "Permission denied (publickey).")
            return subprocess.CompletedProcess(argv, 0, "on\n" if act == "on" else "off\n", "")
        return super().__call__(argv, cwd, timeout_s)


def _propose_and_proceed(ctx, phrase):
    ctx.claude_runner.replies = [
        {"result": "Scanners hit 8080.\nPROPOSED FIX: drop CONNECT on 8080 with nftables", "session_id": "S1"},
        {"result": "Done, rule added.", "session_id": "S1"}]
    say(ctx, phrase)
    note = ctx.notifier.sent[-1]
    pid = note.buttons[0].url.split("/confirm/")[1].split("/")[0]
    kind, payload = ctx.store.take_pending(pid)
    return run_confirmed(kind, payload, ctx)


def test_proceed_switches_sudo_on_for_the_run_then_off(ctx):
    ctx.cfg.raw["hosts"]["racknerd"]["proceed_sudo"] = "ssh:rr-sudo@old.example"
    ctx.claude_runner = SwitchFake()
    res = _propose_and_proceed(ctx, "Ask Claude Code on RackNerd about the 500s")
    assert res.state == "done"
    assert ctx.claude_runner.switch == ["on", "off"]
    first = args_of(ctx.claude_runner.calls[0])           # read-only run is told root comes on go-ahead
    assert "passwordless sudo" in first[first.index("-p") + 1]
    second = args_of(ctx.claude_runner.calls[1])
    assert "use `sudo -n`" in second[second.index("-p") + 1]


def test_switch_failure_still_runs_but_says_sudo_is_unavailable(ctx):
    ctx.cfg.raw["hosts"]["racknerd"]["proceed_sudo"] = "ssh:rr-sudo@old.example"
    ctx.claude_runner = SwitchFake(switch_ok=False)
    res = _propose_and_proceed(ctx, "Ask Claude Code on RackNerd about the 500s")
    assert res.state == "done" and ctx.claude_runner.switch == ["on"]      # never on, so no "off"
    msg = args_of(ctx.claude_runner.calls[1])
    assert "could not be switched on" in msg[msg.index("-p") + 1]


def test_local_host_uses_sudo_n_on_the_switch(ctx):
    ctx.cfg.raw["hosts"]["mac_mini"]["proceed_sudo"] = "local"
    ctx.claude_runner = SwitchFake()
    _propose_and_proceed(ctx, "Ask Claude Code on the Mac mini why calsync stopped")
    assert ctx.claude_runner.switch == ["on", "off"]


def test_host_without_proceed_sudo_never_touches_the_switch(ctx):
    ctx.claude_runner = SwitchFake()
    _propose_and_proceed(ctx, "Ask Claude Code on RackNerd about the 500s")
    assert ctx.claude_runner.switch == []
    msg = args_of(ctx.claude_runner.calls[0])
    assert "passwordless sudo" not in msg[msg.index("-p") + 1]


def test_always_sends_the_sudo_notes_without_a_switch(ctx):
    ctx.cfg.raw["hosts"]["truenas"]["proceed_sudo"] = "always"
    ctx.claude_runner = SwitchFake()
    res = _propose_and_proceed(ctx, "Ask Claude on the NAS why Plex is down")
    assert res.state == "done" and ctx.claude_runner.switch == []
    first, second = (args_of(c) for c in ctx.claude_runner.calls)
    assert "may use sudo" in first[first.index("-p") + 1]
    assert "use `sudo -n`" in second[second.index("-p") + 1]


def test_extra_read_tools_and_read_hint_reach_the_read_run(ctx):
    ctx.cfg.raw["hosts"]["truenas"].update(extra_read_tools=["Bash(ssh truenas-ro:*)"],
                                           read_hint="Use `ssh truenas-ro` for host checks.")
    say(ctx, "Ask Claude on the NAS why Plex is down")
    a = args_of(ctx.claude_runner.calls[0])
    tools = a[a.index("--allowedTools") + 1:a.index("--disallowedTools")]
    assert "Bash(ssh truenas-ro:*)" in tools and "Bash(cat:*)" in tools and "Bash" not in tools
    assert "Use `ssh truenas-ro` for host checks." in a[a.index("-p") + 1]
