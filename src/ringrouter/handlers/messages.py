"""iMessage: compose (the on-device "ask" rewrite), confirm weak matches, resolve the recipient, send."""

from __future__ import annotations

import logging
import re

from ..fm import checked_rewrite
from ..notify import Button
from ..router import Route
from .context import Context, Result

log = logging.getLogger(__name__)


def compose_imessage(r: Route, ctx: Context) -> tuple[str, bool]:
    """The exact text that will be sent, and whether the on-device rewrite was used."""
    rc = ctx.cfg.raw["recipients"][r.params["recipient"]]
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
    rc = ctx.cfg.raw["recipients"][r.params["recipient"]]
    display = rc.get("display", r.params["recipient"])
    text, rewrote = compose_imessage(r, ctx)
    ctx.trace("message_composed", recipient=display, tier=r.params.get("tier"), heard=r.params.get("heard"),
              spoken=r.body, text=text, rewritten=rewrote)
    if not text:
        return Result("discarded", "empty message")

    payload = {"recipient": r.params["recipient"], "text": text}
    if r.params.get("tier") == "weak":
        pid = ctx.store.add_pending(ctx.delivery_id, "imessage", payload,
                                    ctx.cfg.section("notify").get("confirm_ttl_s", 300))
        ctx.notify("Confirm message", f"Send to {display}: “{text}”? (heard “{r.params['heard']}”)",
                   [Button("Send", f"{ctx.public_url}/confirm/{pid}/yes", background=True),
                    Button("Cancel", f"{ctx.public_url}/confirm/{pid}/no", background=True)])
        return Result("needs_confirm", f"weak match {r.params['heard']!r} -> {display}; confirmation {pid}")
    return send_imessage(payload, ctx, rewrote)


def send_imessage(payload: dict, ctx: Context, rewrote: bool = False) -> Result:
    rc = ctx.cfg.raw["recipients"][payload["recipient"]]
    display = rc.get("display", payload["recipient"])
    text = payload["text"]
    note = " (rewritten)" if rewrote else ""
    live_only = ctx.cfg.server.get("live_recipients")   # testing safety: only these get real sends
    if ctx.dry("imessage") or (live_only is not None and payload["recipient"] not in live_only):
        ctx.notify("Dry run", f"Would send to {display}{note}: “{text}”")
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
        ctx.notify("Send status unknown", f"To {display}: “{text}”. Check Messages; not retrying.")
        return Result("unknown", "send timed out; status unknown, will not resend", 422)
    except Exception as e:
        ctx.notify("Not sent", f"To {display}: {e}")
        return Result("failed", f"send failed: {e}", 422)
    ctx.notify(f"Sent to {display}", f"“{text}”{note}")
    return Result("done", f"sent to {display}: {text!r}")


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
