"""iMessage: compose (the on-device "ask" rewrite), confirm weak matches, resolve the recipient, send."""

from __future__ import annotations

import json
import logging
import re

from .. import people
from ..fm import checked_rewrite
from ..notify import Button
from ..router import Route, clean_body
from .context import Context, Result

log = logging.getLogger(__name__)

LAST_RECIPIENT = "last_message_recipient"     # kv key: who "her" / "him" / "them" means
LAST_CONTACT = "last_message_contact"         # kv key: that recipient's details, when it came from Contacts
PRONOUN_FORMS = {"she": "her", "her": "her", "he": "him", "him": "him", "they": "them", "them": "them"}


def recipient(ctx: Context, d: dict) -> dict:
    """A recipient's settings: from config.toml, or carried along (`rc`) for a Contacts match."""
    return d["rc"] if d.get("rc") else ctx.cfg.raw["recipients"][d["recipient"]]


def _remember_recipient(ctx: Context, payload: dict) -> None:
    ctx.store.set_kv(LAST_RECIPIENT, payload["recipient"])
    if payload.get("rc"):
        ctx.store.set_kv(LAST_CONTACT, json.dumps(payload["rc"]))


def recipient_pronoun(rc: dict) -> str | None:
    """'her' / 'him' / 'them' for a recipient: its `pronoun` setting; a group chat is 'them'."""
    p = PRONOUN_FORMS.get(str(rc.get("pronoun", "")).lower().split("/")[0].strip())
    return p or ("them" if rc.get("chat_id") else None)


def h_imessage_followup(r: Route, ctx: Context) -> Result:
    """"Tell her I'll grab dinner" right after messaging Terri: 'her' is whoever was messaged last,
    within [messages] followup_window_s. Sends straight away when the pronoun fits that recipient's
    `pronoun`; asks with a Send tap when it doesn't (or none is set). Never guesses past the window."""
    said = r.params["pronoun"]
    window = int(ctx.cfg.section("messages").get("followup_window_s", 600))
    key = ctx.store.get_kv(LAST_RECIPIENT, window)
    recips = dict(ctx.cfg.raw.get("recipients", {}))
    contact = None
    if key and key.startswith("contact:"):          # last sent to someone found in Contacts
        contact = json.loads(ctx.store.get_kv(LAST_CONTACT) or "null")
        if contact:
            recips[key] = contact
    if not key or key not in recips:
        msg = (f"Who's “{said}”? You haven't messaged anyone in the last {window // 60} minutes. "
               f"Nothing sent. Say the name, e.g. “tell Terri …”.")
        ctx.notify("Not sent", msg)
        return Result("refused", f"no recent recipient for {said!r}")
    rc = recips[key]
    fits = recipient_pronoun(rc) == said
    ctx.trace("pronoun", said=said, recipient=rc.get("display", key), fits=fits,
              recipient_pronoun=recipient_pronoun(rc) or "unset")
    rerouted = r.params.get("tier") == "weak"                  # the agent dug it out of noise: always ask
    params = {**r.params, "recipient": key, "tier": "strong" if fits and not rerouted else "weak",
              "heard": r.params.get("heard") or said, "rc": contact}
    return h_imessage(Route("imessage", r.transcript, body=r.body, verb=r.verb, params=params,
                            reason=f"{said!r} = last recipient {key}"), ctx)


def compose_imessage(r: Route, ctx: Context) -> tuple[str, bool]:
    """The exact text that will be sent, and whether the on-device rewrite was used."""
    rc = recipient(ctx, r.params)
    display = rc.get("display", r.params["recipient"])
    text = r.body
    if r.params.get("rewrite") == "ask_question" and text:
        try:
            out = ctx.fm.rewrite_ask(display, text, group="chat_id" in rc)
            names = [display] + list(rc.get("sounds_like", []))
            return checked_rewrite(text, out, [n for n in names if n.lower() != "the guys"])
        except Exception as e:
            log.warning("fm rewrite failed, sending as spoken: %s", e)
    return text, False


def h_imessage(r: Route, ctx: Context) -> Result:
    if (r.params.get("tier") == "weak" and not r.params.get("rc") and r.params.get("recipient")
            and _is_contact_name(r.params.get("heard"), ctx)):
        # "Tell Sherry ..." is a near-miss of Terri, but Sherry is a real contact: she wins
        return h_imessage_contact(Route("imessage_contact", r.transcript, body=r.body, verb=r.verb, params={
            "heard": r.params["heard"], "kind": r.verb, "rest": r.body,
            "rewrite": r.params.get("rewrite", "none")}, reason="exact Contacts name beats a near-miss"), ctx)
    rc = recipient(ctx, r.params)
    display = rc.get("display", r.params["recipient"])
    text, rewrote = compose_imessage(r, ctx)
    ctx.trace("message_composed", recipient=display, tier=r.params.get("tier"), heard=r.params.get("heard"),
              spoken=r.body, text=text, rewritten=rewrote)
    if not text:
        return Result("discarded", "empty message")

    payload = {"recipient": r.params["recipient"], "text": text}
    if r.params.get("rc"):
        payload["rc"] = r.params["rc"]
    if r.params.get("tier") == "weak":
        pid = ctx.store.add_pending(ctx.delivery_id, "imessage", payload,
                                    ctx.cfg.section("notify").get("confirm_ttl_s", 300))
        why = f", {r.params['why']}" if r.params.get("why") else ""
        ctx.notify("Confirm message", f"Send to {display}{why}: “{text}”? (heard “{r.params['heard']}”)",
                   [Button("Send", f"{ctx.public_url}/confirm/{pid}/yes", background=True),
                    Button("Cancel", f"{ctx.public_url}/confirm/{pid}/no", background=True)])
        return Result("needs_confirm", f"weak match {r.params['heard']!r} -> {display}; confirmation {pid}")
    return send_imessage(payload, ctx, rewrote)


