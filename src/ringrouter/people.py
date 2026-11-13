"""People you can message without configuring them: Contacts cards, ranked by recent 1:1 Messages.

Both databases are read directly, read-only (Ring Router.app has Full Disk Access). Only metadata is
read from Messages: which handle, who sent, when, and the service. Never message text.

Decision for a spoken name (see match()):
  send  - "Nate": exactly one card whose first name/nickname is Nate (or a nickname of it) that you
          messaged in the last `recent_days`, and nobody else messaged that recently sounds like it
          (_sounds_like: code, no name list; errs toward "alike" since that only adds a tap).
          Or a spoken first + last name that matches exactly one card, however long ago.
  ask   - several matches, only a sound-alike, or only someone you haven't messaged lately: a Send
          tap per person (up to `max_choices`).
  none  - no card matches: refuse (or, for "ask X what ...", hand to the agent).
"""

from __future__ import annotations

import glob
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field

from .text import similarity, simple, sound_key, soundex

MESSAGES_DB = "~/Library/Messages/chat.db"
ADDRESSBOOK_GLOB = "~/Library/Application Support/AddressBook/Sources/*/AddressBook-v22.abcddb"
APPLE_EPOCH = 978307200          # 2001-01-01 in Unix time
DAY = 86400

# Spoken name -> the formal names it stands for (and back). Only common, unambiguous pairs.
NICKNAMES = {
    "nate": ["nathan", "nathaniel"], "mike": ["michael"], "dan": ["daniel"], "danny": ["daniel"],
    "dave": ["david"], "chris": ["christopher", "christian", "christine", "christina"],
    "matt": ["matthew"], "nick": ["nicholas"], "rob": ["robert"], "bob": ["robert"], "bobby": ["robert"],
    "bill": ["william"], "will": ["william"], "jim": ["james"], "jimmy": ["james"], "joe": ["joseph"],
    "tom": ["thomas"], "tony": ["anthony"], "steve": ["steven", "stephen"], "ben": ["benjamin"],
    "sam": ["samuel", "samantha"], "alex": ["alexander", "alexandra"], "andy": ["andrew"],
    "drew": ["andrew"], "jon": ["jonathan"], "josh": ["joshua"], "jake": ["jacob"], "zach": ["zachary"],
    "greg": ["gregory"], "jeff": ["jeffrey"], "ken": ["kenneth"], "ron": ["ronald"], "don": ["donald"],
    "rick": ["richard"], "rich": ["richard"], "dick": ["richard"], "ed": ["edward"], "eddie": ["edward"],
    "pat": ["patrick", "patricia"], "tim": ["timothy"], "liz": ["elizabeth"], "beth": ["elizabeth"],
    "kate": ["katherine", "kathryn", "catherine"], "katie": ["katherine", "kathryn", "catherine"],
    "jen": ["jennifer"], "jenny": ["jennifer"], "becky": ["rebecca"], "meg": ["megan", "margaret"],
    "abby": ["abigail"], "sue": ["susan"], "deb": ["deborah"], "vicky": ["victoria"], "ally": ["allison"],
}
_FORMAL = {}
for _short, _longs in NICKNAMES.items():
    for _l in _longs:
        _FORMAL.setdefault(_l, set()).add(_short)


def name_forms(word: str) -> set[str]:
    """'nate' -> {'nate', 'nathan', 'nathaniel'}; 'nathan' -> {'nathan', 'nate'}."""
    w = simple(word)
    return {w, *NICKNAMES.get(w, []), *_FORMAL.get(w, set())}


