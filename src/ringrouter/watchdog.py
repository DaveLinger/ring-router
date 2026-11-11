"""Down alert: check the listener locally and through the public tunnel; Pushcut on change.

Run every few minutes by launchd (launchd/local.ring-router.watchdog.plist). Alerts after
`fail_threshold` consecutive failures (so a restart doesn't page you), and once more on recovery.
It notifies Pushcut directly, so it works even when the listener itself is down.
Limitation: it runs on the same Mac, so it can't report the Mac itself being off.
"""

from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path

from .notify import Notice, make_notifier


def check(url: str, timeout: float = 10) -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            body = r.read().decode(errors="replace").strip()
            return (r.status == 200 and body == "ok"), f"HTTP {r.status}"
    except Exception as e:
        return False, str(e)[:200]


def diagnose(local_ok: bool, public_ok: bool) -> str:
    if local_ok and public_ok:
        return "up"
    if not local_ok:
        return "listener down"
    return "tunnel down"


def run_once(cfg, notifier=None, checker=check, state_path: Path | None = None) -> str:
    srv = cfg.server
    local_url = f"http://127.0.0.1:{srv.get('port', 8642)}/health"
    public = srv.get("public_url")
    w = cfg.section("watchdog")
    threshold = int(w.get("fail_threshold", 2))
    state_path = state_path or (cfg.state_dir / "watchdog.json")
    try:
        state = json.loads(state_path.read_text())
    except Exception:
        state = {"failures": 0, "alerted": False, "status": "up"}

    local_ok, local_detail = checker(local_url)
    public_ok, public_detail = checker(f"{public}/health") if public else (True, "no public_url")
    status = diagnose(local_ok, public_ok)
    notifier = notifier or make_notifier(cfg)

    if status == "up":
        if state.get("alerted"):
            down_for = int((time.time() - state.get("since", time.time())) / 60)
            notifier.send(Notice("Ring router is back up", f"Recovered after about {down_for} min."))
        state = {"failures": 0, "alerted": False, "status": "up"}
    else:
        state["failures"] = state.get("failures", 0) + 1
        state.setdefault("since", time.time())
        if state["failures"] >= threshold and (not state.get("alerted") or state.get("status") != status):
            detail = local_detail if status == "listener down" else public_detail
            hint = ("launchctl kickstart -k gui/$(id -u)/local.ring-router" if status == "listener down"
                    else "check Tailscale: tailscale funnel status")
            notifier.send(Notice(f"Ring router: {status}",
                                 f"Ring commands won't work. {detail}. Try: {hint}"))
            state["alerted"] = True
        state["status"] = status
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state))
    return f"{status} (local: {local_detail}; public: {public_detail}; failures: {state.get('failures', 0)})"