def send_imessage(payload: dict, ctx: Context, rewrote: bool = False) -> Result:
    rc = recipient(ctx, payload)
    display = rc.get("display", payload["recipient"])
    text = payload["text"]
    note = " (rewritten)" if rewrote else ""
    live_only = ctx.cfg.server.get("live_recipients")   # testing safety: only these get real sends
    is_live = live_only is None or payload["recipient"] in live_only or (payload.get("rc") and "contacts" in live_only)
    if ctx.dry("imessage") or not is_live:
        ctx.notify("Dry run", f"Would send to {display}{note}: “{text}”")
        _remember_recipient(ctx, payload)                          # "tell her..." follows a dry run too
        return Result("dry_run", f"DRY RUN: would send to {display}{note}: {text!r}")
    args = {"action": "create", "text": text}
    if rc.get("chat_id"):
        args["chatId"] = rc["chat_id"]
    else:
        try:
            args["to"] = resolve_handle(rc, ctx)
        except Exception as e:      # nothing was sent: the lookup failed, timed out or was ambiguous
            ctx.notify("Not sent", f"To {display}: {e}")
            return Result("failed", f"recipient lookup failed, nothing sent: {e}", 422)
    ctx.store.set_state(ctx.delivery_id, "sending")
    try:
        ctx.mcp_call("messages_chat", args)
    except TimeoutError:
        _remember_recipient(ctx, payload)                          # it may well have gone; "tell her" still means them
        ctx.notify("Send status unknown", f"To {display}: “{text}”. Check Messages; not retrying.")
        return Result("unknown", "send timed out; status unknown, will not resend", 422)
    except Exception as e:
        ctx.notify("Not sent", f"To {display}: {e}")
        return Result("failed", f"send failed: {e}", 422)
    _remember_recipient(ctx, payload)
    ctx.notify(f"Sent to {display}", f"“{text}”{note}")
    return Result("done", f"sent to {display}: {text!r}")


def _people(ctx: Context) -> list[people.Person]:
    return ctx.people_loader()


def _is_contact_name(heard: str | None, ctx: Context) -> bool:
    """Is `heard` exactly the first name (or nickname) of a Contacts card? Only with [messages] contacts on."""
    if not heard or " " in heard or not ctx.cfg.section("messages").get("contacts"):
        return False
    try:
        return any(people.simple(heard) in p.first_names() for p in _people(ctx))
    except Exception:
        log.warning("Contacts lookup failed", exc_info=True)
        return False


def _after_words(rest: str, n: int) -> str:
    if n <= 0:
        return rest
    m = re.match(r"\s*(?:[\w']+[\s,.:;!?\-–—…]*){%d}" % n, rest)
    return rest[m.end():] if m else rest


