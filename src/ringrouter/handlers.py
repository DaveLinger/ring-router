"""Route handlers. Each takes (Route, Context) and returns a Result.

Dry-run is enforced here: in dry run a handler does everything up to the side effect
(rewrites, titles, item splitting, model calls) and reports what it WOULD do.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from typing import Callable

from . import md, mcp
from .fm import FM, checked_rewrite
from .llm import LLMStore, OpenRouter, Reply, load_profile
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
    llm: OpenRouter = field(default=None)
    llm_store: LLMStore = field(default=None)
    notes_writer: Callable = field(default=None)   # (title, html, folder) -> None
    agent_runner: Callable = field(default=None)   # (kind, request, cfg, history) -> (text, AgentSpec)

    def __post_init__(self):
        if self.mcp_call is None:
            cmd = self.cfg.section("macos_mcp").get("command", ["npx", "-y", "mcp-macos@4.1.0"])
            self.mcp_call = lambda name, args: mcp.call_tool(cmd, name, args)
        if self.llm is None:
            self.llm = OpenRouter(self.cfg.secret("openrouter_api_key"))
        if self.llm_store is None:
            self.llm_store = LLMStore(self.store)
        if self.notes_writer is None:
            self.notes_writer = applescript_note
        if self.agent_runner is None:
            self.agent_runner = _default_agent_runner

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


NOTE_SCRIPT = """on run argv
  set noteTitle to item 1 of argv
  set noteBody to item 2 of argv
  set folderName to item 3 of argv
  tell application "Notes"
    if not (exists folder folderName) then make new folder with properties {name:folderName}
    make new note at folder folderName with properties {name:noteTitle, body:noteBody}
  end tell
end run"""


def applescript_note(title: str, body_html: str, folder: str) -> None:
    """Create an Apple Note directly (no 2,000-character cap, unlike mcp-macos).

    Text goes in as osascript arguments, never spliced into the script, so model output
    can't inject AppleScript.
    """
    import html as _html  # noqa: PLC0415
    full = f"<h1>{_html.escape(title)}</h1>\n{body_html}"
    subprocess.run(["/usr/bin/osascript", "-e", NOTE_SCRIPT, title, full, folder],
                   check=True, capture_output=True, text=True, timeout=60)


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

def system_prompt(*names: str) -> str:
    parts = [prompt("spoken_preamble"), load_profile()] + [prompt(n) for n in names]
    return "\n\n".join(p for p in parts if p)


def _short(text: str, limit: int) -> str:
    t = " ".join(md.plain(text).split())
    return t if len(t) <= limit else t[: limit - 1].rsplit(" ", 1)[0] + "…"


def h_llm(r: Route, ctx: Context) -> Result:
    """'Ask <model> ...': answer as a notification; full answer + sources on the answer page."""
    key = r.params["model"]
    m = ctx.cfg.raw["models"][key]
    name = m.get("display", key)
    oc = ctx.cfg.section("openrouter")
    history = ctx.llm_store.history(key, int(oc.get("followup_window_s", 600)))
    question = r.body
    if history:   # make the follow-up standalone so the web search has something real to find
        prev = next((m["content"] for m in reversed(history) if m["role"] == "user"), "")
        try:
            rewritten = ctx.fm.standalone(prev, r.body).strip().strip('"')
            if rewritten and len(rewritten) < 4 * max(len(r.body), 40):
                question = rewritten
        except Exception as e:
            log.warning("follow-up rewrite failed, sending as spoken: %s", e)
    messages = ([{"role": "system", "content": system_prompt("llm_answer")}] + history
                + [{"role": "user", "content": question}])
    try:
        reply = ctx.llm.chat(m["model"], messages, web=bool(m.get("web_search", oc.get("web_search_ask", True))),
                             web_results=int(oc.get("web_results_ask", 3)))
    except Exception as e:
        ctx.notify(name, f"Not asked: {e}")
        return Result("failed", str(e))
    ctx.llm_store.remember(key, question, reply.text)
    aid = ctx.llm_store.save_answer("ask", m["model"], question, reply)
    limit = int(ctx.cfg.section("notify").get("max_chars", 300))
    buttons = []
    if len(reply.text) > limit or reply.citations:
        buttons = [Button("Full answer", f"{ctx.public_url}/answers/{aid}")]
    ctx.notify(name, _short(reply.text, limit), buttons)
    follow = f", follow-up of {len(history) // 2}" if history else ""
    cost = f", ${reply.cost:.4f}" if reply.cost is not None else ""
    return Result("done", f"{name} answered ({len(reply.text)} chars, {len(reply.citations)} sources{follow}{cost})")


def h_research(r: Route, ctx: Context) -> Result:
    """'Research ...': web-backed write-up saved as an Apple Note; Pushcut only says it's ready."""
    key = r.params.get("model") or ctx.cfg.section("research").get("default_model", "chatgpt")
    m = ctx.cfg.raw["models"][key]
    rc = ctx.cfg.section("research")
    folder = rc.get("notes_folder", "Research")
    messages = [{"role": "system", "content": system_prompt("research")}, {"role": "user", "content": r.body}]
    try:
        reply = ctx.llm.chat(m["model"], messages, web=True, web_results=int(rc.get("web_results", 8)))
    except Exception as e:
        ctx.notify("Research", f"Not done: {e}")
        return Result("failed", str(e))
    aid = ctx.llm_store.save_answer("research", m["model"], r.body, reply)
    try:
        title = ctx.fm.title(r.body)
    except Exception:
        title = _short(r.body, 60)
    body = reply.text
    if reply.citations and "sources" not in body.lower()[-2000:]:
        body += "\n\n## Sources\n" + "\n".join(f"- [{c['title']}]({c['url']})" for c in reply.citations)
    link = [Button("Read it", f"{ctx.public_url}/answers/{aid}")]
    if ctx.dry("research"):
        ctx.notify("Research ready (dry run)", f"“{title}” — note not saved", link)
        return Result("dry_run", f"DRY RUN: would save note {title!r} in {folder!r} ({len(body)} chars)")
    ctx.notes_writer(title, md.to_html(body), folder)
    ctx.notify("Research ready", f"“{title}” in Notes › {folder}", link)
    return Result("done", f"research note {title!r} in {folder!r} ({len(body)} chars)")


