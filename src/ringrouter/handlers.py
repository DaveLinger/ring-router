"""Route handlers. Each takes (Route, Context) and returns a Result.

Dry-run is enforced here: in dry run a handler does everything up to the side effect
(rewrites, titles, item splitting, model calls) and reports what it WOULD do.
"""

from __future__ import annotations

import json
import logging
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

from . import mcp
from .fm import FM, checked_rewrite
from .notify import Button, Notice
from .prompts import prompt
from .router import Route
from .store import Store

log = logging.getLogger(__name__)

# Slow routes reply "accepted" to the Pebble app right away and finish in the background.
SLOW_ROUTES = {"llm", "research", "questions", "agent", "claude_code"}


@dataclass
class Result:
    state: str          # done | dry_run | cancelled | refused | discarded | needs_confirm | failed | unknown
    summary: str
    http_status: int = 200


@dataclass
class Context:
    cfg: object
    notifier: object
    store: Store
    fm: FM
    delivery_id: str = ""
    public_url: str = ""                 # base URL for confirm buttons
    mcp_call: Callable = field(default=None)

    def __post_init__(self):
        if self.mcp_call is None:
            cmd = self.cfg.section("macos_mcp").get("command", ["npx", "-y", "mcp-macos@4.1.0"])
            self.mcp_call = lambda name, args: mcp.call_tool(cmd, name, args)

    def dry(self, route: str) -> bool:
        return self.cfg.is_dry_run(route)

    def notify(self, title: str, text: str, buttons: list[Button] | None = None) -> None:
        self.notifier.send(Notice(title, text, buttons or []))


def _dry_tag(ctx: Context, route: str) -> str:
    return "DRY RUN: would " if ctx.dry(route) else ""


# ------------------------------------------------------------------ simple ----

def h_discard(r: Route, ctx: Context) -> Result:
    return Result("discarded", r.reason)


def h_cancelled(r: Route, ctx: Context) -> Result:
    ctx.notify("Cancelled", f"Nothing done. Heard: “{r.transcript}”")
    return Result("cancelled", f"cancelled ({r.params['phrase']!r})")


def h_refuse(r: Route, ctx: Context) -> Result:
    ctx.notify("Not done", r.params.get("message", "Not supported."))
    return Result("refused", r.params.get("message", "refused"))


# ------------------------------------------------------------------ iMessage --

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
    if not text:
        return Result("discarded", "empty message")

    payload = {"recipient": r.params["recipient"], "text": text}
    if r.params.get("tier") == "weak":
        pid = ctx.store.add_pending(ctx.delivery_id, "imessage", payload,
                                    ctx.cfg.section("notify").get("confirm_ttl_s", 300))
        ctx.notify("Confirm message", f"Send to {display}: “{text}”? (heard “{r.params['heard']}”)",
                   [Button("Send", f"{ctx.public_url}/confirm/{pid}/yes"),
                    Button("Cancel", f"{ctx.public_url}/confirm/{pid}/no")])
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
        args["to"] = resolve_handle(rc, ctx)
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
    if rc.get("handle"):
        return rc["handle"]
    out = ctx.mcp_call("contacts_people", {"action": "search", "search": rc["contact"]})
    import re  # noqa: PLC0415
    phones = re.findall(r"\+?\d[\d\s().-]{7,}\d", out)
    emails = re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", out)
    if not (phones or emails):
        raise RuntimeError(f"no phone/email found for contact {rc['contact']!r}")
    return (phones or emails)[0]


# ------------------------------------------------------------- Apple apps ----

def h_note(r: Route, ctx: Context, folder: str | None = None, route_name: str = "note") -> Result:
    folder = folder or r.params.get("folder", "Notes")
    try:
        title = ctx.fm.title(r.body)
    except Exception as e:
        log.warning("fm title failed: %s", e)
        title = " ".join(r.body.split()[:6])
    title = title.strip().strip('"')[:200]
    if ctx.dry(route_name):
        ctx.notify("Dry run", f"Would create note “{title}” in {folder}")
        return Result("dry_run", f"DRY RUN: would create note {title!r} in {folder!r}: {r.body[:2000]!r}")
    ctx.mcp_call("notes_items", {"action": "create", "title": title, "body": r.body[:2000], "folder": folder})
    ctx.notify("Note created", f"{title} ({folder})")
    return Result("done", f"note {title!r} in {folder!r}")


