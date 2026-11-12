"""Home control: one device catalog (Hubitat, HomeKit or both), commands, confirmations, scenes."""

from __future__ import annotations

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
           tuple(kc.get("homes", [])))
    if _HUB is None or _HUB[0] != key:
        from ..homekit import Home, HomeKit  # noqa: PLC0415
        from ..hubitat import Hubitat  # noqa: PLC0415
        hub = Hubitat(hc["host"], str(hc["app_id"]), token, exclude=hc.get("exclude", [])) if hub_ok else None
        # With Hubitat on, its devices' HomeKit copies are skipped (Hubitat controls them directly).
        skip = kc.get("skip_manufacturers", ["Hubitat"]) if hub_ok else []
        hk = HomeKit(kc["command"], homes=kc.get("homes"), exclude_rooms=kc.get("exclude_rooms", []),
                     skip_manufacturers=skip) if hk_ok else None
        _HUB = (key, Home(hub, hk, aliases=hc.get("aliases", {}), prefer=hc.get("prefer", [])))
    return _HUB[1]


_HUB = None


def run_home(ctx: Context, action: str, spoken: str, *, confirm: bool = False) -> Result:
    """Resolve 'turn off the living room lights' / 'set the fan to low' and run it on Hubitat."""
    from ..hubitat import describe, parse_value, target_of  # noqa: PLC0415
    hub = ctx.hubitat_factory(ctx)
    if hub is None:
        ctx.notify("Not done", "Hubitat isn't configured ([hubitat] host/app_id + token).")
        return Result("failed", "hubitat not configured")
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


def execute_home(ctx: Context, payload: dict, what: str | None = None) -> Result:
    from ..hubitat import describe  # noqa: PLC0415
    hub = ctx.hubitat_factory(ctx)
    labels = payload["labels"]
    what = what or describe(payload["cmd"], payload.get("value"), ", ".join(labels))
    if ctx.dry("home"):
        ctx.notify("Dry run", f"Would: {what}")
        return Result("dry_run", f"DRY RUN: would {what}")
    devs = {d.id: d for d in hub.devices()}
    failed = []
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
        except Exception as e:
            ctx.trace("command_result", device=label, ok=False, error=str(e))
            failed.append(f"{label} ({e})")
    if failed:
        ctx.notify("Home: partly failed" if len(failed) < len(labels) else "Home: failed", "; ".join(failed))
        return Result("failed", f"{what}; failed: {'; '.join(failed)}", 422)
    ctx.notify("Home", what[:1].upper() + what[1:])
    return Result("done", what)


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
    ok = all(x.state in ("done", "dry_run") for x in results)
    return Result("done" if ok and not ctx.dry("scene") else ("dry_run" if ok else "failed"),
                  f"scene {name!r}: " + "; ".join(x.summary for x in results))
