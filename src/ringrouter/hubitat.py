"""Hubitat via the local Maker API: device list, spoken-name matching, commands.

Matching is deterministic (no LLM): the spoken target is compared with device labels and
config aliases, word by word, and candidates are narrowed by what the action needs (unlock →
locks, fan speed → fans, dim → dimmers). Ambiguous → ask, never guess.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
import urllib.request
from dataclasses import dataclass, field

from .text import similarity

# action -> capability a device must have
ACTION_CAPS = {"on": "Switch", "off": "Switch", "level": "SwitchLevel", "speed": "FanControl",
               "lock": "Lock", "unlock": "Lock", "open": "DoorControl", "close": "DoorControl"}
CONTROL_CAPS = {"Switch", "SwitchLevel", "FanControl", "Lock", "DoorControl", "GarageDoorControl"}
LIGHT_WORDS = {"light", "lamp", "sconce", "pendant", "bulb"}      # singular; _words() drops the plural s
FAN_SPEEDS = {"low": "low", "medium low": "medium-low", "medium-low": "medium-low", "medium": "medium",
              "medium high": "medium-high", "medium-high": "medium-high", "high": "high", "max": "high",
              "full": "high", "auto": "auto"}
FILLER = {"the", "my", "a", "please", "all", "of", "in", "on", "to", "and"}
DEFAULT_EXCLUDE_WORDS = ("detector", "smoke", "alarm", "siren")


@dataclass
class Device:
    id: str
    label: str
    room: str
    caps: set[str]
    attrs: dict = field(default_factory=dict)
    backend: str = "hubitat"          # hubitat | homekit

    @property
    def is_outlet(self) -> bool:
        """A plug: left alone by "everything" (it may be feeding a fridge or a server), fine by name."""
        return (self.attrs.get("type") == "outlet" or "Outlet" in self.caps
                or bool({"outlet", "plug"} & set(_words(self.label))))

    @property
    def is_light(self) -> bool:
        return bool(LIGHT_WORDS & set(_words(self.label))) or "light" in self.label.lower()


def excluded(label: str, names: set[str]) -> bool:
    """Never controlled: a label on the exclude list, or one naming a detector, alarm or siren."""
    low = label.lower()
    return low in names or any(w in low for w in DEFAULT_EXCLUDE_WORDS)


@dataclass
class Match:
    devices: list[Device]
    kind: str            # device | room | all | ambiguous | none
    note: str = ""


def _words(s: str) -> list[str]:
    ws = re.findall(r"[a-z0-9]+", s.lower())
    out = []
    for w in ws:
        if w in FILLER:
            continue
        if w.endswith("s") and len(w) > 3 and w not in ("blinds",):   # lights -> light, fans -> fan
            w = w[:-1]
        out.append(w)
    return out


class Catalog:
    """Spoken-name matching over a device list (Hubitat, HomeKit, or both combined)."""

    def __init__(self, *, aliases: dict | None = None, prefer: list[str] | None = None,
                 room_aliases: dict | None = None):
        self.aliases = {k.lower(): v for k, v in (aliases or {}).items()}
        # spoken room name -> the room's real name, anywhere in a phrase: "guest bedroom fan" -> "guest room fan"
        self.room_aliases = {k.lower(): v.lower() for k, v in (room_aliases or {}).items()}
        # tie-breakers when a name matches several devices, e.g. ["ceiling fan"]: "fan" -> the ceiling fan
        self.prefer = [p.lower() for p in (prefer or [])]

    def devices(self, refresh: bool = False) -> list[Device]:
        raise NotImplementedError

    def command(self, device: Device, cmd: str, value: str | None = None) -> dict:
        raise NotImplementedError

    def state(self, device: Device) -> dict:
        """Current state, in one shape for every backend. Keys present only when known:
        on (bool), level (0-100), locked (bool), door (open|closed|opening|closing|stopped),
        speed (str), reachable (bool)."""
        raise NotImplementedError

    def rooms(self) -> set[str]:
        return {d.room for d in self.devices() if d.room}

    def match(self, target: str, action: str) -> "Match":
        return _match(self, target, action)


class Hubitat(Catalog):
    def __init__(self, host: str, app_id: str, token: str, *, exclude: list[str] | None = None,
                 aliases: dict | None = None, prefer: list[str] | None = None, fetch=None, cache_s: int = 300):
        super().__init__(aliases=aliases, prefer=prefer)
        self.base = f"http://{host}/apps/api/{app_id}"
        self.token, self.exclude = token, {e.lower() for e in (exclude or [])}
        self._fetch = fetch or self._http
        self.cache_s, self._cache, self._cache_t = cache_s, None, 0.0

    # -- transport ---------------------------------------------------------
    def _http(self, path: str):
        """GET via /usr/bin/curl. macOS Local Network privacy tracks grants unreliably for a
        launchd-run Python ("No route to host"), while Apple's curl under Ring Router.app gets
        through consistently. The token goes in via stdin config, not argv (not visible in ps)."""
        cfg = f'header = "Authorization: Bearer {self.token}"\n'
        p = subprocess.run(["/usr/bin/curl", "-sS", "-m", "15", "-K", "-", "-w", "\n%{http_code}", self.base + path],
                           input=cfg, capture_output=True, text=True, timeout=20)
        if p.returncode != 0:
            raise RuntimeError(f"Hubitat unreachable: {p.stderr.strip() or 'curl exit ' + str(p.returncode)}")
        body, _, code = p.stdout.rpartition("\n")
        if code != "200":
            raise RuntimeError(f"Hubitat HTTP {code}: {body[:200]}")
        return json.loads(body) if body.strip() else {}

    # -- devices -------------------------------------------------------------
    def devices(self, refresh: bool = False) -> list[Device]:
        if refresh or self._cache is None or time.time() - self._cache_t > self.cache_s:
            out = []
            for d in self._fetch("/devices/all"):
                label = d.get("label") or d.get("name") or ""
                caps = {c if isinstance(c, str) else next(iter(c)) for c in d.get("capabilities", [])}
                if excluded(label, self.exclude):
                    continue
                if not caps & CONTROL_CAPS:
                    continue
                out.append(Device(str(d["id"]), label, d.get("room") or "", caps, d.get("attributes") or {}))
            self._cache, self._cache_t = out, time.time()
        return self._cache

    def state(self, device: Device) -> dict:
        info = self._fetch(f"/devices/{device.id}")
        attrs = {a.get("name"): a.get("currentValue") for a in info.get("attributes", []) if isinstance(a, dict)}
        out: dict = {}
        if attrs.get("switch") in ("on", "off"):
            out["on"] = attrs["switch"] == "on"
        if str(attrs.get("level", "")).isdigit():
            out["level"] = int(attrs["level"])
        if attrs.get("lock") in ("locked", "unlocked"):
            out["locked"] = attrs["lock"] == "locked"
        if attrs.get("door"):
            out["door"] = str(attrs["door"])
        if attrs.get("speed"):
            out["speed"] = str(attrs["speed"])
        return out

    # -- commands ------------------------------------------------------------
    def command(self, device: Device, cmd: str, value: str | None = None) -> dict:
        path = f"/devices/{device.id}/{cmd}" + (f"/{urllib.request.quote(str(value))}" if value is not None else "")
        return self._fetch(path)     # the catalog holds names and capabilities, not state: no need to refetch


def _match(self, target: str, action: str) -> Match:
    """Find the device(s) a spoken target means, for an action."""
    need = ACTION_CAPS.get(action)
    pool = [d for d in self.devices() if not need or need in d.caps or
            (need == "DoorControl" and "GarageDoorControl" in d.caps)]
    t = target.lower().strip()
    tw = _words(t)
    if not tw:
        return Match([], "none", "no device named")

    if action in ("open", "close") and tw == ["door"]:
        # "open the door" names no door, and the only thing that opens is the garage: ask, don't guess
        return Match(pool[:4], "ambiguous" if pool else "none", "which door?")

    def by_alias(phrase: str):
        alias = self.aliases.get(re.sub(r"^(the|my)\s+", "", phrase))
        hits = [d for d in pool if alias and d.label.lower() == alias.lower()]
        return Match(hits, "device", f"alias {phrase!r}") if hits else None

    if (m := by_alias(t)):
        return m
    for spoken in sorted(self.room_aliases, key=len, reverse=True):
        t = re.sub(rf"\b{re.escape(spoken)}\b", self.room_aliases[spoken], t)
    tw = _words(t)
    if (m := by_alias(t)):
        return m

    # "all the lights" / "the lights" / "everything"
    everything = tw == ["everything"]
    if everything or (t.startswith(("all ", "every ")) and tw == ["light"]):
        hits = [d for d in pool if (not d.is_outlet if everything else d.is_light)]
        return Match(hits, "all" if hits else "none", "everything" if everything else "all lights")

    # exact label
    exact = [d for d in pool if _words(d.label) == tw]
    if len(exact) == 1:
        return Match(exact, "device")

    # room + lights ("the living room lights", "living room")
    for room in sorted(self.rooms(), key=len, reverse=True):
        rw = _words(room)
        if rw and tw[:len(rw)] == rw:
            rest = tw[len(rw):]
            in_room = [d for d in pool if d.room == room]
            if rest in ([], ["light"]) and action in ("on", "off", "level"):
                lights = [d for d in in_room if d.is_light]
                if t.endswith("lights") or rest == [] or len(lights) == 1:
                    if lights:
                        return Match(lights, "room", f"lights in {room}")

    # word-overlap scoring. A spoken room keeps the search inside that room when anything there fits,
    # so "the guest room ceiling fan" can't land on the living room's ceiling fan.
    spoken_room = next((r for r in sorted(self.rooms(), key=len, reverse=True)
                        if _words(r) and tw[:len(_words(r))] == _words(r)), None)
    in_room = [d for d in pool if d.room == spoken_room] if spoken_room else []
    m = _score(self, in_room, tw, target) if in_room else None
    return m if m and m.kind != "none" else _score(self, pool, tw, target)


def _score(self, pool: list, tw: list[str], target: str) -> Match:
    scored = []
    for d in pool:
        lw = set(_words(d.label)) | set(_words(d.room))
        overlap = sum(1 for w in tw if w in lw) / len(tw)
        score = overlap + 0.25 * similarity(" ".join(tw), " ".join(_words(d.label)))
        scored.append((score, overlap, d))
    scored.sort(key=lambda x: -x[0])
    if not scored or scored[0][1] < 0.6:
        return Match([], "none", f"no device matching {target!r}")
    best = scored[0]
    close = [s for s in scored if s[1] == best[1] and best[0] - s[0] < 0.15]
    if len(close) > 1:
        for pref in self.prefer:
            picked = [s[2] for s in close if pref in s[2].label.lower()]
            if len(picked) == 1:
                return Match(picked, "device", f"preferred {pref!r}")
        return Match([s[2] for s in close[:4]], "ambiguous")
    return Match([best[2]], "device")


def describe_state(st: dict) -> str:
    """'locked' / 'closed' / 'on, 40%' / 'not responding' for a state() result."""
    if st.get("reachable") is False:
        return "not responding"
    parts = []
    if "locked" in st:
        parts.append("locked" if st["locked"] else "unlocked")
    if "door" in st:
        parts.append(st["door"])
    if "on" in st:
        parts.append("on" if st["on"] else "off")
    if st.get("on") and "level" in st:
        parts.append(f"{st['level']}%")
    if st.get("on") and st.get("speed") not in (None, "off", "on"):
        parts.append(f"speed {st['speed']}")
    return ", ".join(parts) or "state unknown"


def parse_value(action: str, rest: str) -> tuple[str, str | None]:
    """Map the router's action + spoken remainder to (command, value).

    "set the fan to low" -> ("setSpeed", "low"); "dim the sconces to 30%" -> ("setLevel", "30")
    """
    low = rest.lower()
    m = re.search(r"\bto\s+(.+)$", low)
    val = m.group(1).strip(" .%") if m else ""
    num = re.search(r"(\d{1,3})\s*(%|percent)?", val)
    if action in ("lock", "unlock", "on", "off", "open", "close"):
        return action, None
    if val in ("on", "off"):
        return val, None
    speed = FAN_SPEEDS.get(val.replace("speed", "").strip())
    if speed:
        return "setSpeed", speed
    if num:
        return "setLevel", str(max(0, min(100, int(num.group(1)))))
    if action == "dim":
        return "setLevel", "30"
    if action == "brighten":
        return "setLevel", "100"
    return "", None


def target_of(rest: str) -> str:
    """Spoken device part: 'the master bedroom fan to low' -> 'the master bedroom fan'."""
    return re.split(r"\s+to\s+", rest.strip(" ."), maxsplit=1, flags=re.IGNORECASE)[0].strip()


def describe(cmd: str, value: str | None, label: str) -> str:
    if cmd in ("on", "off"):
        return f"{label} {cmd}"
    if cmd in ("lock", "unlock"):
        return f"{label} {cmd}ed"
    if cmd == "setLevel":
        return f"{label} set to {value}%"
    if cmd == "setSpeed":
        return f"{label} speed {value}"
    return f"{label}: {cmd}" + (f" {value}" if value else "")
