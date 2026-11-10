"""ring-router command line.

  ring-router init                 create path token + signing secret in .env
  ring-router serve                run the listener
  ring-router route "phrase"       route a phrase offline and run its handler (dry run, console)
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
    for name in ("path_token", "signing_secret"):
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
        if not args.no_run:
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
        h, b = protocol.build_request(p, secret, trigger=args.trigger, include_audio=args.audio,
                                      bad_signature=args.bad_signature)
        code, text = _post(url, h, b)
        print(f"▶ {p}\n  ← {code} {text}")
        if args.replay:
            print("  replay ←", *_post(url, h, b))
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
    s.add_argument("--no-run", action="store_true", help="only show the routing decision"); s.set_defaults(fn=cmd_route)
    s = sub.add_parser("sim"); s.add_argument("phrases", nargs="*")
    s.add_argument("--file", help=f"TOML phrase list (e.g. {PROJECT_DIR / 'tests/phrases.toml'})")
    s.add_argument("--url"); s.add_argument("--trigger", default="single-click-hold")
    s.add_argument("--audio", action="store_true", help="include a fake audio part")
    s.add_argument("--bad-signature", action="store_true")
    s.add_argument("--replay", action="store_true", help="send each request twice (dedupe check)")
    s.add_argument("--test-event", action="store_true"); s.set_defaults(fn=cmd_sim)
    s = sub.add_parser("recent"); s.add_argument("-n", type=int, default=20); s.set_defaults(fn=cmd_recent)

    args = ap.parse_args(argv)
    _setup_logging(args.verbose)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
