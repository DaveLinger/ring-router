"""Home control: one device catalog (Hubitat, HomeKit or both), commands, confirmations, scenes."""

from __future__ import annotations

import time

from ..notify import Button
from ..router import Route
from .context import Context, Result

CMD_TO_ACTION = {"on": "on", "off": "off", "lock": "lock", "unlock": "unlock", "open": "open",
                 "close": "close", "setLevel": "level", "setSpeed": "speed"}


def get_hubitat(ctx: Context):
    """The home device catalog: Hubitat, HomeKit (HomeMCPBridge), or both combined.
    None if neither is configured. Cached per process."""
    global _HUB
    hc, kc = ctx.cfg.section("hubitat"), ctx.cfg.section("homekit")
    token = ctx.cfg.secret("hubitat_token")
    # Hubitat is an optional module: off unless [hubitat] enabled = true. HomeKit (the superset)
    # then handles everything, including devices bridged in from Hubitat.
    hub_ok = bool(hc.get("enabled", False) and hc.get("host") and hc.get("app_id") and token)
    hk_ok = bool(kc.get("command")) and kc.get("enabled", True)
    if not (hub_ok or hk_ok):
        return None
    key = (hub_ok, hc.get("host"), str(hc.get("app_id")), token, hk_ok, tuple(kc.get("command", [])),
           tuple(kc.get("homes", [])), tuple(hc.get("exclude", [])), tuple(kc.get("exclude", [])),
           tuple(ctx.cfg.section("home").get("exclude", [])), repr(_names(ctx)))
    if _HUB is None or _HUB[0] != key:
        from ..homekit import Home, HomeKit  # noqa: PLC0415
        from ..hubitat import Hubitat  # noqa: PLC0415
        # One exclude list for every backend: a device kept out of Hubitat must not come back through HomeKit.
        exclude = [*hc.get("exclude", []), *kc.get("exclude", []), *ctx.cfg.section("home").get("exclude", [])]
        hub = Hubitat(hc["host"], str(hc["app_id"]), token, exclude=exclude) if hub_ok else None
        # With Hubitat on, its devices' HomeKit copies are skipped (Hubitat controls them directly).
        skip = kc.get("skip_manufacturers", ["Hubitat"]) if hub_ok else []
        hk = HomeKit(kc["command"], homes=kc.get("homes"), exclude_rooms=kc.get("exclude_rooms", []),
                     skip_manufacturers=skip, exclude=exclude) if hk_ok else None
        aliases, prefer, room_aliases = _names(ctx)
        _HUB = (key, Home(hub, hk, aliases=aliases, prefer=prefer, room_aliases=room_aliases))
    return _HUB[1]


_HUB = None


def _names(ctx: Context) -> tuple[dict, list, dict]:
    """Spoken nicknames, tie-breakers and room nicknames for every backend: [home] aliases / prefer /
    room_aliases. [hubitat] aliases / prefer are still read (older configs kept them there); [home]
    wins on a clash."""
    hc, home = ctx.cfg.section("hubitat"), ctx.cfg.section("home")
    aliases = {**hc.get("aliases", {}), **home.get("aliases", {})}
    prefer = list(home.get("prefer", [])) + [p for p in hc.get("prefer", []) if p not in home.get("prefer", [])]
    return aliases, prefer, dict(home.get("room_aliases", {}))


