"""People found in Contacts and ranked by recent Messages ([messages] contacts). All names invented."""

import sqlite3
import time

import pytest

from ringrouter import people
from ringrouter.handlers import Context, run, run_confirmed
from ringrouter.notify import ConsoleNotifier
from ringrouter.people import Person, match
from ringrouter.router import route
from ringrouter.store import Store

NOW = time.time()
DAY = 86400


def person(first, last="", days_ago=None, handle=None, incoming_only=False, nickname=""):
    """A card; days_ago = when you last wrote to them (None: no conversation)."""
    h = handle or f"+1555{abs(hash(first + last)) % 10**7:07d}"
    p = Person(first, last, nickname, phones=[h])
    if days_ago is not None:
        mine = 0.0 if incoming_only else NOW - days_ago * DAY
        p.chats[h] = (mine, NOW - days_ago * DAY, "iMessage")
    return p


NATE = person("Nate", "Bowman", days_ago=1, handle="+15550100001")
FRANK = person("Frank", "Schwartz", handle="+15550100002")


def names(d):
    return [c.person.display for c in d.choices]


# ---------------------------------------------------------------- matching --

def test_only_recent_nate_sends():
    d = match(["nate", "i'm"], [NATE, FRANK, person("Nate", "Old", days_ago=200)], now=NOW)
    assert (d.tier, names(d), d.words) == ("send", ["Nate Bowman"], 1)


def test_nickname_and_formal_name_match_each_other():
    assert match(["nathan"], [NATE], now=NOW).tier == "send"
    assert match(["nate"], [person("Nathan", "Price", days_ago=2)], now=NOW).tier == "send"


def test_not_messaged_lately_asks():
    d = match(["frank", "i"], [NATE, FRANK], now=NOW)
    assert (d.tier, names(d)) == ("ask", ["Frank Schwartz"])
    assert d.choices[0].why == "no messages in the past year"


def test_eight_days_ago_is_not_recent():
    assert match(["nate"], [person("Nate", "X", days_ago=8)], now=NOW).tier == "ask"


def test_only_their_messages_dont_count_as_talking():
    assert match(["nate"], [person("Nate", "X", days_ago=1, incoming_only=True)], now=NOW).tier == "ask"


def test_two_recent_nates_ask_with_both_newest_first():
    other = person("Nate", "Cole", days_ago=3)
    d = match(["nate"], [other, NATE], now=NOW)
    assert (d.tier, names(d)) == ("ask", ["Nate Bowman", "Nate Cole"])


def test_recent_sound_alike_blocks_a_send():
    kate = person("Kate", "Moss", days_ago=2)
    d = match(["nate"], [NATE, kate], now=NOW)
    assert (d.tier, names(d)) == ("ask", ["Nate Bowman", "Kate Moss"])
    assert "sounds like" in d.choices[1].why


def test_old_sound_alike_does_not_block():
    assert match(["nate"], [NATE, person("Kate", "Moss", days_ago=90)], now=NOW).tier == "send"


def test_only_a_sound_alike_asks():
    d = match(["nate"], [person("Kate", "Moss", days_ago=2)], now=NOW)
    assert (d.tier, names(d)) == ("ask", ["Kate Moss"])


def test_full_name_sends_however_long_ago():
    d = match(["frank", "schwartz", "i"], [NATE, FRANK, person("Frank", "Other")], now=NOW)
    assert (d.tier, names(d), d.words) == ("send", ["Frank Schwartz"], 2)


def test_full_name_on_two_cards_asks():
    d = match(["frank", "schwartz"], [FRANK, person("Frank", "Schwartz", handle="+15550100999")], now=NOW)
    assert d.tier == "ask" and len(d.choices) == 2


def test_last_name_after_a_first_name_match_is_consumed():
    assert match(["nate", "bowman", "i'm"], [NATE], now=NOW).words == 2
    assert match(["nate", "thanks"], [NATE], now=NOW).words == 1


