"""ring-router command line.

  ring-router init                 create path token + signing secret in .env
  ring-router serve                run the listener
  ring-router route "phrase"       route a phrase and run its handler in dry run (cloud-model routes
                                   only with --online)
  ring-router sim "phrase" ...     send phrases to the running listener exactly as the ring would
  ring-router sim --file tests/phrases.toml
  ring-router recent               show recent deliveries
"""

from __future__ import annotations

import argparse
import logging
import secrets
import sys
import time
import tomllib
import urllib.error
import urllib.request

from . import protocol
from .config import ENV_FILE, PROJECT_DIR, load_config, read_secrets, write_secret


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")


def cmd_init(args) -> int:
    have = read_secrets()
    for name in ("path_token", "signing_secret", "activity_token"):
        if name in have and not args.force:
            print(f"{name}: already set (use --force to replace)")
            continue
        write_secret(name, secrets.token_urlsafe(32))
        print(f"{name}: created in {ENV_FILE} (mode 600)")
    print("Optional secrets: ring-router set-secret openrouter_api_key | pushcut_webhook_url | hubitat_token")
    print("(or edit .env directly: RING_ROUTER_<NAME>=value)")
    return 0


def cmd_set_secret(args) -> int:
    import getpass  # noqa: PLC0415
    write_secret(args.name, getpass.getpass(f"{args.name}: "))
    print("saved")
    return 0


def cmd_serve(args) -> int:
    from .server import serve  # noqa: PLC0415
    serve(load_config(args.config))
    return 0


def cmd_route(args) -> int:
    from .handlers import SLOW_ROUTES  # noqa: PLC0415
    from .router import route  # noqa: PLC0415
    from .server import App  # noqa: PLC0415
    from .store import Store  # noqa: PLC0415
    cfg = load_config(args.config)
    cfg.raw.setdefault("server", {})["dry_run"] = True  # offline routing never acts
    app = App(cfg, store=Store(":memory:"), background=False)
    from .notify import ConsoleNotifier  # noqa: PLC0415
    app.notifier = ConsoleNotifier()
    for phrase in args.phrases:
        r = route(phrase, cfg)
        print(f"▶ {phrase}\n  {r.describe()}")
        if args.no_run:
            continue
        if r.route in SLOW_ROUTES and not args.online:   # these call a cloud model: real requests, real money
            print("  → not run: this route calls a cloud model (add --online to run it)")
            continue
        code, text = app.process(f"local-{time.time_ns()}", phrase)
        print(f"  → {code} {text}")
    return 0


def _post(url: str, headers: dict, body: bytes) -> tuple[int, str]:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read().decode().strip()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode().strip()


def cmd_sim(args) -> int:
    cfg = load_config(args.config)
    secret, token = cfg.secret("signing_secret"), cfg.secret("path_token")
    if not (secret and token):
        print("No path_token/signing_secret. Run `ring-router init` first.", file=sys.stderr)
        return 2
    base = args.url or f"http://{cfg.server.get('host', '127.0.0.1')}:{cfg.server.get('port', 8642)}"
    url = f"{base}/hook/{token}"
    phrases = list(args.phrases)
    if args.file:
        with open(args.file, "rb") as f:
            phrases += [c["say"] for c in tomllib.load(f).get("case", [])]
    if args.test_event:
        h, b = protocol.build_request(None, secret, trigger="test-event", is_test=True)
        print("test event →", *_post(url, h, b))
    for p in phrases:
        ago = {"recorded_at_ms": int((time.time() - args.recorded_ago) * 1000)} if args.recorded_ago else {}
        h, b = protocol.build_request(p, secret, trigger=args.trigger, include_audio=args.audio,
                                      bad_signature=args.bad_signature, **ago)
        code, text = _post(url, h, b)
        print(f"▶ {p}\n  ← {code} {text}")
        if args.replay:
            print("  replay ←", *_post(url, h, b))
    return 0


