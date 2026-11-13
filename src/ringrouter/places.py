"""Named places for location reminders: "when I get to the gym", "when I leave work".

Pure functions of (text, config): the router uses them to decide that a reminder is about a place,
and the reminder handler uses the same parse to build the geofence.

Home is built in (the address on your Contacts "My Card"). Other places come from [places.<key>]:

    [places.gym]
    name = "Gym"                        # shown in notifications and on the reminder
    spoken = ["the gym", "gym"]         # what you say; "the"/"my" in front is always allowed
    address = "1 Fitness Way, Town, ST"  # or lat = .. / lon = .., or contact_label = "work"
    radius_m = 150                      # optional; default [reminders] geofence_radius_m

An unknown place is never guessed: the reminder is refused and the known places are listed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

HOME_SPOKEN = ["home", "the house", "my house", "the home"]


@dataclass
class Place:
    key: str
    name: str
    spoken: list[str]
    address: str | None = None
    lat: float | None = None
    lon: float | None = None
    contact_label: str | None = None      # an address on the Contacts My Card, by label
    radius_m: int | None = None

    @property
    def is_home(self) -> bool:
        return self.key == "home"


@dataclass
class Geo:
    title: str
    proximity: str                        # enter | leave
    place: Place | None                   # None: a place was named but isn't configured
    heard: str = ""                       # the words taken as the place name
    notes: list[str] = field(default_factory=list)


def places(raw: dict) -> list[Place]:
    """Home (always) plus every [places.<key>] entry. [places.home] may override home's settings."""
    cfg = raw.get("places", {}) or {}
    home_cfg = cfg.get("home", {})
    out = [Place("home", home_cfg.get("name", "Home"),
                 _spoken(home_cfg.get("spoken", []) + HOME_SPOKEN),
                 home_cfg.get("address"), home_cfg.get("lat"), home_cfg.get("lon"),
                 home_cfg.get("contact_label", "home"), home_cfg.get("radius_m"))]
    for key, p in cfg.items():
        if key == "home" or not isinstance(p, dict):
            continue
        name = p.get("name") or key.replace("_", " ").title()
        out.append(Place(key, name, _spoken(list(p.get("spoken", [])) + [key.replace("_", " "), name]),
                         p.get("address"), p.get("lat"), p.get("lon"), p.get("contact_label"), p.get("radius_m")))
    return out


def _spoken(names: list[str]) -> list[str]:
    seen, out = set(), []
    for n in names:
        n = re.sub(r"^(?:the|my)\s+", "", n.strip().lower())
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


# After "when I": (pattern, proximity, slot). slot = "need": a place must follow; "maybe": a place may
# follow (otherwise home, unless the words after it name somewhere else); "home": the trigger is home;
# "back": "get back to <known place>", otherwise "get back" (home) and the "to ..." is the reminder.
_I = r"\bwhen\s+i"
TRIGGERS = [
    (_I + r"\s+(?:get|come|go|head|make\s+it)\s+back\s+from\s+", "enter", "need"),      # "...back from vacation"
    (_I + r"(?:'m|\s+am)\s+(?:back\s+)?(?:at|in)\s+", "enter", "need"),                # "when I'm at the gym"
    (_I + r"\s+(?:get|come|go|head|make\s+it)\s+back\s+to\s+", "enter", "back"),          # "...back to the office"
    (_I + r"\s+(?:get|come|go|head|make\s+it)\s+to\s+", "enter", "need"),                  # "when I get to work"
    (_I + r"\s+(?:arrive|get)\s+(?:back\s+)?(?:at|in)\s+", "enter", "need"),           # "when I arrive at the office"
    (_I + r"\s+reach\s+", "enter", "need"),
    (_I + r"(?:'m|\s+(?:get|arrive|come|am))\s+(?:back\s+)?home\b", "enter", "home"),
    (_I + r"\s+get\s+back\b", "enter", "home"),                                        # "when I get back"
    (_I + r"\s+arrive\b", "enter", "home"),
    (_I + r"(?:'m|\s+am)\s+leaving\b", "leave", "maybe"),
    (_I + r"\s+leave(?:\s+from)?\b", "leave", "maybe"),
    (_I + r"\s+(?:head|walk|get)\s+out(?:\s+of)?\b", "leave", "maybe"),
]
# "when I leave here" / "when I get here": only the phone knows where "here" is, and ring-router has no
# access to your location. Refused (the handler says so), never set at home by mistake.
HERE = re.compile(_I + r"(?:'m|\s+am)?\s+(?:(?:leav(?:e|ing)|get|arrive|come|head\s+out\s+of|walk\s+out\s+of"
                  r"|get\s+out\s+of|get\s+back)\s+(?:(?:to|at|from|in)\s+)?)?(here|there)(?![\w'])", re.IGNORECASE)