def test_nobody_matches():
    assert match(["zebulon"], [NATE, FRANK], now=NOW).tier == "none"


def test_card_without_phone_or_email_is_never_offered():
    assert match(["frank"], [Person("Frank", "Nobody")], now=NOW).tier == "none"


def test_choices_are_capped():
    many = [person("Nate", f"N{i}", days_ago=i + 1) for i in range(5)]
    assert len(match(["nate"], many, now=NOW, max_choices=3).choices) == 3


# ----------------------------------------------------------------- handler --

def make_ctx(cfg, fake_fm, everyone, agent=None):
    cfg.raw["server"]["dry_run"] = False
    cfg.raw["server"].pop("live_recipients", None)
    calls = []

    def mcp_call(name, args):
        calls.append((name, args))
        return "sent"

    ctx = Context(cfg=cfg, notifier=ConsoleNotifier(echo=False), store=Store(":memory:"), fm=fake_fm,
                  delivery_id="d1", mcp_call=mcp_call, people_loader=lambda: everyone,
                  agent_runner=agent or (lambda *a, **k: (_ for _ in ()).throw(AssertionError("no agent"))))
    notes = []
    ctx.notifier.send = notes.append
    return ctx, calls, notes


def sent(calls):
    return [a for n, a in calls if n == "messages_chat"]


def say(text, ctx):
    return run(route(text, ctx.cfg), ctx)


def test_tell_nate_sends_to_his_recent_handle(cfg, fake_fm):
    ctx, calls, _ = make_ctx(cfg, fake_fm, [NATE, FRANK])
    res = say("Tell Nate I'm excited for the new jack daniels bottle release today", ctx)
    assert res.state == "done"
    assert sent(calls) == [{"action": "create", "to": "+15550100001",
                            "text": "I'm excited for the new jack daniels bottle release today"}]


def test_spoken_last_name_is_not_sent(cfg, fake_fm):
    ctx, calls, _ = make_ctx(cfg, fake_fm, [NATE])
    say("Tell Nate Bowman that I'm running late", ctx)
    assert sent(calls)[0]["text"] == "I'm running late"


def test_frank_asks_first_then_sends_on_tap(cfg, fake_fm):
    ctx, calls, notes = make_ctx(cfg, fake_fm, [NATE, FRANK])
    res = say("Tell Frank I say hi", ctx)
    assert res.state == "needs_confirm" and not sent(calls)
    assert "Frank Schwartz" in notes[-1].text and "past year" in notes[-1].text
    pid = notes[-1].buttons[0].url.split("/")[-2]
    kind, payload = ctx.store.take_pending(pid)
    assert run_confirmed(kind, payload, ctx).state == "done"
    assert sent(calls) == [{"action": "create", "to": "+15550100002", "text": "I say hi"}]


def test_frank_schwartz_by_full_name_sends(cfg, fake_fm):
    ctx, calls, _ = make_ctx(cfg, fake_fm, [NATE, FRANK])
    assert say("Tell Frank Schwartz I say hi", ctx).state == "done"
    assert sent(calls) == [{"action": "create", "to": "+15550100002", "text": "I say hi"}]


def test_two_nates_offer_a_button_each(cfg, fake_fm):
    cole = person("Nate", "Cole", days_ago=3, handle="+15550100003")
    ctx, calls, notes = make_ctx(cfg, fake_fm, [NATE, cole])
    assert say("Tell Nate the bottle came in", ctx).state == "needs_confirm"
    labels = [b.label for b in notes[-1].buttons]
    assert labels == ["Nate Bowman", "Nate Cole", "Cancel"]
    pid = notes[-1].buttons[1].url.split("/")[-2]
    kind, payload = ctx.store.take_pending(pid)
    assert run_confirmed(kind, payload, ctx).state == "refused"          # a plain "yes" picks nobody
    assert run_confirmed(kind, payload, ctx, choice=2).state == "done"
    assert sent(calls) == [{"action": "create", "to": "+15550100003", "text": "The bottle came in"}]


