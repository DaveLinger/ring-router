"""Route handlers. Each takes (Route, Context) and returns a Result.

Dry-run is enforced here: in dry run a handler does everything up to the side effect
(rewrites, titles, item splitting, model calls) and reports what it WOULD do.

Layout:  context   Context, Result, tracing wrappers
         messages  iMessage
         apple     notes, lists, reminders, calendar
         home      Hubitat / HomeKit devices and scenes
         models    ask-a-model, research, the general agent
This module holds the route table (HANDLERS) and the two entry points, run() and run_confirmed().
"""

from __future__ import annotations

import logging
import subprocess
from typing import Callable

from ..notify import Button, Notice  # noqa: F401  (re-exported)
from ..router import Route
from .apple import (EVENT_SCHEMA, REMINDER_SCHEMA, applescript_note, default_end,  # noqa: F401
                    h_calendar, h_geo_reminder, h_list_add, h_note, h_reminder, h_remember, my_card_address,
                    parse_geo)
from .claude_code import default_runner as claude_default_runner  # noqa: F401
from .claude_code import h_claude_code, h_claude_proceed, run_proceed  # noqa: F401
from .context import Context, Result
from .home import (CMD_TO_ACTION, execute_home, get_hubitat, h_home, h_scene, home_status, read_back,  # noqa: F401
                   run_home)
from .messages import (compose_imessage, default_people_loader, h_imessage, h_imessage_contact,  # noqa: F401
                       h_imessage_followup, parse_contacts, recipient_pronoun, resolve_handle, send_imessage)
from .models import AGENT_THREAD, _default_agent_runner, h_agent, h_llm, h_research, system_prompt  # noqa: F401

log = logging.getLogger(__name__)

# Slow routes reply "accepted" to the Pebble app right away and finish in the background.
SLOW_ROUTES = {"llm", "research", "agent", "claude_code", "claude_code_proceed"}
SLOW_CONFIRMS = {"claude_code"}      # a Proceed tap that starts minutes of work answers right away
# Routes that act on the world right away. A recording delivered long after it was spoken (the ring
# was out of range, the phone was offline) asks first instead of running: holding the button meant
# "now", not "whenever this arrives".
LATE_CONFIRM_ROUTES = {"imessage", "imessage_followup", "imessage_contact", "home", "scene", "claude_code_proceed"}


# [modules]: each feature can be switched off. A route belongs to the module(s) it needs; research
# needs a model and Notes. The general agent also loses a switched-off module's tools (agents.py,
# models.py), so turning calendar off really means nothing touches the calendar.
MODULES = {"messages": "Messages", "notes": "Notes", "reminders": "Reminders", "calendar": "Calendar",
           "home": "Home control", "models": "Asking models", "agent": "The general agent",
           "claude_code": "Claude Code"}
ROUTE_MODULES = {"imessage": ("messages",), "imessage_followup": ("messages",), "imessage_contact": ("messages",), "note": ("notes",),
                 "remember": ("notes",), "list_add": ("reminders",), "reminder": ("reminders",),
                 "calendar": ("calendar",), "home": ("home",), "scene": ("home",), "llm": ("models",),
                 "research": ("models", "notes"), "agent": ("agent",), "claude_code": ("claude_code",),
                 "claude_code_proceed": ("claude_code",)}


def module_off(route: str, cfg) -> str | None:
    """The [modules] key that turns this route off, or None if it may run."""
    return next((m for m in ROUTE_MODULES.get(route, ()) if not cfg.module_on(m)), None)


def h_discard(r: Route, ctx: Context) -> Result:
    return Result("discarded", r.reason)


def h_cancelled(r: Route, ctx: Context) -> Result:
    ctx.notify("Cancelled", f"Nothing done. Heard: “{r.transcript}”")
    return Result("cancelled", f"cancelled ({r.params['phrase']!r})")


def h_refuse(r: Route, ctx: Context) -> Result:
    ctx.notify("Not done", r.params.get("message", "Not supported."))
    return Result("refused", r.params.get("message", "refused"))


def _stub(name: str) -> Callable:
    def h(r: Route, ctx: Context) -> Result:
        extra = f" on {r.params.get('host')}" if name == "Claude Code" else ""
        msg = f"{name} isn't built yet. Would handle{extra}: “{r.body}”"
        ctx.notify("Not done", msg)
        return Result("dry_run", msg)
    return h


HANDLERS: dict[str, Callable[[Route, Context], Result]] = {
    "discard": h_discard,
    "cancelled": h_cancelled,
    "refuse": h_refuse,
    "imessage": h_imessage,
    "imessage_followup": h_imessage_followup,
    "imessage_contact": h_imessage_contact,
    "note": h_note,
    "remember": h_remember,
    "list_add": h_list_add,
    "reminder": h_reminder,
    "calendar": h_calendar,
    "home": h_home,
    "scene": h_scene,
    "llm": h_llm,
    "research": h_research,
    "agent": lambda r, ctx: h_agent(r, ctx, "general"),
    "claude_code": h_claude_code,
    "claude_code_proceed": h_claude_proceed,
}


def run(r: Route, ctx: Context) -> Result:
    off = module_off(r.route, ctx.cfg)
    if off:
        ctx.notify("Not done", f"{MODULES[off]} is turned off ([modules] {off} = false). Heard: \u201c{r.transcript}\u201d")
        return Result("refused", f"module {off} is off")
    try:
        return HANDLERS[r.route](r, ctx)
    except (TimeoutError, subprocess.TimeoutExpired) as e:      # no answer: we can't know if it happened
        log.exception("handler %s timed out", r.route)
        ctx.notify("Status unknown", f"{r.route}: no answer in time ({e}). It may or may not have happened, "
                                     f"so check before saying it again. Heard: “{r.transcript}”")
        return Result("unknown", f"{r.route} timed out: {e}", 422)
    except Exception as e:
        log.exception("handler %s failed", r.route)
        ctx.notify("Error", f"{r.route}: {e}. Heard: “{r.transcript}”")
        return Result("failed", f"{r.route} error: {e}", 422)


def run_confirmed(kind: str, payload: dict, ctx: Context, choice: int | None = None) -> Result:
    if "choices" in payload:            # "Which one?": the tapped button picks the recipient
        if choice is None or not 1 <= choice <= len(payload["choices"]):
            return Result("refused", "no recipient chosen, nothing sent", 400)
        payload = {k: v for k, v in payload.items() if k != "choices"} | payload["choices"][choice - 1]
    if kind == "imessage":
        return send_imessage(payload, ctx)
    if kind == "home":
        return execute_home(ctx, payload)
    if kind == "claude_code":
        return run_proceed(payload, ctx)
    return Result("failed", f"unknown confirmation kind {kind}")
