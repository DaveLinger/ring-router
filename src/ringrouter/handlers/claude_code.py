"""Claude Code on a named host: "ask Claude (Code) on the home server why Plex is down".

Each request runs `claude -p` on the host (locally, or over SSH) and answers by notification:

1. **Investigate** (read-only): Claude Code may read files and run diagnostic commands, nothing that
   changes the host (the allowed tools are a read-only list; Edit/Write are refused outright).
2. If it says a fix needs changes, it ends with `PROPOSED FIX: ...`. The notification then has a
   **Proceed** button, and saying "go ahead" (within the follow-up window) does the same.
3. **Proceed** resumes the *same* Claude Code session with the host's change tools, so it acts on
   the diagnosis it already made instead of starting over.

Follow-up questions to the same host within `[claude_code] followup_window_s` resume that session.
A host with `allow_changes = false` never gets a Proceed button.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import subprocess

from ..llm import Reply
from ..notify import Button
from ..prompts import prompt
from ..router import Route
from .context import Context, Result

log = logging.getLogger(__name__)

# Read-only investigation. Claude Code checks each part of a compound command against this list, and
# in a non-interactive run anything not listed is refused (there is nobody to ask).
READ_TOOLS = [
    "Read", "Grep", "Glob", "WebSearch", "WebFetch",
    "Bash(ls:*)", "Bash(cat:*)", "Bash(head:*)", "Bash(tail:*)", "Bash(grep:*)", "Bash(find:*)", "Bash(wc:*)",
    "Bash(df:*)", "Bash(du:*)", "Bash(free:*)", "Bash(uptime:*)", "Bash(ps:*)", "Bash(top -bn1:*)",
    "Bash(ss:*)", "Bash(netstat:*)", "Bash(ping:*)", "Bash(dig:*)", "Bash(host:*)", "Bash(nslookup:*)",
    "Bash(curl -I:*)", "Bash(curl -sI:*)", "Bash(curl -sSI:*)",
    "Bash(systemctl status:*)", "Bash(systemctl list-units:*)", "Bash(systemctl is-active:*)",
    "Bash(journalctl:*)", "Bash(dmesg:*)", "Bash(nginx -t:*)", "Bash(apachectl -t:*)",
    "Bash(docker ps:*)", "Bash(docker logs:*)", "Bash(docker inspect:*)", "Bash(docker stats --no-stream:*)",
    "Bash(docker compose ps:*)", "Bash(docker compose logs:*)", "Bash(git status:*)", "Bash(git log:*)",
    "Bash(git diff:*)", "Bash(launchctl list:*)", "Bash(launchctl print:*)", "Bash(zpool status:*)",
    "Bash(zfs list:*)", "Bash(midclt call:*)",
]
NEVER_WHEN_READING = ["Edit", "Write", "NotebookEdit", "MultiEdit"]
CHANGE_TOOLS = ["Bash", "Read", "Edit", "Write", "Grep", "Glob", "WebSearch", "WebFetch"]
PROPOSAL = re.compile(r"^\s*\**PROPOSED FIX:?\**:?\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
SESSION_KEY = "claude_session:{host}"
PENDING_KEY = "claude_pending"


def _settings(ctx: Context, host: str) -> tuple[dict, dict]:
    cc = ctx.cfg.section("claude_code")
    h = ctx.cfg.raw.get("hosts", {}).get(host)
    if h is None:
        raise RuntimeError(f"no host {host!r} in [hosts]")
    return cc, h


def command(ctx: Context, host: str, args: list[str]) -> tuple[list[str], str | None]:
    """The argv that runs `claude <args>` on the host, and the local working directory (None for SSH)."""
    cc, h = _settings(ctx, host)
    claude = h.get("claude", cc.get("claude", "claude"))
    workdir = h.get("workdir", "~")
    if not h.get("ssh"):
        return [os.path.expanduser(claude), *args], os.path.expanduser(workdir)
    cd = 'cd "$HOME"' + ("/" + shlex.quote(workdir[2:]) if workdir.startswith("~/") else "") \
        if workdir.startswith("~") else "cd " + shlex.quote(workdir)
    remote = f"{cd} && {shlex.join([claude, *args])}"
    ssh = ["/usr/bin/ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={int(cc.get('ssh_timeout_s', 15))}"]
    ident = cc.get("ssh_identity", "")
    if ident:      # a dedicated key, so a background run never waits on a password manager's prompt
        ssh += ["-i", os.path.expanduser(ident), "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none"]
    return [*ssh, h["ssh"], "bash -lc " + shlex.quote(remote)], None


def run_claude(ctx: Context, host: str, message: str, *, resume: str | None, changes: bool) -> dict:
    """Run one `claude -p` turn on the host; returns its JSON result (result, session_id, cost...)."""
    cc, h = _settings(ctx, host)
    args = ["-p", message, "--output-format", "json", "--max-turns", str(int(cc.get("max_turns", 30))),
            "--append-system-prompt", prompt("claude_code" if not changes else "claude_code_proceed")]
    if changes:
        args += ["--permission-mode", "acceptEdits", "--allowedTools", *h.get("change_tools", CHANGE_TOOLS)]
    else:
        args += ["--permission-mode", "default", "--allowedTools", *h.get("read_tools", cc.get("read_tools", READ_TOOLS)),
                 "--disallowedTools", *NEVER_WHEN_READING]
    if h.get("model", cc.get("model")):
        args += ["--model", h.get("model", cc.get("model"))]
    if resume:
        args += ["--resume", resume]
    argv, cwd = command(ctx, host, args)
    ctx.trace("command", target=f"claude -p on {h.get('display', host)}", changes=changes, resumed=bool(resume),
              via="ssh " + h["ssh"] if h.get("ssh") else "local")
    p = ctx.claude_runner(argv, cwd, int(cc.get("timeout_s", 900)))
    out = None
    for line in reversed(p.stdout.strip().splitlines()):
        try:
            out = json.loads(line)
            break
        except ValueError:
            continue
    if not isinstance(out, dict):
        err = (p.stderr or p.stdout or f"exit {p.returncode}").strip()[-400:]
        raise RuntimeError(f"Claude Code on {h.get('display', host)} didn't answer: {err}")
    return out


def default_runner(argv: list[str], cwd: str | None, timeout_s: int) -> subprocess.CompletedProcess:
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout_s,
                          stdin=subprocess.DEVNULL)


def _summary(text: str, limit: int) -> str:
    first = next((ln.strip(" #*") for ln in text.splitlines() if ln.strip() and not PROPOSAL.match(ln)), "")
    return first if len(first) <= limit else first[:limit - 1].rstrip() + "…"


def _answer(ctx: Context, host: str, question: str, out: dict, *, title: str, proposal_ok: bool) -> Result:
    cc, h = _settings(ctx, host)
    display = h.get("display", host)
    text = str(out.get("result") or "").strip()
    cost = out.get("total_cost_usd")
    if out.get("is_error") or out.get("subtype", "success") != "success":
        ctx.notify(f"Claude Code on {display}", f"Didn't finish: {_summary(text, 200) or out.get('subtype')}")
        return Result("failed", f"claude on {host}: {out.get('subtype')}: {text[:200]}")
    sid = out.get("session_id")
    if sid:
        ctx.store.set_kv(SESSION_KEY.format(host=host), sid)
    # Claude Code reports an API-price estimate on every run. On a Claude subscription (the default,
    # [claude_code] billing = "subscription") nothing is charged: it only counts toward plan limits.
    paid = cc.get("billing", "subscription") == "api"
    label = f"Claude Code on {display}" + ("" if paid else " (subscription)")
    aid = ctx.llm_store.save_answer("claude_code", label, question,
                                    Reply(text, [], label, cost=cost if paid else None))
    buttons = [Button("Full answer", f"{ctx.public_url}/answers/{aid}")]
    proposals = PROPOSAL.findall(text)
    proposal = proposals[-1] if proposals and proposal_ok and h.get("allow_changes", True) and sid else None
    if proposal:
        ttl = int(cc.get("followup_window_s", 900))
        pid = ctx.store.add_pending(ctx.delivery_id, "claude_code",
                                    {"host": host, "session": sid, "proposal": proposal}, ttl)
        ctx.store.set_kv(PENDING_KEY, pid)
        buttons.insert(0, Button("Proceed", f"{ctx.public_url}/confirm/{pid}/yes", background=True))
    limit = int(ctx.cfg.section("notify").get("max_chars", 300))
    body = _summary(text, limit) + (f"\nProposed: {proposal}" if proposal else "")
    ctx.notify(title, body, buttons)
    spent = "" if not isinstance(cost, (int, float)) else (
        f", ${cost:.3f}" if paid else f", subscription (API-equivalent ~${cost:.2f})")
    state = "needs_confirm" if proposal else "done"
    return Result(state, f"claude on {host}: {_summary(text, 120)}" + (f" [proposal: {proposal}]" if proposal else "") + spent)


def h_claude_code(r: Route, ctx: Context) -> Result:
    host = r.params.get("host")
    cc, h = _settings(ctx, host)
    display = h.get("display", host)
    if not r.body:
        ctx.notify("Not done", f"What should Claude Code on {display} look at?")
        return Result("refused", "empty request")
    if h.get("not_ready"):
        ctx.notify("Not done", f"Claude Code on {display} isn't set up yet: {h['not_ready']}")
        return Result("refused", f"host {host} not ready: {h['not_ready']}")
    window = int(cc.get("followup_window_s", 900))
    resume = ctx.store.get_kv(SESSION_KEY.format(host=host), window)
    if ctx.dry("claude_code"):
        ctx.notify("Dry run", f"Would ask Claude Code on {display}{' (follow-up)' if resume else ''}: “{r.body}”")
        return Result("dry_run", f"DRY RUN: would ask Claude Code on {display}: {r.body!r}")
    ctx.notify(f"Claude Code on {display}", f"Looking into it: “{r.body}”")
    try:
        out = run_claude(ctx, host, r.body, resume=resume, changes=False)
    except subprocess.TimeoutExpired:
        ctx.notify(f"Claude Code on {display}", "No answer in time; it was only looking, nothing was changed.")
        return Result("failed", f"claude on {host}: timed out (read-only run)")
    except Exception as e:
        ctx.notify(f"Claude Code on {display}", f"Not asked: {e}")
        return Result("failed", str(e))
    return _answer(ctx, host, r.body, out, title=f"Claude Code on {display}", proposal_ok=True)


def run_proceed(payload: dict, ctx: Context) -> Result:
    """Apply the proposal: resume the investigating session, now allowed to make changes."""
    host = payload["host"]
    _, h = _settings(ctx, host)
    display = h.get("display", host)
    ctx.store.set_kv(PENDING_KEY, "")
    if ctx.dry("claude_code"):
        ctx.notify("Dry run", f"Would let Claude Code on {display} apply: {payload['proposal']}")
        return Result("dry_run", f"DRY RUN: would apply on {host}: {payload['proposal']}")
    ctx.notify(f"Claude Code on {display}", f"Going ahead: {payload['proposal']}")
    try:
        out = run_claude(ctx, host, f"Go ahead with the fix you proposed: {payload['proposal']}",
                         resume=payload["session"], changes=True)
    except subprocess.TimeoutExpired:
        ctx.notify(f"Claude Code on {display}", "No answer in time while making changes. Check the host before retrying.")
        return Result("unknown", f"claude on {host}: timed out while changing")
    except Exception as e:
        ctx.notify(f"Claude Code on {display}", f"Not done: {e}")
        return Result("failed", str(e))
    return _answer(ctx, host, payload["proposal"], out, title=f"Claude Code on {display}: done", proposal_ok=False)


def h_claude_proceed(r: Route, ctx: Context) -> Result:
    """Spoken "go ahead": the one waiting proposal, if there is exactly one and it's still fresh."""
    pid = ctx.store.get_kv(PENDING_KEY, int(ctx.cfg.section("claude_code").get("followup_window_s", 900)))
    claimed = ctx.store.take_pending(pid) if pid else None
    if not claimed or claimed[0] != "claude_code":
        ctx.notify("Not done", "Nothing is waiting for a go-ahead. Ask Claude Code on a host first.")
        return Result("refused", "go-ahead with no waiting Claude Code proposal")
    return run_proceed(claimed[1], ctx)