def run_home(ctx: Context, action: str, spoken: str, *, confirm: bool = False) -> Result:
    """Resolve 'turn off the living room lights' / 'set the fan to low' and run it on Hubitat."""
    from ..hubitat import describe, parse_value, target_of  # noqa: PLC0415
    hub = ctx.hubitat_factory(ctx)
    if hub is None:
        ctx.notify("Not done", "No home backend is configured ([homekit] command, or [hubitat] enabled with "
                               "host, app_id and a token).")
        return Result("failed", "no home backend configured")
    cmd, value = parse_value(action, spoken)
    target = target_of(spoken)
    hact = CMD_TO_ACTION.get(cmd)
    if not hact:
        ctx.notify("Not done", f"Didn't understand what to do with \u201c{spoken}\u201d.")
        return Result("refused", f"no command for {action!r} {spoken!r}")
    m = hub.match(target, hact)
    ctx.trace("device_match", target=target, action=hact, command=cmd, value=value, kind=m.kind, note=m.note,
              devices=[{"id": d.id, "label": d.label, "room": d.room, "backend": d.backend} for d in m.devices])
    if m.kind == "none":
        down = list(getattr(hub, "errors", None) or [])
        if down:        # a backend didn't answer: the device may well exist, we just can't see it
            ctx.notify("Not done", f"Couldn't reach {'; '.join(down)}. Nothing reachable matches \u201c{target}\u201d.")
            return Result("failed", f"backend down ({'; '.join(down)}); no reachable device matching {target!r}", 422)
        ctx.notify("Not done", f"No device matching \u201c{target}\u201d.")
        return Result("refused", m.note or f"no device matching {target!r}")
    if m.kind == "ambiguous":
        names = ", ".join(d.label for d in m.devices)
        ctx.notify("Which one?", f"\u201c{target}\u201d could be: {names}. Nothing changed.")
        return Result("refused", f"ambiguous {target!r}: {names}")
    labels = [d.label for d in m.devices]
    what = describe(cmd, value, labels[0] if len(labels) == 1 else f"{len(labels)} devices ({', '.join(labels)})")
    payload = {"device_ids": [d.id for d in m.devices], "labels": labels, "cmd": cmd, "value": value}
    if confirm:
        pid = ctx.store.add_pending(ctx.delivery_id, "home", payload)
        ctx.notify("Confirm", f"{what}?", [Button("Yes", f"{ctx.public_url}/confirm/{pid}/yes", background=True),
                                           Button("Cancel", f"{ctx.public_url}/confirm/{pid}/no", background=True)])
        return Result("needs_confirm", f"confirm: {what} ({pid})")
    return execute_home(ctx, payload, what)


# What a lock or door should report once a command has worked. Checked only with [home] read_back.
EXPECTED = {"lock": ("locked", True), "unlock": ("locked", False), "open": ("door", "open"), "close": ("door", "closed")}
_sleep = time.sleep      # replaced in tests


def read_back(ctx: Context, hub, dev, cmd: str) -> tuple[bool | None, str]:
    """Ask the device what state it is in after a lock/unlock/open/close, waiting for it to get
    there. Returns (True, 'locked') when it did, (False, 'unlocked') when it didn't in time, and
    (None, reason) when its state couldn't be read at all."""
    from ..hubitat import describe_state  # noqa: PLC0415
    key, want = EXPECTED[cmd]
    hc = ctx.cfg.section("home")
    deadline = time.monotonic() + float(hc.get("read_back_timeout_s", 20))
    seen, last = "", None
    while True:
        try:
            st = hub.state(dev)
            seen, last = describe_state(st), st.get(key)
            if last == want:
                break
        except Exception as e:
            seen, last = f"couldn't read its state ({e})", None
        if time.monotonic() >= deadline:
            break
        _sleep(float(hc.get("read_back_every_s", 2)))
    ctx.trace("read_back", device=dev.label, command=cmd, wanted=f"{key}={want}", reported=seen, confirmed=last == want)
    return (True if last == want else None if last is None else False), seen