def h_remember(r: Route, ctx: Context) -> Result:
    return h_note(r, ctx, folder=r.params.get("folder", "Remember"), route_name="remember")


def h_list_add(r: Route, ctx: Context) -> Result:
    lst = ctx.cfg.raw["lists"][r.params["list"]]
    rl = lst["reminders_list"]
    try:
        items = ctx.fm.split_items(r.body) or [r.body]
    except Exception as e:
        log.warning("fm split failed, adding as one item: %s", e)
        items = [r.body]
    items = [i[:1].upper() + i[1:] for i in items]
    if ctx.dry("list_add"):
        ctx.notify("Dry run", f"Would add to {rl}: {', '.join(items)}")
        return Result("dry_run", f"DRY RUN: would add to {rl!r}: {items}")
    for it in items:
        ctx.mcp_call("reminders_tasks", {"action": "create", "title": it, "targetList": rl})
    ctx.notify(f"Added to {rl}", ", ".join(items))
    return Result("done", f"added {items} to {rl!r}")


REMINDER_SCHEMA = {"title": "Reminder", "type": "object", "additionalProperties": False,
                   "required": ["title"], "x-order": ["title", "due"],
                   "properties": {"title": {"type": "string", "description": "What to be reminded of, short"},
                                  "due": {"type": "string", "description":
                                          "Local due time 'YYYY-MM-DD HH:mm:ss', or empty if none was said"}}}


def h_reminder(r: Route, ctx: Context) -> Result:
    if r.params.get("location_trigger"):
        msg = "Location reminders (“when I get home”) aren't built yet."
        ctx.notify("Not done", msg)
        return Result("refused", msg)
    try:
        data = ctx.fm.extract(r.body, prompt("reminder_extract"), REMINDER_SCHEMA)
    except Exception as e:
        log.warning("fm extract failed: %s", e)
        data = {"title": r.body}
    title, due = data.get("title") or r.body, (data.get("due") or "").strip()
    when = f" due {due}" if due else ""
    if ctx.dry("reminder"):
        ctx.notify("Dry run", f"Would create reminder “{title}”{when}")
        return Result("dry_run", f"DRY RUN: would create reminder {title!r}{when}")
    args = {"action": "create", "title": title}
    if due:
        args["dueDate"] = due
    ctx.mcp_call("reminders_tasks", args)
    ctx.notify("Reminder set", f"{title}{when}")
    return Result("done", f"reminder {title!r}{when}")


EVENT_SCHEMA = {"title": "Event", "type": "object", "additionalProperties": False,
                "required": ["title", "start"], "x-order": ["title", "start", "end", "all_day"],
                "properties": {"title": {"type": "string"},
                               "start": {"type": "string", "description": "Local 'YYYY-MM-DD HH:mm:ss'"},
                               "end": {"type": "string", "description": "Local 'YYYY-MM-DD HH:mm:ss'; empty if not said"},
                               "all_day": {"type": "boolean"}}}


def h_calendar(r: Route, ctx: Context) -> Result:
    cal = r.params.get("calendar")
    try:
        data = ctx.fm.extract(r.body, prompt("calendar_extract"), EVENT_SCHEMA)
    except Exception as e:
        msg = f"Couldn't work out the event time: {e}"
        ctx.notify("Not done", msg)
        return Result("failed", msg)
    desc = f"“{data.get('title')}” {data.get('start')}" + (f" to {data['end']}" if data.get("end") else "")
    if ctx.dry("calendar") or not cal or cal == "CHANGE-ME":
        ctx.notify("Dry run", f"Would add {desc} to {cal}")
        return Result("dry_run", f"DRY RUN: would add {desc} to calendar {cal!r}")
    args = {"action": "create", "title": data["title"], "startDate": data["start"], "targetCalendar": cal}
    if data.get("end"):
        args["endDate"] = data["end"]
    if data.get("all_day"):
        args["isAllDay"] = True
    ctx.mcp_call("calendar_events", args)
    ctx.notify("Event added", f"{desc} ({cal})")
    return Result("done", f"event {desc} on {cal!r}")


# ---------------------------------------------------------------- home --------