def cmd_check_permissions(args) -> int:
    """Touch each macOS app once so its permission prompt appears for *this* process tree.

    Run it the way the service runs (launchd), so approvals attach to the service's binaries.
    Everything is a read except one iMessage to the test recipient (--message-to).
    """
    import subprocess  # noqa: PLC0415
    from . import mcp  # noqa: PLC0415
    cfg = load_config(args.config)
    cmd = cfg.section("macos_mcp").get("command", ["npx", "-y", "mcp-macos@4.1.0"])
    env = mcp.macos_mcp_env(cfg)
    probes = [
        ("Calendars", lambda: mcp.call_tool(cmd, "calendar_calendars", {"action": "read"}, env=env)),
        ("Reminders", lambda: mcp.call_tool(cmd, "reminders_lists", {"action": "read"}, env=env)),
        ("Notes (mcp)", lambda: mcp.call_tool(cmd, "notes_folders", {"action": "read"}, env=env)),
        ("Notes (AppleScript)", lambda: subprocess.run(
            ["/usr/bin/osascript", "-e", 'tell application "Notes" to count folders'],
            check=True, capture_output=True, text=True, timeout=120).stdout),
        ("Contacts", lambda: mcp.call_tool(cmd, "contacts_people", {"action": "search", "search": "Dave"}, env=env)),
    ]
    hub = cfg.section("hubitat")
    if hub.get("host") and cfg.secret("hubitat_token"):
        def hubitat_probe():
            import json as _j  # noqa: PLC0415
            import urllib.request as _u  # noqa: PLC0415
            req = _u.Request(f"http://{hub['host']}/apps/api/{hub['app_id']}/devices",
                             headers={"Authorization": f"Bearer {cfg.secret('hubitat_token')}"})
            # Local Network privacy may not register for a process that fails and exits at once
            # (Apple DTS): on failure, stay alive and retry before giving up.
            last = None
            for attempt in range(3):
                try:
                    with _u.urlopen(req, timeout=15) as r:
                        return f"{len(_j.load(r))} devices visible (attempt {attempt + 1})"
                except Exception as e:  # noqa: PERF203
                    last = e
                    time.sleep(10)
            raise last
        probes.append(("Hubitat (local network)", hubitat_probe))

        def hubitat_curl_probe():
            out = subprocess.run(["/usr/bin/curl", "-s", "-m", "10", "-o", "/dev/null", "-w", "%{http_code}",
                                  "-H", f"Authorization: Bearer {cfg.secret('hubitat_token')}",
                                  f"http://{hub['host']}/apps/api/{hub['app_id']}/devices"],
                                 capture_output=True, text=True, timeout=20).stdout
            if out != "200":
                raise RuntimeError(f"curl HTTP {out or 'no response'}")
            return "HTTP 200 via /usr/bin/curl"
        probes.append(("Hubitat via curl", hubitat_curl_probe))
    rc = cfg.raw.get("recipients", {}).get(args.message_to) if args.message_to else None
    if rc:
        to = rc.get("handle")
        probes.append(("Messages (send)", lambda: mcp.call_tool(cmd, "messages_chat", {
            "action": "create", "to": to, "text": "ring-router permission check: if you see this, sending works."}, env=env)))
    ok = True
    for name, fn in probes:
        try:
            out = str(fn()).strip().replace("\n", " ")
            print(f"OK    {name}: {out[:400]}", flush=True)
        except Exception as e:
            ok = False
            print(f"FAIL  {name}: {str(e)[:300]}", flush=True)
    return 0 if ok else 1


def cmd_watchdog(args) -> int:
    from .logfile import trim_own_log  # noqa: PLC0415
    from .watchdog import run_once  # noqa: PLC0415
    trim_own_log()
    print(time.strftime("%F %T"), run_once(load_config(args.config)), flush=True)
    return 0


def cmd_recent(args) -> int:
    from .store import Store  # noqa: PLC0415
    cfg = load_config(args.config, load_secrets=False)
    for did, state, rt, tr, summ, upd in Store(cfg.state_dir / "state.db").recent(args.n):
        print(f"{time.strftime('%m-%d %H:%M:%S', time.localtime(upd))} {state:13} {rt or '':11} "
              f"{(tr or '')[:50]!r} {summ or ''}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="ring-router")
    ap.add_argument("--config", help="config file (default: config.toml, else config.example.toml)")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init"); s.add_argument("--force", action="store_true"); s.set_defaults(fn=cmd_init)
    s = sub.add_parser("set-secret"); s.add_argument("name"); s.set_defaults(fn=cmd_set_secret)
    s = sub.add_parser("serve"); s.set_defaults(fn=cmd_serve)
    s = sub.add_parser("route"); s.add_argument("phrases", nargs="+")
    s.add_argument("--no-run", action="store_true", help="only show the routing decision")
    s.add_argument("--online", action="store_true", help="also run routes that call a cloud model (costs money)")
    s.set_defaults(fn=cmd_route)
    s = sub.add_parser("sim"); s.add_argument("phrases", nargs="*")
    s.add_argument("--file", help=f"TOML phrase list (e.g. {PROJECT_DIR / 'tests/phrases.toml'})")
    s.add_argument("--url"); s.add_argument("--trigger", default="single-click-hold")
    s.add_argument("--audio", action="store_true", help="include a fake audio part")
    s.add_argument("--bad-signature", action="store_true")
    s.add_argument("--replay", action="store_true", help="send each request twice (dedupe check)")
    s.add_argument("--recorded-ago", type=float, default=0, metavar="SECONDS",
                   help="pretend the recording was made this long ago (old-recording check)")
    s.add_argument("--test-event", action="store_true"); s.set_defaults(fn=cmd_sim)
    s = sub.add_parser("check-permissions", help="trigger/verify macOS permission prompts")
    s.add_argument("--message-to", help="recipient key for one test iMessage (e.g. dave)")
    s.set_defaults(fn=cmd_check_permissions)
    s = sub.add_parser("watchdog", help="health check; Pushcut alert when the listener or tunnel is down")
    s.set_defaults(fn=cmd_watchdog)
    s = sub.add_parser("recent"); s.add_argument("-n", type=int, default=20); s.set_defaults(fn=cmd_recent)

    args = ap.parse_args(argv)
    _setup_logging(args.verbose)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