def h_agent(r: Route, ctx: Context, kind: str) -> Result:
    """Questions agent (read-only) or general agent (macOS tools, no mail/messages)."""
    label = "Answer" if kind == "questions" else "Done"
    window = int(ctx.cfg.section("openrouter").get("followup_window_s", 600))
    history = ctx.llm_store.history(AGENT_THREAD, window)   # one thread shared by both agents
    try:
        text, spec = ctx.agent_runner(kind, r.body, ctx.cfg, history)
    except Exception as e:
        log.exception("%s agent failed", kind)
        ctx.notify("Not done", f"{e}. Heard: \u201c{r.transcript}\u201d")
        return Result("failed", f"{kind} agent error: {e}")
    ctx.llm_store.remember(AGENT_THREAD, r.body, text)
    calls = ", ".join(f"{c['tool']}.{c['action']}={c['result']}" for c in spec.calls) or "no tools"
    limit = int(ctx.cfg.section("notify").get("max_chars", 300))
    dry = any(c["result"] == "dry_run" for c in spec.calls)
    buttons = []
    if len(md.plain(text)) > limit or spec.citations:
        model_id = ctx.cfg.section("agents").get("model", "anthropic/claude-sonnet-5.5")
        aid = ctx.llm_store.save_answer(kind, model_id, r.body, Reply(text, spec.citations, model_id))
        buttons = [Button("Full answer", f"{ctx.public_url}/answers/{aid}")]
    ctx.notify(label + (" (dry run)" if dry else ""), _short(text, limit), buttons)
    follow = f", follow-up of {len(history) // 2}" if history else ""
    return Result("dry_run" if dry else "done", f"{kind} agent: {_short(text, 120)} [{calls}{follow}]")


AGENT_THREAD = "agent"   # follow-up thread key shared by the questions and general agents


def _default_agent_runner(kind: str, request: str, cfg, history: list[dict] | None = None):
    from .agents import run_agent  # noqa: PLC0415  (pydantic-ai imported only when an agent runs)
    return run_agent(kind, request, cfg, history=history)


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
    "questions": lambda r, ctx: h_agent(r, ctx, "questions"),
    "agent": lambda r, ctx: h_agent(r, ctx, "general"),
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