@dataclass
class Person:
    first: str
    last: str = ""
    nickname: str = ""
    phones: list[str] = field(default_factory=list)       # as on the card
    emails: list[str] = field(default_factory=list)
    # handle -> (last message from you, last message either way, service), Unix seconds
    chats: dict[str, tuple[float, float, str]] = field(default_factory=dict)

    @property
    def display(self) -> str:
        return " ".join(p for p in (self.first, self.last) if p)

    @property
    def last_from_me(self) -> float:
        return max((c[0] for c in self.chats.values()), default=0.0)

    def handle(self) -> str | None:
        """Where to send: the handle of the conversation you last wrote in, else the card's first
        mobile-looking phone, else its first email."""
        if self.chats:
            return max(self.chats.items(), key=lambda kv: (kv[1][0], kv[1][1]))[0]
        return (self.phones or self.emails or [None])[0]

    def first_names(self) -> set[str]:
        return {simple(n) for n in (self.first, self.nickname) if n}


@dataclass
class Choice:
    person: Person
    why: str              # "messaged 2 days ago", "last messaged Mar 2025", "sounds like 'Kate'"


@dataclass
class Decision:
    tier: str                         # send | ask | none
    choices: list[Choice]
    words: int                        # spoken words that made up the name (1, or 2 for first + last)
    heard: str


# ------------------------------------------------------------------- loading --

def _digits(s: str) -> str:
    d = re.sub(r"\D", "", s)
    return d[-10:] if len(d) >= 10 else d


def _norm(handle: str) -> str:
    return handle.strip().lower() if "@" in handle else _digits(handle)