_LEAVE_WORDS = re.compile(r"leav|out\s+of", re.IGNORECASE)

# Words after "get to" that aren't a place: "when I get to it", "when I get to that".
NOT_A_PLACE = r"(?:it|that|this|them|him|her|you|go|see|talk|work\s+on)\b"
# After a bare "when I leave": words that mean somewhere other than home ("when I leave school").
ELSEWHERE = (r"(?:from\b|at\b|in\b|here\b|there\b|work\b|job\b|class\b|practice\b|school\b|town\b"
             r"|church\b|campus\b"
             r"|to\s+(?:the|my|a|an|work|school|town|church|campus)\b|(?:the|my|a|an)\s+\w)")


def _place_at(text: str, pos: int, known: list[Place]) -> tuple[Place, int] | None:
    """The configured place whose spoken name starts at text[pos:] (longest name wins)."""
    names = sorted(((n, p) for p in known for n in p.spoken), key=lambda np: len(np[0]), reverse=True)
    for n, p in names:
        m = re.compile(r"(?:(?:the|my)\s+)?" + r"\s+".join(map(re.escape, n.split())) + r"(?![\w'])",
                       re.IGNORECASE).match(text, pos)
        if m:
            return p, m.end()
    return None


def _heard(text: str, pos: int) -> tuple[str, int]:
    """Up to three words taken as an unknown place name, stopping before "to"/"and"/punctuation."""
    m = re.compile(r"((?:[\w'&-]+)(?:\s+(?!to\b|and\b|so\b|for\b)[\w'&-]+){0,2})", re.IGNORECASE).match(text, pos)
    return (m.group(1), m.end()) if m else ("", pos)


def _tidy(text: str) -> str:
    t = " ".join(text.split()).strip(" ,.")
    t = re.sub(r"^(?:to|that|about)\s+", "", t, flags=re.IGNORECASE).strip()
    return t[:1].upper() + t[1:]


def parse(body: str, raw: dict) -> Geo | None:
    """'to buy protein when I get to the gym' -> Geo('Buy protein', 'enter', <gym>).

    None when the text isn't a location reminder at all. A Geo with place=None when a place was
    named that isn't configured (the caller refuses, naming it)."""
    known = places(raw)
    home = known[0]
    h = HERE.search(body)
    if h:
        prox = "leave" if _LEAVE_WORDS.search(h.group(0)) else "enter"
        return Geo(_tidy(body[:h.start()] + " " + body[h.end():]), prox, None, h.group(1).lower())
    for pat, prox, slot in TRIGGERS:
        m = re.search(pat, body, re.IGNORECASE)
        if not m:
            continue
        end, place, heard = m.end(), None, ""
        if slot == "home":
            place = home
        elif slot == "back":
            hit = _place_at(body, end, known)
            if hit:
                place, end = hit
            elif re.match(r"(?:the|my|a|an|work|job|class|school|town|church|campus)\b", body[end:], re.IGNORECASE):
                heard, end = _heard(body, end)          # "back to the office", no office configured
            else:
                place, end = home, end - len(re.search(r"to\s+$", m.group(0), re.IGNORECASE).group(0))
        elif slot == "need":
            if re.match(NOT_A_PLACE, body[end:], re.IGNORECASE):
                return None
            hit = _place_at(body, end, known)
            if hit:
                place, end = hit
            else:
                heard, end = _heard(body, end)
                if not heard:
                    return None
        else:   # "maybe": "when I leave", "when I leave the gym", "when I leave school" (unknown)
            rest = body[end:].lstrip(" ,")
            off = len(body[end:]) - len(rest)
            hit = _place_at(body, end + off, known)
            if hit:
                place, end = hit
            elif re.match(ELSEWHERE, rest, re.IGNORECASE):
                heard, end = _heard(body, end + off)
                heard = re.sub(r"^(?:from|at|in)\s+", "", heard, flags=re.IGNORECASE)
            else:
                place = home
        title = _tidy(body[:m.start()] + " " + body[end:])
        return Geo(title, prox, place, heard)
    return None


def describe(g: Geo) -> str:
    """'arrive home' / 'leave the Gym' style phrase for notifications."""
    if g.place and g.place.is_home:
        return "arrive home" if g.proximity == "enter" else "leave home"
    name = g.place.name if g.place else g.heard
    return f"arrive at {name}" if g.proximity == "enter" else f"leave {name}"