def test_unknown_name_is_refused(cfg, fake_fm):
    ctx, calls, notes = make_ctx(cfg, fake_fm, [NATE])
    assert say("Tell Zebulon hello", ctx).state == "refused" and not sent(calls)
    assert "Zebulon" in notes[-1].text or "zebulon" in notes[-1].text


def test_ask_google_a_question_goes_to_the_agent(cfg, fake_fm):
    seen = []

    class Spec:
        calls, citations, rerouted, home = [], [], [], []

    def agent(kind, request, cfg_, history, rerouter):
        seen.append(request)
        return "It's 3 pm.", Spec()

    ctx, calls, _ = make_ctx(cfg, fake_fm, [NATE], agent=agent)
    assert say("Ask Google what time it is", ctx).state == "done"
    assert seen == ["Ask Google what time it is"] and not sent(calls)


def test_ask_nate_is_rewritten_and_sent(cfg, fake_fm):
    ctx, calls, _ = make_ctx(cfg, fake_fm, [NATE])
    assert say("Ask Nate what time we're meeting", ctx).state == "done"
    assert fake_fm.calls[-1][:2] == ("rewrite", "Nate Bowman")


def test_tell_him_after_a_contact_asks_with_a_tap(cfg, fake_fm):
    ctx, calls, notes = make_ctx(cfg, fake_fm, [NATE])
    say("Tell Nate I'm on my way", ctx)
    assert say("Tell him I'm parking", ctx).state == "needs_confirm"     # no pronoun known for a card
    assert "Nate Bowman" in notes[-1].text
    assert len(sent(calls)) == 1


def test_contacts_off_keeps_the_old_refusal(cfg, fake_fm):
    cfg.raw["messages"]["contacts"] = False
    assert route("Tell Nate hello", cfg).route == "refuse"


def test_live_recipients_keeps_contacts_in_dry_run_unless_listed(cfg, fake_fm):
    ctx, calls, _ = make_ctx(cfg, fake_fm, [NATE])
    cfg.raw["server"]["live_recipients"] = ["terri"]
    assert say("Tell Nate hello", ctx).state == "dry_run" and not sent(calls)
    cfg.raw["server"]["live_recipients"] = ["terri", "contacts"]
    assert say("Tell Nate hello", ctx).state == "done"


def test_unreadable_databases_send_nothing(cfg, fake_fm):
    ctx, calls, _ = make_ctx(cfg, fake_fm, [])
    ctx.people_loader = lambda: (_ for _ in ()).throw(PermissionError("authorization denied"))
    assert say("Tell Nate hello", ctx).state == "failed" and not sent(calls)


# ------------------------------------------------------------------ loading --