def execute_home(ctx: Context, payload: dict, what: str | None = None) -> Result:
    from ..hubitat import describe  # noqa: PLC0415
    hub = ctx.hubitat_factory(ctx)
    labels = payload["labels"]
    what = what or describe(payload["cmd"], payload.get("value"), ", ".join(labels))
    if ctx.dry("home"):
        ctx.notify("Dry run", f"Would: {what}")
        return Result("dry_run", f"DRY RUN: would {what}")
    devs = {d.id: d for d in hub.devices()}
    check = payload["cmd"] in EXPECTED and bool(ctx.cfg.section("home").get("read_back", False))
    failed, unconfirmed = [], []
    for did, label in zip(payload["device_ids"], labels):
        dev = devs.get(did)
        try:
            if dev is None:
                raise RuntimeError("device no longer available")
            ctx.trace("command", target="HomeKit (HomeMCPBridge)" if dev.backend == "homekit" else "Hubitat Maker API",
                      device=label, command=payload["cmd"],
                      value=payload.get("value"))
            hub.command(dev, payload["cmd"], payload.get("value"))
            ctx.trace("command_result", device=label, ok=True)
            if check:
                ok, seen = read_back(ctx, hub, dev, payload["cmd"])
                if not ok:
                    unconfirmed.append(f"{label} was told to {payload['cmd']} but "
                                       + (f"reports {seen}" if ok is False else seen))
        except Exception as e:
            ctx.trace("command_result", device=label, ok=False, error=str(e))
            failed.append(f"{label} ({e})")
    if failed:
        some = len(failed) < len(labels)
        ctx.notify("Home: partly failed" if some else "Home: failed", "; ".join(failed))
        return Result("partial" if some else "failed", f"{what}; failed: {'; '.join(failed)}", 422)
    if unconfirmed:     # the command went out, the device didn't end up where it should be
        ctx.notify("Home: check it", "; ".join(unconfirmed))
        return Result("partial", f"{what}; not confirmed: {'; '.join(unconfirmed)}", 422)
    done = what[:1].upper() + what[1:] + (" (confirmed)" if check else "")
    ctx.notify("Home", done)
    return Result("done", what + (" (confirmed)" if check else ""))


def home_status(ctx: Context, target: str, limit: int = 8) -> str:
    """What the named device(s) or room report right now, one per line (for the agent's home_status)."""
    from ..hubitat import _words, describe_state  # noqa: PLC0415
    hub = ctx.hubitat_factory(ctx)
    if hub is None:
        return "No home backend is configured."
    devices, tw = hub.devices(), set(_words(target))
    hits = [d for d in devices if tw and tw <= set(_words(d.label)) | set(_words(d.room))]
    if not hits:
        hits = hub.match(target, "").devices
    down = "; ".join(getattr(hub, "errors", None) or [])
    if not hits:
        return (f"Couldn't reach {down}. " if down else "") + f"No device matching '{target}'."
    lines = []
    for d in hits[:limit]:
        try:
            lines.append(f"{d.label} ({d.room or 'no room'}): {describe_state(hub.state(d))}")
        except Exception as e:
            lines.append(f"{d.label}: couldn't read its state ({e})")
    ctx.trace("home_status", target=target, devices=lines)
    more = f"\n(and {len(hits) - limit} more: ask about a room or a device)" if len(hits) > limit else ""
    return "\n".join(lines) + more


def h_home(r: Route, ctx: Context) -> Result:
    return run_home(ctx, r.params["action"], r.body, confirm=bool(r.params.get("confirm")))


def h_scene(r: Route, ctx: Context) -> Result:
    """Exact-phrase scene: a fixed list of [{device = "...", action = "off", value = ...}]."""
    name = r.params["scene"]
    acts = ctx.cfg.raw["scenes"][name].get("actions", [])
    if not acts:
        ctx.notify("Scene", f"Scene \u2018{name}\u2019 has no actions configured.")
        return Result("refused", f"scene {name!r} empty")
    results = []
    for a in acts:
        spoken = a["device"] + (f" to {a['value']}" if a.get("value") is not None else "")
        res = run_home(ctx, a.get("action", "on"), spoken)
        results.append(res)
    states = [x.state for x in results]
    if all(st in ("done", "dry_run") for st in states):
        state = "dry_run" if "dry_run" in states else "done"
    else:       # "failed" only if no step did anything; otherwise the scene is half-run
        state = "partial" if any(st in ("done", "partial") for st in states) else "failed"
    return Result(state, f"scene {name!r}: " + "; ".join(x.summary for x in results))