def _ro(path: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{os.path.expanduser(path)}?mode=ro", uri=True, timeout=5)


def load_contacts(pattern: str = ADDRESSBOOK_GLOB) -> list[Person]:
    people: list[Person] = []
    for path in glob.glob(os.path.expanduser(pattern)):
        con = _ro(path)
        try:
            recs = {pk: Person(first or "", last or "", nick or "") for pk, first, last, nick in con.execute(
                "SELECT Z_PK, ZFIRSTNAME, ZLASTNAME, ZNICKNAME FROM ZABCDRECORD "
                "WHERE ZFIRSTNAME IS NOT NULL OR ZNICKNAME IS NOT NULL")}
            for owner, num, label in con.execute("SELECT ZOWNER, ZFULLNUMBER, ZLABEL FROM ZABCDPHONENUMBER"):
                if owner in recs and num:
                    mobile = bool(label and re.search(r"mobile|iphone|cell", label, re.IGNORECASE))
                    (recs[owner].phones.insert(0, num) if mobile else recs[owner].phones.append(num))
            for owner, addr in con.execute("SELECT ZOWNER, ZADDRESS FROM ZABCDEMAILADDRESS"):
                if owner in recs and addr:
                    recs[owner].emails.append(addr)
        finally:
            con.close()
        people += recs.values()
    return people


def load_chats(since_days: int = 365, path: str = MESSAGES_DB) -> dict[str, tuple[float, float, str, str]]:
    """normalized handle -> (last from you, last any, service, raw handle), 1:1 chats only (style 45)."""
    cutoff_ns = (time.time() - APPLE_EPOCH - since_days * DAY) * 1e9
    con = _ro(path)
    try:
        rows = con.execute("""
            SELECT h.id, c.service_name,
                   MAX(CASE WHEN m.is_from_me = 1 THEN m.date END), MAX(m.date)
            FROM chat c
            JOIN chat_handle_join chj ON chj.chat_id = c.ROWID
            JOIN handle h ON h.ROWID = chj.handle_id
            JOIN chat_message_join cmj ON cmj.chat_id = c.ROWID
            JOIN message m ON m.ROWID = cmj.message_id
            WHERE c.style = 45 AND m.date > ?
            GROUP BY h.id, c.service_name""", (cutoff_ns,)).fetchall()
    finally:
        con.close()

    def unix(d):
        if not d:
            return 0.0
        return (d / 1e9 if d > 1e11 else d) + APPLE_EPOCH     # nanoseconds since 2001 (seconds on old rows)

    out: dict[str, tuple[float, float, str, str]] = {}
    for hid, service, mine, last in rows:
        key = _norm(hid)
        cur = out.get(key)
        rec = (unix(mine), unix(last), service or "", hid)
        if not cur or rec[:2] > cur[:2]:
            out[key] = rec
    return out


def load_people(**kw) -> list[Person]:
    """Contacts cards with their recent 1:1 conversations attached."""
    people = load_contacts(kw.get("contacts_glob", ADDRESSBOOK_GLOB))
    chats = load_chats(kw.get("since_days", 365), kw.get("messages_db", MESSAGES_DB))
    for p in people:
        for h in p.phones + p.emails:
            c = chats.get(_norm(h))
            if c:
                p.chats[c[3]] = c[:3]
    return people


# ------------------------------------------------------------------ matching --

def _ago(ts: float, now: float) -> str:
    days = int((now - ts) // DAY)
    if days <= 0:
        return "messaged today"
    if days == 1:
        return "messaged yesterday"
    if days < 60:
        return f"messaged {days} days ago"
    return "last messaged " + time.strftime("%b %Y", time.localtime(ts))


def _sounds_like(heard: str, name: str) -> bool:
    """Could a transcriber have confused them? Either the allowlist near-miss rule (Nate/Kate, Dan/Dean)
    or the same sound key (Erin/Aaron, Sean/Shawn, Lori/Laurie). This only ever adds a Send tap, so
    it errs toward "alike": Tom/Tim and Greg/Craig count too."""
    if heard == name:
        return False
    hs, ns = soundex(heard), soundex(name)
    near = bool(hs) and (hs == ns or hs[1:] == ns[1:]) and (heard[-2:] == name[-2:] or similarity(heard, name) >= 0.8)
    return near or (bool(sound_key(heard)) and sound_key(heard) == sound_key(name))


def match(words: list[str], people: list[Person], *, now: float | None = None, recent_days: int = 7,
          max_choices: int = 3) -> Decision:
    """Decide who words[0] (maybe with words[1] as a last name) means."""
    now = now or time.time()
    words = [simple(w) for w in words if simple(w)]
    if not words:
        return Decision("none", [], 0, "")
    first = words[0]
    forms = name_forms(first)
    recent_cut = now - recent_days * DAY

    def recent(p: Person) -> bool:
        return p.last_from_me >= recent_cut

    def why(p: Person) -> str:
        return _ago(p.last_from_me, now) if p.last_from_me else "no messages in the past year"

    # First + last name: one card with exactly that name sends, however long ago.
    if len(words) > 1:
        full = [p for p in people if p.first_names() & forms and simple(p.last) == words[1] and p.handle()]
        if len(full) == 1:
            return Decision("send", [Choice(full[0], why(full[0]))], 2, f"{words[0]} {words[1]}")
        if full:
            full.sort(key=lambda p: -p.last_from_me)
            return Decision("ask", [Choice(p, why(p)) for p in full[:max_choices]], 2, f"{words[0]} {words[1]}")

    exact = [p for p in people if p.first_names() & forms]
    recent_exact = sorted((p for p in exact if recent(p)), key=lambda p: -p.last_from_me)
    alike = sorted((p for p in people if recent(p) and p not in exact
                    and any(_sounds_like(first, n) for n in p.first_names())), key=lambda p: -p.last_from_me)

    def consumed(p: Person) -> int:       # "tell Nate Bowman ..." when only "Nate" decided it
        return 2 if len(words) > 1 and p.last and simple(p.last) == words[1] else 1

    if len(recent_exact) == 1 and not alike:
        p = recent_exact[0]
        return Decision("send", [Choice(p, why(p))], consumed(p), first)

    older = sorted((p for p in exact if not recent(p)), key=lambda p: -p.last_from_me)
    choices = ([Choice(p, why(p)) for p in recent_exact]
               + [Choice(p, f"sounds like “{first}”, {why(p)}") for p in alike]
               + [Choice(p, why(p)) for p in older])
    choices = [c for c in choices if c.person.handle()][:max_choices]
    if not choices:
        return Decision("none", [], 1, first)
    n = consumed(choices[0].person) if len(choices) == 1 else 1
    return Decision("ask", choices, n, first)