def h_home(r: Route, ctx: Context) -> Result:
    what = f"{r.params['action']} {r.body}".strip()
    if r.params.get("confirm"):
        pid = ctx.store.add_pending(ctx.delivery_id, "home", {"action": r.params["action"], "target": r.body})
        ctx.notify("Confirm", f"{what}?", [Button("Yes", f"{ctx.public_url}/confirm/{pid}/yes"),
                                           Button("Cancel", f"{ctx.public_url}/confirm/{pid}/no")])
        return Result("needs_confirm", f"home action needs confirmation: {what} ({pid})")
    msg = f"Home control isn't built yet. Would: {what}"
    ctx.notify("Not done", msg)
    return Result("dry_run" if ctx.dry("home") else "refused", msg)


def h_scene(r: Route, ctx: Context) -> Result:
    scene = ctx.cfg.raw["scenes"][r.params["scene"]]
    acts = scene.get("actions", [])
    msg = f"Scene '{r.params['scene']}': {len(acts)} action(s) (home control not built yet)"
    ctx.notify("Scene", msg)
    return Result("dry_run", msg)


# ---------------------------------------------------------------- models -------

def openrouter_chat(ctx: Context, model_key: str, prompt: str, system: str = "") -> str:
    m = ctx.cfg.raw["models"][model_key]
    key = ctx.cfg.secret("openrouter_api_key")
    if not key or "CHANGE-ME" in m["model"]:
        raise RuntimeError(f"OpenRouter not configured for {m.get('display', model_key)} "
                           "(set openrouter_api_key and the model ID)")
    msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
    req = urllib.request.Request("https://openrouter.ai/api/v1/chat/completions", method="POST",
                                 data=json.dumps({"model": m["model"], "messages": msgs}).encode(),
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                                          "X-Title": "ring-router"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.load(resp)
    return data["choices"][0]["message"]["content"].strip()


def h_llm(r: Route, ctx: Context) -> Result:
    m = ctx.cfg.raw["models"][r.params["model"]]
    try:
        answer = openrouter_chat(ctx, r.params["model"], r.body, prompt("spoken_preamble") + "\n\n" + prompt("llm_answer"))
    except Exception as e:
        ctx.notify(m.get("display", "Model"), f"Not asked: {e}")
        return Result("failed", str(e))
    ctx.notify(m.get("display", "Model"), answer)
    return Result("done", f"{m.get('display')} answered ({len(answer)} chars)")


def h_research(r: Route, ctx: Context) -> Result:
    model = r.params.get("model")
    msg = f"Research isn't built yet. Would research with {model}: “{r.body}” and save a note."
    ctx.notify("Not done", msg)
    return Result("dry_run", msg)


def _stub(name: str) -> Callable:
    def h(r: Route, ctx: Context) -> Result:
        extra = f" on {r.params.get('host')} ({r.params.get('mode')})" if name == "Claude Code" else ""
        msg = f"{name} isn't built yet. Would handle{extra}: “{r.body}”"
        ctx.notify("Not done", msg)
        return Result("dry_run", msg)
    return h


HANDLERS: dict[str, Callable[[Route, Context], Result]] = {
    "discard": h_discard,
    "cancelled": h_cancelled,
    "refuse": h_refuse,
    "imessage": h_imessage,
    "note": h_note,
    "remember": h_remember,
    "list_add": h_list_add,
    "reminder": h_reminder,
    "calendar": h_calendar,
    "home": h_home,
    "scene": h_scene,
    "llm": h_llm,
    "research": h_research,
    "questions": _stub("Questions agent"),
    "agent": _stub("General agent"),
    "claude_code": _stub("Claude Code"),
}


def run(r: Route, ctx: Context) -> Result:
    try:
        return HANDLERS[r.route](r, ctx)
    except Exception as e:
        log.exception("handler %s failed", r.route)
        ctx.notify("Error", f"{r.route}: {e}. Heard: “{r.transcript}”")
        return Result("failed", f"{r.route} error: {e}", 422)


def run_confirmed(kind: str, payload: dict, ctx: Context) -> Result:
    if kind == "imessage":
        return send_imessage(payload, ctx)
    if kind == "home":
        msg = f"Home control isn't built yet. Would: {payload['action']} {payload['target']}"
        ctx.notify("Not done", msg)
        return Result("dry_run", msg)
    return Result("failed", f"unknown confirmation kind {kind}")
