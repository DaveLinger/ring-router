"""What every handler gets (Context) and returns (Result), plus the activity-trace wrappers."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from .. import mcp
from ..fm import FM
from ..llm import LLMStore, OpenRouter
from ..notify import Button, Notice
from ..store import Store

log = logging.getLogger(__name__)


@dataclass
class Result:
    # done | dry_run | cancelled | refused | discarded | needs_confirm, or one of three ways to not finish:
    #   failed   nothing happened (the only state a retry of the same delivery may run again)
    #   partial  some of it happened
    #   unknown  no answer in time: it may or may not have happened
    state: str
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
    recorded_at: float | None = None     # Unix time the words were spoken, when the ring reported it
    mcp_call: Callable = field(default=None)
    llm: OpenRouter = field(default=None)
    llm_store: LLMStore = field(default=None)
    notes_writer: Callable = field(default=None)   # (title, html, folder) -> None
    agent_runner: Callable = field(default=None)   # (kind, request, cfg, history, rerouter) -> (text, AgentSpec)
    hubitat_factory: Callable = field(default=None)  # (ctx) -> Hubitat | None
    address_lookup: Callable = field(default=None)   # () -> home address (Contacts My Card)

    def __post_init__(self):
        if self.mcp_call is None:
            cmd = self.cfg.section("macos_mcp").get("command", ["npx", "-y", "mcp-macos@4.1.0"])
            env = mcp.macos_mcp_env(self.cfg)
            self.mcp_call = lambda name, args: mcp.call_tool(cmd, name, args, env=env)
        if self.llm is None:
            self.llm = OpenRouter(self.cfg.secret("openrouter_api_key"))
        if self.llm_store is None:
            self.llm_store = LLMStore(self.store)
        from .. import handlers as h  # noqa: PLC0415  (defaults live on the package, so they can be replaced)
        if self.notes_writer is None:
            self.notes_writer = h.applescript_note
        if self.agent_runner is None:
            self.agent_runner = h._default_agent_runner
        if self.hubitat_factory is None:
            self.hubitat_factory = h.get_hubitat
        if self.address_lookup is None:
            self.address_lookup = h.my_card_address
        # Tracing wrappers: every side effect and model call lands in the activity trace.
        self.mcp_call = _traced_mcp(self, self.mcp_call)
        self.notes_writer = _traced_notes(self, self.notes_writer)
        self.llm = _TracedLLM(self, self.llm)
        self.fm = _TracedFM(self, self.fm)

    def dry(self, route: str) -> bool:
        return self.cfg.is_dry_run(route)

    def spoken_at(self) -> datetime | None:
        """Local time the words were spoken (None if unknown): the "now" for "in 20 minutes"."""
        return datetime.fromtimestamp(self.recorded_at) if self.recorded_at else None

    def trace(self, phase: str, **data) -> None:
        try:
            self.store.add_trace(self.delivery_id, phase, data)
        except Exception:  # tracing must never break a command
            log.debug("trace failed", exc_info=True)

    def notify(self, title: str, text: str, buttons: list[Button] | None = None) -> None:
        self.trace("notification", title=title, text=text,
                   buttons=[{"label": b.label, "url": b.url, "background": b.background} for b in buttons or []])
        self.notifier.send(Notice(title, text, buttons or []))


def _traced_mcp(ctx: Context, call: Callable) -> Callable:
    if getattr(call, "_traced", False):
        return call

    def wrapped(name: str, args: dict):
        ctx.trace("command", target="mcp-macos", tool=name, args=args)
        try:
            out = call(name, args)
        except Exception as e:
            ctx.trace("command_result", tool=name, ok=False, error=str(e))
            raise
        ctx.trace("command_result", tool=name, ok=True, output=str(out)[:2000])
        return out

    wrapped._traced = True
    return wrapped


def _traced_notes(ctx: Context, writer: Callable) -> Callable:
    if getattr(writer, "_traced", False):
        return writer

    def wrapped(title: str, body_html: str, folder: str):
        ctx.trace("command", target="AppleScript Notes", title=title, folder=folder, chars=len(body_html))
        writer(title, body_html, folder)
        ctx.trace("command_result", target="AppleScript Notes", ok=True)

    wrapped._traced = True
    return wrapped


class _TracedLLM:
    """Records each OpenRouter request and reply."""

    def __init__(self, ctx: Context, inner):
        self._ctx, self._inner = ctx, getattr(inner, "_inner", inner)

    def __getattr__(self, name):          # pass everything else through (e.g. test fakes' .calls)
        return getattr(self._inner, name)

    def chat(self, model, messages, **kw):
        user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        self._ctx.trace("model_request", model=model, web=kw.get("web", False), messages=len(messages),
                        history_turns=max(0, (len(messages) - 2) // 2), sent=user,
                        system_prompt=next((m["content"] for m in messages if m["role"] == "system"), ""))
        reply = self._inner.chat(model, messages, **kw)
        self._ctx.trace("model_reply", model=reply.model, chars=len(reply.text), cost=reply.cost,
                        tokens=reply.tokens, sources=reply.citations, text=reply.text[:4000])
        return reply


class _TracedFM:
    """Records each on-device model task (input -> output)."""

    TASKS = ("rewrite_ask", "title", "split_items", "extract", "standalone")

    def __init__(self, ctx: Context, inner):
        self._ctx, self._inner = ctx, getattr(inner, "_inner", inner)

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        if name not in self.TASKS:
            return attr

        def wrapped(*args, **kw):
            try:
                out = attr(*args, **kw)
            except Exception as e:
                self._ctx.trace("on_device_model", task=name, input=[str(a)[:500] for a in args], error=str(e))
                raise
            self._ctx.trace("on_device_model", task=name, input=[str(a)[:500] for a in args
                                                                 if not isinstance(a, dict)], output=out)
            return out

        return wrapped


def _dry_tag(ctx: Context, route: str) -> str:
    return "DRY RUN: would " if ctx.dry(route) else ""
