"""Recipient lookup through Contacts: refuse rather than guess (all names and numbers invented)."""

import pytest

from ringrouter.handlers import Context, parse_contacts, send_imessage
from ringrouter.notify import ConsoleNotifier
from ringrouter.store import Store

# One card, in the shape mcp-macos prints. The ID is one the old "first thing that looks like a
# phone number" search would have picked instead of the phone.
TERRI = """### Contacts matching "Terri Lastname"

- **Terri Lastname**
  - ID: 2F4A8C91-3345-4D21-9876-1234567890AB:ABPerson
  - Email: terri@example.com (home)
  - Phone: 555-010-0111 (home), +1 (555) 010-0199 (mobile)
"""
SENIOR = """- **Terri Lastname Sr**
  - ID: 11111111-2222-3333-4444-555555555555:ABPerson
  - Phone: 555-010-0222 (mobile)
"""


def run(cfg, fake_fm, contacts_out, recipient="terri"):
    cfg.raw["server"]["dry_run"] = False
    calls = []

    def mcp_call(name, args):
        calls.append((name, args))
        return contacts_out if name == "contacts_people" else "sent"

    ctx = Context(cfg=cfg, notifier=ConsoleNotifier(echo=False), store=Store(":memory:"), fm=fake_fm,
                  delivery_id="d1", mcp_call=mcp_call)
    res = send_imessage({"recipient": recipient, "text": "On my way"}, ctx)
    return res, [a for n, a in calls if n == "messages_chat"], ctx


def test_parse_reads_only_phone_and_email_lines():
    card, = parse_contacts(TERRI)
    assert card["name"] == "Terri Lastname"
    assert card["phones"] == [("555-010-0111", "home"), ("+1 (555) 010-0199", "mobile")]
    assert card["emails"] == [("terri@example.com", "home")]


def test_mobile_number_is_used_not_the_card_id(cfg, fake_fm):
    res, sent, _ = run(cfg, fake_fm, TERRI)
    assert res.state == "done" and sent == [{"action": "create", "text": "On my way", "to": "+1 (555) 010-0199"}]


def test_exact_name_wins_over_a_longer_one(cfg, fake_fm):
    res, sent, _ = run(cfg, fake_fm, SENIOR + TERRI)
    assert res.state == "done" and sent[0]["to"] == "+1 (555) 010-0199"


@pytest.mark.parametrize("out,why", [
    (TERRI + TERRI.replace("0199", "0333"), "2 Contacts cards"),          # two cards with the same name
    ('### Contacts matching "Terri Lastname"\n\nNo contacts found matching search.', "no Contacts cards"),
    ("- **Terri Lastname**\n  - ID: 2F4A8C91-3345-4D21-9876-1234567890AB:ABPerson\n", "no usable phone"),
])
def test_ambiguous_or_empty_lookup_sends_nothing(cfg, fake_fm, out, why):
    res, sent, ctx = run(cfg, fake_fm, out)
    assert res.state == "failed" and res.http_status == 422 and sent == []
    assert why in res.summary and ctx.notifier.sent[-1].title == "Not sent"
    assert ctx.store.get_state("d1") is None           # never reached "sending"


def test_email_when_the_card_has_no_phone(cfg, fake_fm):
    res, sent, _ = run(cfg, fake_fm, "- **Terri Lastname**\n  - Email: terri@example.com (home)\n")
    assert sent[0]["to"] == "terri@example.com"


def test_pinned_handle_skips_the_lookup(cfg, fake_fm):
    cfg.raw["recipients"]["terri"]["handle"] = "+15550100123"
    res, sent, _ = run(cfg, fake_fm, "should not be read")
    assert sent[0]["to"] == "+15550100123"