@pytest.fixture
def dbs(tmp_path):
    ab = tmp_path / "Sources" / "X" / "AddressBook-v22.abcddb"
    ab.parent.mkdir(parents=True)
    con = sqlite3.connect(ab)
    con.executescript("""
        CREATE TABLE ZABCDRECORD (Z_PK INTEGER PRIMARY KEY, ZFIRSTNAME, ZLASTNAME, ZNICKNAME);
        CREATE TABLE ZABCDPHONENUMBER (ZOWNER, ZFULLNUMBER, ZLABEL);
        CREATE TABLE ZABCDEMAILADDRESS (ZOWNER, ZADDRESS);
        INSERT INTO ZABCDRECORD VALUES (1, 'Nate', 'Bowman', NULL), (2, 'Frank', 'Schwartz', NULL);
        INSERT INTO ZABCDPHONENUMBER VALUES (1, '(555) 010-0001', '_$!<Mobile>!$_'), (2, '555.010.0002', NULL);
        INSERT INTO ZABCDEMAILADDRESS VALUES (1, 'nate@example.com');
    """)
    con.commit()
    con.close()
    chat = tmp_path / "chat.db"
    con = sqlite3.connect(chat)
    ns = lambda days: int((NOW - people.APPLE_EPOCH - days * DAY) * 1e9)  # noqa: E731
    con.executescript("""
        CREATE TABLE chat (ROWID INTEGER PRIMARY KEY, style, service_name);
        CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id);
        CREATE TABLE chat_handle_join (chat_id, handle_id);
        CREATE TABLE message (ROWID INTEGER PRIMARY KEY, is_from_me, date, text);
        CREATE TABLE chat_message_join (chat_id, message_id);
    """)
    con.executemany("INSERT INTO chat VALUES (?,?,?)", [(1, 45, "iMessage"), (2, 43, "iMessage"), (3, 45, "SMS")])
    con.executemany("INSERT INTO handle VALUES (?,?)", [(1, "+15550100001"), (2, "+15550100002")])
    con.executemany("INSERT INTO chat_handle_join VALUES (?,?)", [(1, 1), (2, 2), (3, 1)])
    con.executemany("INSERT INTO message VALUES (?,?,?,?)",
                    [(1, 1, ns(2), "x"), (2, 0, ns(1), "x"),          # Nate 1:1: you wrote 2 days ago
                     (3, 1, ns(0), "x"),                             # Frank: only in a group chat
                     (4, 1, ns(30), "x")])                           # Nate over SMS, a month ago
    con.executemany("INSERT INTO chat_message_join VALUES (?,?)", [(1, 1), (1, 2), (2, 3), (3, 4)])
    con.commit()
    con.close()
    return str(tmp_path / "Sources" / "*" / "AddressBook-v22.abcddb"), str(chat)


def test_loader_joins_cards_to_one_to_one_chats(dbs):
    ab, chat = dbs
    everyone = {p.display: p for p in people.load_people(contacts_glob=ab, messages_db=chat)}
    nate, frank = everyone["Nate Bowman"], everyone["Frank Schwartz"]
    assert nate.handle() == "+15550100001"                       # the conversation you last wrote in
    assert abs(nate.last_from_me - (NOW - 2 * DAY)) < 5
    assert frank.chats == {} and frank.handle() == "555.010.0002"   # group chats don't count
    assert match(["nate"], list(everyone.values()), now=NOW).tier == "send"
    assert match(["frank"], list(everyone.values()), now=NOW).tier == "ask"


# ------------------------------------------------------------- sound-alikes --
# Code only (no name list, no model). Over-matching is fine here: it only adds a Send tap.

@pytest.mark.parametrize("a,b", [
    ("erin", "aaron"), ("sean", "shawn"), ("lori", "laurie"), ("katie", "kady"), ("jeff", "geoff"),
    ("kris", "chris"), ("stephen", "steven"), ("phil", "fil"), ("cathy", "kathy"), ("jon", "john"),
    ("alan", "allen"), ("sara", "sarah"), ("carl", "karl"), ("eric", "erik"), ("bryan", "brian"),
    ("tracy", "tracey"), ("jackie", "jacky"), ("ann", "anne"), ("isaac", "izaak"), ("nate", "kate"),
    ("dan", "dean"), ("ben", "ken"), ("greg", "craig"),
])
def test_names_that_sound_alike(a, b):
    assert people._sounds_like(a, b) and people._sounds_like(b, a)


@pytest.mark.parametrize("a,b", [
    ("nate", "dave"), ("mike", "mark"), ("frank", "hank"), ("sam", "dan"), ("nate", "neil"),
    ("eric", "aaron"), ("lisa", "laura"), ("dave", "doug"), ("mike", "matt"), ("steve", "stan"),
])
def test_names_that_dont(a, b):
    assert not people._sounds_like(a, b)


def test_a_recent_erin_stops_aaron_sending_without_a_tap():
    d = match(["aaron"], [person("Aaron", "Price", days_ago=1), person("Erin", "Wolfe", days_ago=2)], now=NOW)
    assert (d.tier, names(d)) == ("ask", ["Aaron Price", "Erin Wolfe"])
