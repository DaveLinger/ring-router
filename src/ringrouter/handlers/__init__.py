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
from typing import Callable

from ..notify import Button, Notice  # noqa: F401  (re-exported)
from ..router import Route
from .apple import (EVENT_SCHEMA, GEO_PATTERNS, REMINDER_SCHEMA, applescript_note, default_end,  # noqa: F401
                    h_calendar, h_geo_reminder, h_list_add, h_note, h_reminder, h_remember, my_card_address,
                    parse_geo)
from .context import Context, Result
from .home import CMD_TO_ACTION, execute_home, get_hubitat, h_home, h_scene, run_home  # noqa: F401
from .messages import compose_imessage, h_imessage, parse_contacts, resolve_handle, send_imessage  # noqa: F401
from .models import AGENT_THREAD, _default_agent_runner, h_agent, h_llm, h_research, system_prompt  # noqa: F401

log = logging.getLogger(__name__)

# Slow routes reply "accepted" to the Pebble app right away and finish in the background.
SLOW_ROUTES = {"llm", "research", "agent", "claude_code"}
# Routes that act on the world right away. A recording delivered long after it was spoken (the ring
# was out of range, the phone was offline) asks first instead of running: holding the button meant
# "now", not "whenever this arrives".
LATE_CONFIRM_ROUTES = {"imessage", "home", "scene"}


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
        return execute_home(ctx, payload)
    return Result("failed", f"unknown confirmation kind {kind}")