def h_imessage_contact(r: Route, ctx: Context) -> Result:
    """A name that isn't in config.toml: Contacts cards ranked by recent 1:1 Messages (people.match).
    One clear recent match, or an exact first + last name, sends; anything less asks with a Send tap
    per person; no card refuses (or, for "ask X what ...", goes to the agent)."""
    mc = ctx.cfg.section("messages")
    heard = r.params["heard"]
    try:
        everyone = _people(ctx)
    except Exception as e:
        ctx.notify("Not sent", f"Couldn't read Contacts or Messages ({e}). Heard: “{r.transcript}”")
        return Result("failed", f"contacts lookup failed, nothing sent: {e}", 422)
    d = people.match([heard] + r.params["rest"].split()[:1], everyone, recent_days=int(mc.get("recent_days", 7)),
                     max_choices=int(mc.get("max_choices", 3)))
    ctx.trace("contact_match", heard=d.heard or heard, decision=d.tier, words=d.words,
              choices=[{"name": c.person.display, "why": c.why, "handle": c.person.handle()} for c in d.choices])
    if d.tier == "none":
        if r.params.get("fallback") == "agent":
            from . import run  # noqa: PLC0415
            return run(Route("agent", r.transcript, body=r.transcript,
                             reason=f"no contact named {heard!r}: general agent"), ctx)
        ctx.notify("Not sent", f"Didn't recognize “{heard}” in Contacts, nothing sent.")
        return Result("refused", f"no contact named {heard!r}")

    kind = r.params.get("kind", "tell")
    body = clean_body(_after_words(r.params["rest"], d.words - 1), kind)   # rest starts after the first name

    def as_recipient(c: people.Choice) -> tuple[str, dict]:
        p, h = c.person, c.person.handle()
        return f"contact:{h}", {"display": p.display, "handle": h, "sounds_like": sorted(p.first_names())}

    key, rc = as_recipient(d.choices[0])
    base = {"recipient": key, "rc": rc, "heard": d.heard, "rewrite": r.params.get("rewrite", "none"),
            "why": d.choices[0].why}
    if len(d.choices) == 1:
        sub_r = Route("imessage", r.transcript, body=body, verb=kind,
                      params={**base, "tier": "strong" if d.tier == "send" else "weak"}, reason=r.reason)
        return h_imessage(sub_r, ctx)

    # Several people: one button each. The text is composed once (an "ask" rewrite doesn't depend on who).
    text, rewrote = compose_imessage(Route("imessage", r.transcript, body=body, verb=kind, params=base), ctx)
    ctx.trace("message_composed", recipient=f"one of {len(d.choices)}", tier="choice", heard=d.heard,
              spoken=body, text=text, rewritten=rewrote)
    if not text:
        return Result("discarded", "empty message")
    choices = [dict(zip(("recipient", "rc"), as_recipient(c))) for c in d.choices]
    pid = ctx.store.add_pending(ctx.delivery_id, "imessage", {"text": text, "choices": choices},
                                ctx.cfg.section("notify").get("confirm_ttl_s", 300))
    who = "; ".join(f"{c.person.display} ({c.why})" for c in d.choices)
    buttons = [Button(c.person.display, f"{ctx.public_url}/confirm/{pid}/{i}", background=True)
               for i, c in enumerate(d.choices, 1)]
    ctx.notify("Which one?", f"“{text}” to: {who}? (heard “{d.heard}”)",
               buttons + [Button("Cancel", f"{ctx.public_url}/confirm/{pid}/no", background=True)])
    return Result("needs_confirm", f"{len(d.choices)} contacts match {d.heard!r}; confirmation {pid}")


def default_people_loader(ctx: Context):
    return lambda: people.load_people(since_days=int(ctx.cfg.section("messages").get("history_days", 365)))


def resolve_handle(rc: dict, ctx: Context) -> str:
    """Where to send: the pinned `handle`, else the one Contacts card named `contact`.

    The lookup refuses rather than guesses: exactly one card must match, and only its Phone / Email
    lines are read (never the card ID or anything else in the search output).
    """
    if rc.get("handle"):
        return rc["handle"]
    name = rc["contact"]
    cards = parse_contacts(ctx.mcp_call("contacts_people", {"action": "search", "search": name}))
    exact = [c for c in cards if c["name"].casefold() == name.casefold()]
    cards = exact or cards
    hint = "set `handle` for this recipient in config.toml"
    if len(cards) != 1:
        raise RuntimeError(f"{len(cards) or 'no'} Contacts cards match {name!r}; {hint}")
    card = cards[0]
    mobile = [v for v, label in card["phones"] if re.search(r"mobile|iphone|cell", label, re.IGNORECASE)]
    handles = mobile or [v for v, _ in card["phones"]] or [v for v, _ in card["emails"]]
    if not handles:
        raise RuntimeError(f"the Contacts card for {name!r} has no usable phone or email; {hint}")
    return handles[0]


def parse_contacts(out: str) -> list[dict]:
    """mcp-macos contact search output -> [{"name", "phones": [(value, label)], "emails": [...]}].

    Shape (one block per card):   - **Full Name**
                                    - ID: 1234ABCD-...:ABPerson
                                    - Email: a@b.com (home), c@d.com
                                    - Phone: +1 (555) 010-0199 (mobile), 555-0100 (work)
    """
    cards: list[dict] = []
    for line in out.splitlines():
        head = re.match(r"\s*-\s+\*\*(.+?)\*\*\s*$", line)
        if head:
            cards.append({"name": head.group(1).strip(), "phones": [], "emails": []})
            continue
        field_ = re.match(r"\s+-\s+(Phone|Email):\s*(.+)$", line)
        if not (field_ and cards):
            continue
        for item in field_.group(2).split(", "):
            m = re.match(r"(.*?)(?:\s+\(([^()]*)\))?$", item.strip())
            value, label = m.group(1).strip(), m.group(2) or ""
            if label and re.fullmatch(r"[\d\s-]+", label):      # "(555) 010 (0199)": digits, not a label
                value, label = item.strip(), ""
            if field_.group(1) == "Phone":
                if re.fullmatch(r"\+?[\d\s().-]+", value) and 7 <= len(re.sub(r"\D", "", value)) <= 15:
                    cards[-1]["phones"].append((value, label))
            elif re.fullmatch(r"[\w.+-]+@[\w-]+(\.[\w-]+)+", value):
                cards[-1]["emails"].append((value, label))
    return cards
