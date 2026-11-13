"""Decide what a transcript means. Pure function of (transcript, config): no side effects.

Order:
  1. cancel phrase at the end        -> cancelled
  2. scene (exact whole phrase)      -> scene
  3. fixed prefix routes, most specific first
       go ahead (Claude Code proposal) / claude code on <host> / research / ask <model> / note / remember /
       shopping list / reminder / calendar / home / messaging
  4. refusals (timers, alarms)       -> refuse
  5. anything else, question or not  -> agent (general agent)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from . import cancel, places, recipients
from .text import SEP, capitalize_first, clean, match_prefix, simple, strip_body

QUESTION_WORDS = ("what", "what's", "whats", "when", "when's", "where", "where's", "who", "who's",
                  "which", "did", "does", "do", "is", "are", "was", "were", "has", "have", "how",
                  "how's", "can", "could", "will", "should", "why")


@dataclass
class Route:
    route: str                      # handler name
    transcript: str                 # cleaned transcript
    body: str = ""                  # text after the prefix
    verb: str = ""                  # ask | tell | hey | bare | ...
    params: dict[str, Any] = field(default_factory=dict)
    reason: str = ""                # human-readable explanation

    def describe(self) -> str:
        extra = " ".join(f"{k}={v!r}" for k, v in self.params.items())
        return f"[{self.route}] {self.reason} {extra} body={self.body!r}".strip()


def route(transcript: str, cfg) -> Route:
    raw = cfg.raw
    t = clean(transcript)
    if not t:
        return Route("discard", t, reason="empty transcript")

    # 1. cancel -------------------------------------------------------------
    c = raw.get("cancel", {})
    phrase = cancel.detect(t, c.get("strong", []), c.get("weak", []))
    if phrase:
        return Route("cancelled", t, params={"phrase": phrase}, reason=f"ends with cancel phrase {phrase!r}")

    # A spoken continuation ("Also tell her...", "And turn off the fan", "Oh, and add milk") means the
    # same as the command alone. Only at the start, and only when something follows.
    t = strip_continuation(t)

    # 2. scenes: exact whole-transcript match ---------------------------------
    whole = simple(t)
    for key, scene in raw.get("scenes", {}).items():
        if whole in {simple(p) for p in scene.get("phrases", [])}:
            return Route("scene", t, params={"scene": key}, reason="exact scene phrase")

    # 3. prefix routes ---------------------------------------------------------
    for fn in (_go_ahead, _claude_code, _research, _ask_model, _note, _remember, _shopping, _reminder,
               _calendar, _home, _message, _home_suffix):
        r = fn(t, raw)
        if r:
            return r

    # 4. refusals ---------------------------------------------------------------
    low = t.lower()
    for key, rule in raw.get("refuse", {}).items():
        if any(re.search(p, low) for p in rule.get("patterns", [])):
            return Route("refuse", t, params={"rule": key, "message": rule.get("message", "Not supported.")},
                         reason=f"refusal rule {key!r}")

    # 5. everything else, questions and requests alike ----------------------------
    return Route("agent", t, body=t, reason="no prefix matched (general agent)")


# ------------------------------------------------------------------- helpers --

CONTINUATION = re.compile(r"(?:oh(?![\w'])" + SEP + r"*)?(?:(?:and|also|plus|then|go\s+ahead\s+and)(?![\w'])"
                          + SEP + r"*)+", re.IGNORECASE)


def strip_continuation(t: str) -> str:
    """'Also, tell her I'm late' -> 'tell her I'm late'. Unchanged if nothing would be left."""
    m = CONTINUATION.match(t)
    return t[m.end():] if m and m.end() < len(t) and t[m.end():].strip() else t


VERBS_ASK = ("ask", "hey", "yo")
VERBS_TELL = ("tell", "message", "text", "send a message to", "send a text to", "let")


def _spoken_index(section: dict) -> list[tuple[str, str]]:
    """(spoken phrase, key) pairs, longest first."""
    pairs = [(s.lower(), k) for k, v in section.items() for s in v.get("spoken", [])]
    return sorted(pairs, key=lambda kv: len(kv[0]), reverse=True)


def _match_spoken(t: str, pos: int, section: dict) -> tuple[str, int] | None:
    for spoken, key in _spoken_index(section):
        m = match_prefix(t, [spoken], pos)
        if m:
            return key, m[1]
    return None


ON_HOST = re.compile(r"(?:on|in|at)" + SEP + r"+(?:the" + SEP + r"+)?", re.IGNORECASE)
# "Go ahead" / "do it" (the whole command): apply the fix Claude Code just proposed. The handler
# refuses unless exactly one proposal is waiting, so on its own it can't do anything.
GO_AHEAD = re.compile(r"(?:(?:yes|yeah|yep|sure)" + SEP + r"+)?(?:go\s+ahead|do\s+it|proceed|go\s+for\s+it|"
                      r"make\s+the\s+(?:fix|change)|apply\s+(?:the|that)\s+fix|fix\s+it)"
                      r"(?:\s+and\s+(?:fix|do|apply)\s+it)?(?:" + SEP + r"+please)?" + SEP + r"*$", re.IGNORECASE)


def _go_ahead(t: str, raw: dict) -> Route | None:
    if raw.get("hosts") and GO_AHEAD.fullmatch(t.strip()):
        return Route("claude_code_proceed", t, reason="go-ahead for a Claude Code proposal")
    return None


def _claude_code(t: str, raw: dict) -> Route | None:
    hosts = raw.get("hosts", {})
    m = match_prefix(t, ["ask claude code", "tell claude code", "hey claude code", "claude code"])
    if not m:
        # "Ask Claude on the home server why ...": naming a host means Claude Code there. Without a
        # known host it stays "ask Claude" (the OpenRouter model).
        names = dict.fromkeys(["claude", *raw.get("models", {}).get("claude", {}).get("spoken", [])])
        v = match_prefix(t, [f"{verb} {n}" for verb in ("ask", "tell", "hey") for n in names])
        on = v and ON_HOST.match(t, v[1])
        hm = on and _match_spoken(t, on.end(), hosts)
        if not hm:
            return None
        host, pos = hm
        return Route("claude_code", t, body=strip_body(t[pos:]), verb=v[0].split()[0],
                     params={"host": host}, reason="claude + named host")
    phrase, pos = m
    verb = phrase.split()[0] if phrase.split()[0] in ("ask", "tell", "hey") else "ask"
    host = next((k for k, h in hosts.items() if h.get("default")), None)
    on = ON_HOST.match(t, pos)
    if on:
        hm = _match_spoken(t, on.start(), hosts) or _match_spoken(t, on.end(), hosts)
        if not hm:
            heard = t[on.end():].split(" ", 1)[0]
            return Route("refuse", t, params={"rule": "unknown_host",
                                              "message": f"Unknown Claude Code host '{heard}'."},
                         reason="claude code: host not in config")
        host, pos = hm
    # "ask" and "tell" mean the same thing here: the first run only investigates; changes need a go-ahead
    return Route("claude_code", t, body=strip_body(t[pos:]), verb=verb,
                 params={"host": host}, reason="claude code prefix")


def _research(t: str, raw: dict) -> Route | None:
    m = match_prefix(t, ["do some research on", "do research on", "research"])
    if not m:
        return None
    pos = m[1]
    model = raw.get("research", {}).get("default_model")
    w = match_prefix(t, ["with", "using", "on"], pos)
    if w:
        mm = _match_spoken(t, w[1], raw.get("models", {}))
        if mm:
            model, pos = mm
    body = strip_body(t[pos:])
    if not body:
        return None
    return Route("research", t, body=body, params={"model": model}, reason="research prefix")


def _ask_model(t: str, raw: dict) -> Route | None:
    v = match_prefix(t, ["ask", "tell", "hey", "query"])
    if not v:
        return None
    mm = _match_spoken(t, v[1], raw.get("models", {}))
    if not mm:
        return None
    key, pos = mm
    body = strip_body(t[pos:])
    if not body:
        return None
    return Route("llm", t, body=body, verb=v[0], params={"model": key}, reason="ask-a-model prefix")


def _note(t: str, raw: dict) -> Route | None:
    m = match_prefix(t, ["make a note", "take a note", "new note", "note to self", "write a note", "note"])
    if not m:
        return None
    body = strip_body(re.sub(r"^(that|saying|of)\b", "", strip_body(t[m[1]:]), flags=re.IGNORECASE))
    if not body:
        return None
    return Route("note", t, body=body, params={"folder": raw.get("notes", {}).get("default_folder", "Notes")},
                 reason="note prefix")


def _remember(t: str, raw: dict) -> Route | None:
    m = match_prefix(t, ["remember that", "remember"])
    if not m:
        return None
    body = strip_body(t[m[1]:])
    if not body:
        return None
    if t.rstrip().endswith("?") and simple(body).split(" ", 1)[0] in ("when", "how", "what", "where", "who", "why"):
        return None     # "Remember when we went to Paris?" is a question, not something to file
    return Route("remember", t, body=capitalize_first(body),
                 params={"folder": raw.get("notes", {}).get("remember_folder", "Remember")},
                 reason="remember prefix")


def _list_suffix(t: str, lists: dict) -> tuple[str, int, int] | None:
    """Find '... to/on (my|the) <list>' at the end. Returns (key, start_of_suffix, end)."""
    for spoken, key in _spoken_index(lists):
        m = re.search(SEP + r"+(?:to|on|onto|in)" + SEP + r"+(?:my|the|our)?" + SEP + r"*"
                      + re.escape(spoken).replace(r"\ ", SEP + "+") + SEP + r"*$", t, re.IGNORECASE)
        if m:
            return key, m.start(), m.end()
    return None


def _shopping(t: str, raw: dict) -> Route | None:
    lists = raw.get("lists", {})
    v = match_prefix(t, ["add", "put", "we need", "i need"])
    if v:
        suf = _list_suffix(t, lists)
        if suf and suf[1] > v[1]:
            items = strip_body(t[v[1]:suf[1]])
            return Route("list_add", t, body=items, params={"list": suf[0]}, reason="add-to-list")
    # "<list>: x, y"
    for spoken, key in _spoken_index(lists):
        m = re.match(re.escape(spoken).replace(r"\ ", r"\s+") + r"\s*:\s*(.+)$", t, re.IGNORECASE)
        if m:
            return Route("list_add", t, body=strip_body(m.group(1)), params={"list": key}, reason="list: items")
    return None


def _reminder(t: str, raw: dict) -> Route | None:
    m = match_prefix(t, ["set a reminder", "create a reminder", "add a reminder", "remind me"])
    if not m:
        return None
    body = strip_body(re.sub(r"^(to|that|for)\b", "", strip_body(t[m[1]:]), flags=re.IGNORECASE)) \
        if m[0] != "remind me" else strip_body(t[m[1]:])
    if not body:
        return None
    location = places.parse(body, raw) is not None      # "when I get home / to the gym / leave work"
    return Route("reminder", t, body=body, params={"location_trigger": location}, reason="reminder prefix")


def _calendar(t: str, raw: dict) -> Route | None:
    cals = raw.get("calendars", {})
    aliases = {k.lower(): v for k, v in cals.get("aliases", {}).items()}
    names = sorted(list(aliases) + ["my", "the"], key=len, reverse=True)
    alt = "|".join(re.escape(n).replace(r"\ ", SEP + "+") for n in names)
    m = re.search(SEP + r"+(?:to|on|in)" + SEP + r"+(?:(?:the|my)" + SEP + r"+)?(" + alt + r")"
                  + SEP + r"+calendar" + SEP + r"*$", t, re.IGNORECASE)
    v = match_prefix(t, ["add", "put", "schedule", "create an event", "make an event", "book"])
    if v and m and m.start() > v[1]:
        which = re.sub(SEP + "+", " ", m.group(1).lower())
        calendar = aliases.get(which, cals.get("default"))
        return Route("calendar", t, body=strip_body(t[v[1]:m.start()]), params={"calendar": calendar},
                     reason="calendar suffix")
    s = match_prefix(t, ["schedule", "create an event", "make an event"])
    if s and re.match(r"(?:a|an)\s+(?:text|message|email|imessage)\b", t[s[1]:], re.IGNORECASE):
        return Route("refuse", t, params={"rule": "scheduled_send",
                                          "message": "Scheduled messages aren't supported. Nothing sent or added."},
                     reason="schedule a message")
    if s:
        return Route("calendar", t, body=strip_body(t[s[1]:]), params={"calendar": cals.get("default")},
                     reason="calendar prefix")
    return None


HOME_VERBS = ["turn on", "turn off", "switch on", "switch off", "shut off", "turn up", "turn down", "dim", "brighten",
              "unlock", "lock", "open", "close", "set", "turn", "switch", "shut", "kill"]
DOOR_WORDS = r"\b(garage|door|gate|blinds?|shades?|curtains?)\b"
DEVICE_WORDS = r"(?:lights?|lamps?|fans?|sconces?|pendants?|switch|outlet|plug)"


def _room_first(rest: str) -> str:
    """"the lights in the kitchen" -> "the kitchen lights" (the matcher wants the room first)."""
    m = re.match(rf"(?:the\s+|my\s+)?({DEVICE_WORDS})\s+in\s+(?:the\s+|my\s+)?(.+?)(\s+to\s+.+)?$", rest, re.IGNORECASE)
    return f"the {m.group(2)} {m.group(1)}{m.group(3) or ''}" if m else rest


def _home(t: str, raw: dict) -> Route | None:
    m = match_prefix(t, HOME_VERBS)
    if not m:
        return None
    verb, pos = m
    rest = strip_body(t[pos:])
    if verb in ("set", "turn", "switch") and re.match(r"(?:the\s+|my\s+|a\s+|an\s+)?(alarm|timer)\b", rest, re.IGNORECASE):
        return Route("refuse", t, params={"rule": "timers", "message": raw.get("refuse", {}).get("timers", {}).get(
            "message", "Timers and alarms aren't supported from the ring.")}, reason="alarm or timer")
    if verb == "set" and re.match(r"up\b", rest, re.IGNORECASE):
        return None  # "set up a meeting with Terri to go over the budget"
    if verb == "kill" and not re.search(rf"\b{DEVICE_WORDS}\b", rest, re.IGNORECASE):
        return None  # "kill the build" is not about a lamp
    if verb == "set" and not re.search(r"\bto\b", rest, re.IGNORECASE):
        return None  # "set a reminder"/"set a timer" etc. are not home commands
    if verb == "set" and re.match(r"(a|an)\b", rest, re.IGNORECASE):
        return None
    action = {"switch on": "on", "turn on": "on", "switch off": "off", "turn off": "off", "shut off": "off",
              "kill": "off"}.get(verb, verb)
    # "turn the lights off in the kitchen" -> "turn the kitchen lights off"
    mid = re.match(r"(.+?)\s+(on|off)\s+in\s+(?:the\s+|my\s+)?(.+)$", rest, re.IGNORECASE)
    if mid and verb in ("turn", "switch", "shut"):
        thing = re.sub(r"^(?:the|my)\s+", "", mid.group(1), flags=re.IGNORECASE)
        rest = f"the {mid.group(3)} {thing} {mid.group(2)}"
    rest = _room_first(rest)
    # "turn the living room light on" form
    tail = re.search(SEP + r"+(on|off)" + SEP + r"*$", rest, re.IGNORECASE)
    if verb == "shut":                  # "shut the lights off" switches; "shut the garage" closes
        if tail and tail.group(1).lower() == "off":
            action, rest = "off", rest[:tail.start()].strip()
        else:
            verb = action = "close"
    if verb in ("open", "close") and not re.search(DOOR_WORDS, rest, re.I):
        return None
    if verb in ("turn", "switch"):
        if tail:
            action = tail.group(1).lower()
            rest = rest[:tail.start()].strip()
        elif re.search(r"\bto\b", rest, re.IGNORECASE):
            action = "set"
        else:
            return None
    confirm = action in raw.get("home", {}).get("confirm_actions", [])
    return Route("home", t, body=rest, verb=verb, params={"action": action, "confirm": confirm},
                 reason="home control verb")


def _home_suffix(t: str, raw: dict) -> Route | None:
    """"Kitchen lights off" / "Lights on": no verb, a device word, then on or off. Nothing else."""
    m = re.match(rf"((?:[\w']+\s+){{0,4}}{DEVICE_WORDS})\s+(on|off)" + SEP + r"*$", t, re.IGNORECASE)
    if not m:
        return None
    action = m.group(2).lower()
    return Route("home", t, body=m.group(1), verb="suffix",
                 params={"action": action, "confirm": action in raw.get("home", {}).get("confirm_actions", [])},
                 reason="device then on/off")


PRONOUN_AT = re.compile(r"(her|him|them)(?![\w'])" + SEP + "*", re.IGNORECASE)


def _who(t: str, pos: int, recips: dict):
    """The recipient at t[pos:]: a name, or "her"/"him"/"them" (whoever was messaged last: the
    handler decides, since the router keeps no state)."""
    m = PRONOUN_AT.match(t, pos)
    if m:
        return recipients.RecipientMatch(None, "pronoun", m.group(1).lower(), m.end())
    return recipients.resolve(t, pos, recips)


def _message(t: str, raw: dict) -> Route | None:
    recips = raw.get("recipients", {})
    sm = match_prefix(t, ["send"])
    if sm:                                    # "send Terri a text (that / saying) I'm late"
        rm = _who(t, sm[1], recips)
        k = rm and match_prefix(t, ["a text message", "a message", "a text"], rm.end)
        if k:
            return _message_route(t, rm, "tell", t[k[1]:], raw=raw)
    v = match_prefix(t, list(VERBS_ASK + VERBS_TELL))
    if v:
        verb, pos = v
        if verb == "let":  # "let terri know that ..."
            rm = _who(t, pos, recips)
            if not rm:
                return None
            k = match_prefix(t, ["know that", "know"], rm.end)
            if not k:
                return None
            return _message_route(t, rm, "tell", t[k[1]:], raw=raw)
        rm = _who(t, pos, recips) if verb not in ("hey", "yo") else recipients.resolve(t, pos, recips)
        if rm is None:
            return None
        if verb in VERBS_ASK and verb != "ask" and rm.tier == "unknown":
            return None   # "hey so I was thinking..." is just talk, not a message to someone named "so"
        if verb == "ask" and rm.tier == "unknown":
            nxt = simple(t[rm.end:]).split(" ", 1)[0]
            if nxt in QUESTION_WORDS or nxt == "about":
                if not _contact_name(rm, raw):
                    return None   # "ask Google what time it is": a question for the agent, not a message
                # "ask Nate what time dinner is": a message if Nate is in Contacts, else the agent's
                return _message_route(t, rm, "ask", t[rm.end:], raw=raw, fallback="agent")
        # "ask X ..." is indirect speech (rewrite); "hey X, ..." is direct address (send as spoken)
        kind = "ask" if verb == "ask" else "hey" if verb in VERBS_ASK else "tell"
        return _message_route(t, rm, kind, t[rm.end:], raw=raw)
    rm = recipients.resolve(t, 0, recips, bare_only=True)
    if rm:
        return _message_route(t, rm, "hey", t[rm.end:], bare=True)
    return None


def _is_question(s: str) -> bool:
    w = simple(s).split(" ", 1)[0] if s.strip() else ""
    return w in QUESTION_WORDS or s.rstrip().endswith("?")


def clean_body(rest: str, kind: str) -> str:
    """The message text from what follows the name: "that ..." / "saying ..." / "to ..." trimmed."""
    body = strip_body(rest)
    if kind == "ask":
        return body
    if kind == "hey":
        return capitalize_first(body)
    low = body.lower()
    if low.startswith("that "):
        body = body[5:]
    elif low.startswith("saying "):
        body = body[7:]
    elif low.startswith("to "):
        body = body[3:]                        # "tell terri to grab milk" -> "Grab milk"
    return capitalize_first(body)


def _contact_name(rm, raw: dict) -> bool:
    """An unrecognized single name that Contacts and recent Messages may know ([messages] contacts)."""
    return (rm.tier == "unknown" and bool(raw.get("messages", {}).get("contacts"))
            and " " not in rm.heard and not rm.heard.endswith(("'s", "s'")))


def _message_route(t: str, rm, kind: str, rest: str, bare: bool = False, raw: dict | None = None,
                   fallback: str = "") -> Route:
    body = clean_body(rest, kind)
    rewrite = "ask_question" if kind == "ask" else "none"
    if _contact_name(rm, raw or {}):           # the handler looks the name up; the router keeps no data
        params = {"heard": rm.heard, "kind": kind, "rest": rest, "rewrite": rewrite}
        if fallback:
            params["fallback"] = fallback
        return Route("imessage_contact", t, body=body, verb=kind, params=params,
                     reason="message (name not configured: Contacts and recent Messages decide)")
    tier_route = {"strong": "imessage", "weak": "imessage", "unknown": "refuse", "pronoun": "imessage_followup"}[rm.tier]
    params = {"recipient": rm.key, "tier": rm.tier, "heard": rm.heard, "rewrite": rewrite}
    if rm.tier == "pronoun":
        params = {"pronoun": rm.heard, "rewrite": rewrite}
    if rm.tier == "unknown":
        params.update(rule="unknown_recipient", message=f"Didn't recognize '{rm.heard}', nothing sent.")
    return Route(tier_route, t, body=body, verb="bare" if bare else kind, params=params,
                 reason=f"message ({rm.tier} recipient match)")
