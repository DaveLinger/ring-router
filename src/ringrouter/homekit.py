"""HomeKit via HomeMCPBridge (patched build in vendor/HomeMCPBridge), plus the combined catalog.

HomeKit is the superset (it also sees the Hubitat devices, bridged in). The combined catalog uses
Hubitat for devices it has (direct local HTTP) and HomeKit only for HomeKit-native devices:
HomeKit entries whose manufacturer is Hubitat are bridged duplicates and are skipped.
"""

from __future__ import annotations

import json
import os
import time
import unicodedata

from . import mcp
from .hubitat import Catalog, Device

TYPE_CAPS = {
    "light": {"Switch", "SwitchLevel"},
    "switch": {"Switch"},
    "outlet": {"Switch"},
    "fan": {"Switch"},
    "lock": {"Lock"},
    "garage_door": {"DoorControl"},
}
# router command -> HomeMCPBridge control_device action
HK_ACTIONS = {"on": "on", "off": "off", "lock": "lock", "unlock": "unlock", "open": "open", "close": "close",
              "setLevel": "brightness"}


def _norm(s: str) -> str:
    """Case/apostrophe-insensitive compare ("Mom’s House" == "mom's house")."""
    return unicodedata.normalize("NFKC", s).replace("’", "'").strip().lower()


class HomeKit:
    def __init__(self, command: list[str], *, homes: list[str] | None = None, exclude_rooms: list[str] | None = None,
                 skip_manufacturers: list[str] | None = None, call=None, cache_s: int = 300):
        self.bridge_cmd = command
        self.env = dict(os.environ, HOMEMCP_HOMES=",".join(homes or ["primary"]))
        self.exclude_rooms = {_norm(r) for r in (exclude_rooms or [])}
        self.skip_manufacturers = [_norm(m) for m in (skip_manufacturers or ["hubitat"])]
        self._call = call or (lambda tool, args: mcp.call_tool(self.bridge_cmd, tool, args, timeout_s=60, env=self.env))
        self.cache_s, self._cache, self._cache_t = cache_s, None, 0.0

    def devices(self, refresh: bool = False) -> list[Device]:
        if refresh or self._cache is None or time.time() - self._cache_t > self.cache_s:
            data = json.loads(self._call("list_devices", {}))
            out = []
            for d in data.get("devices", []):
                caps = TYPE_CAPS.get(d.get("type", ""))
                if not caps or d.get("source", "HomeKit") != "HomeKit":
                    continue
                if _norm(d.get("room") or "") in self.exclude_rooms:
                    continue
                if any(m in _norm(d.get("manufacturer") or "") for m in self.skip_manufacturers):
                    continue      # bridged in from Hubitat: Hubitat controls it directly
                out.append(Device(id=d["id"], label=d.get("name", ""), room=d.get("room") or "", caps=set(caps),
                                  backend="homekit"))
            self._cache, self._cache_t = out, time.time()
        return self._cache

    def command(self, device: Device, cmd: str, value: str | None = None) -> dict:
        action = HK_ACTIONS.get(cmd)
        if not action:
            raise RuntimeError(f"HomeKit can't {cmd} {device.label}")
        args = {"name": device.id, "action": action}          # patched bridge accepts the exact id
        if value is not None:
            args["value"] = int(value) if str(value).isdigit() else value
        out = self._call("control_device", args)
        try:
            res = json.loads(out)
        except ValueError:
            res = {"raw": out}
        if res.get("success") is False or res.get("error"):
            raise RuntimeError(res.get("error") or f"HomeKit refused {action} on {device.label}")
        self._cache = None
        return res


class Home(Catalog):
    """Hubitat + HomeKit as one catalog; commands go to each device's own backend."""

    def __init__(self, hubitat=None, homekit=None, *, aliases=None, prefer=None):
        super().__init__(aliases=aliases, prefer=prefer)
        self.hubitat, self.homekit = hubitat, homekit
        self.errors: list[str] = []

    def devices(self, refresh: bool = False) -> list[Device]:
        out, self.errors = [], []
        for name, backend in (("Hubitat", self.hubitat), ("HomeKit", self.homekit)):
            if backend is None:
                continue
            try:
                out.extend(backend.devices(refresh))
            except Exception as e:     # one backend down shouldn't take the other with it
                self.errors.append(f"{name}: {e}")
        return out

    def command(self, device: Device, cmd: str, value: str | None = None) -> dict:
        backend = self.homekit if device.backend == "homekit" else self.hubitat
        return backend.command(device, cmd, value)
