"""Daily self-test: touch every backend the router depends on, read-only, and say so when one fails.

Run once a day by launchd (launchd/local.ring-router.selftest.plist), through Ring Router.app so
the checks run with the service's own macOS permissions. It exists because the likely way this
breaks is silently: a macOS update resets a privacy grant, the HomeKit bridge stops loading, the
hub changes address, and nothing notices until a command fails.

Nothing is created, sent or switched, and no cloud model is called (so it costs nothing).
A Pushcut alert goes out when any check fails (each day it stays broken) and once when all pass
again. Pushcut itself is the one thing this can't vouch for: if it is down, the alert is too.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace

from . import mcp
from .config import PROJECT_DIR
from .notify import Notice, make_notifier


def default_probes(cfg) -> list[tuple[str, callable]]:
    """(name, function) pairs; a function returns a short detail string or raises."""
    cmd = cfg.section("macos_mcp").get("command", ["npx", "-y", "mcp-macos@4.1.0"])
    env = mcp.macos_mcp_env(cfg)

    def tool(name: str, args: dict):
        return lambda: f"{len(mcp.call_tool(cmd, name, args, timeout_s=45, env=env))} chars"

    def listener():
        with urllib.request.urlopen(f"http://127.0.0.1:{cfg.server.get('port', 8642)}/health", timeout=10) as r:
            body = r.read().decode(errors="replace").strip()
        if body != "ok":
            raise RuntimeError(f"unexpected reply {body[:40]!r}")
        return "ok"

    def on_device_model():
        from .fm import FM  # noqa: PLC0415
        f = cfg.section("fm")
        out = FM(f.get("path", "/usr/bin/fm"), f.get("timeout_s", 15)).respond("Reply with the single word: ready")
        if not out.strip():
            raise RuntimeError("empty reply")
        return out.strip()[:40]

    def home():
        from . import handlers  # noqa: PLC0415
        catalog = handlers.get_hubitat(SimpleNamespace(cfg=cfg))
        if catalog is None:
            return "not configured"
        devices = catalog.devices(refresh=True)
        if catalog.errors:
            raise RuntimeError("; ".join(catalog.errors))
        if not devices:
            raise RuntimeError("no devices listed")
        kinds = sorted({d.backend for d in devices})
        return f"{len(devices)} devices ({', '.join(kinds)})"

    def geo_helper():
        path = Path(cfg.section("reminders").get("georeminder", str(PROJECT_DIR / "build" / "georeminder")))
        if not (path.is_file() and os.access(path, os.X_OK)):
            raise RuntimeError(f"{path} is missing or not executable")
        return "present"

    return [
        ("Listener", listener),
        ("Calendar", tool("calendar_calendars", {"action": "read"})),
        ("Reminders", tool("reminders_lists", {"action": "read"})),
        ("Notes", tool("notes_folders", {"action": "read"})),
        ("Contacts", tool("contacts_people", {"action": "search", "search": "ring-router-selftest-no-such-name"})),
        ("On-device model", on_device_model),
        ("Home devices", home),
        ("Location-reminder helper", geo_helper),
    ]


def run_once(cfg, notifier=None, probes=None, state_path: Path | None = None) -> str:
    state_path = state_path or (cfg.state_dir / "selftest.json")
    try:
        was_failing = bool(json.loads(state_path.read_text()).get("failed"))
    except Exception:
        was_failing = False
    results = []
    for name, fn in (probes if probes is not None else default_probes(cfg)):
        t0 = time.monotonic()
        try:
            results.append((name, True, str(fn())[:120], time.monotonic() - t0))
        except Exception as e:
            results.append((name, False, (str(e) or type(e).__name__)[:200], time.monotonic() - t0))
    failed = [(n, d) for n, ok, d, _ in results if not ok]
    notifier = notifier or make_notifier(cfg)
    if failed:
        notifier.send(Notice(f"Ring router self-test: {len(failed)} of {len(results)} failed",
                             "; ".join(f"{n}: {d}" for n, d in failed)[:900]))
    elif was_failing:
        notifier.send(Notice("Ring router self-test: all passing again", f"All {len(results)} checks passed."))
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"at": time.time(), "failed": [n for n, _ in failed]}))
    return "\n".join(f"{'OK  ' if ok else 'FAIL'} {n}: {d} ({secs:.1f}s)" for n, ok, d, secs in results)
