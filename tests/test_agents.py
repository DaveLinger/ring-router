"""Phase 6: general + questions agents. Real Pydantic AI + MCP plumbing against a fake
in-process mcp-macos server; a scripted FunctionModel stands in for the LLM."""

import pytest

pytest.importorskip("pydantic_ai")

from fastmcp import FastMCP  # noqa: E402
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart  # noqa: E402
from pydantic_ai.models.function import FunctionModel  # noqa: E402

from ringrouter.agents import run_agent  # noqa: E402
from ringrouter.handlers import Context, h_agent  # noqa: E402
from ringrouter.notify import ConsoleNotifier  # noqa: E402
from ringrouter.router import route  # noqa: E402
from ringrouter.store import Store  # noqa: E402


def fake_macos():
    """Same tool names/shape as mcp-macos (one tool per app, `action` argument)."""
    mcp = FastMCP("fake-mcp-macos")
    executed = []

    @mcp.tool
    def calendar_events(action: str, title: str = "", startDate: str = "", id: str = "") -> str:
        executed.append(("calendar_events", action, title))
        if action == "read":
            return "1 event: Dentist (id=ev1) Tue 2026-11-10 15:00"
        return f"{action}d event {title or id}"

    @mcp.tool
    def reminders_tasks(action: str, title: str = "") -> str:
        executed.append(("reminders_tasks", action, title))
        return f"{action} ok"

    @mcp.tool
    def contacts_people(action: str, search: str = "") -> str:
        executed.append(("contacts_people", action, search))
        return "Instinct: 12 Main St"

    @mcp.tool
    def messages_chat(action: str, text: str = "", to: str = "") -> str:
        executed.append(("messages_chat", action, text))
        return "sent"

    @mcp.tool
    def mail_messages(action: str, subject: str = "") -> str:
        executed.append(("mail_messages", action, subject))
        return "draft created"

    return mcp, executed


def scripted(*steps):
    """FunctionModel that issues the given tool calls in order, then replies with text.
    Records which tools the agent was offered."""
    seen = {}

    def fn(messages, info):
        seen["tools"] = sorted(t.name for t in info.function_tools)
        n = sum(1 for m in messages for p in getattr(m, "parts", []) if isinstance(p, ToolReturnPart))
        if n < len(steps):
            name, args = steps[n]
            return ModelResponse(parts=[ToolCallPart(name, args)])
        last = [p for m in messages for p in getattr(m, "parts", []) if isinstance(p, ToolReturnPart)]
        return ModelResponse(parts=[TextPart("final: " + (str(last[-1].content) if last else "no tools"))])

    return FunctionModel(fn), seen


def test_questions_agent_sees_only_read_tools(cfg):
    mcp, executed = fake_macos()
    model, seen = scripted(("calendar_events", {"action": "read"}))
    text, spec = run_agent("questions", "What's on my calendar Tuesday?", cfg, model=model, mcp_source=mcp)
    assert seen["tools"] == ["calendar_events", "contacts_people", "reminders_tasks"]   # no mail/messages
    assert executed == [("calendar_events", "read", "")] and "Dentist" in text


def test_questions_agent_cannot_write(cfg):
    mcp, executed = fake_macos()
    model, _ = scripted(("calendar_events", {"action": "delete", "id": "ev1"}))
    text, spec = run_agent("questions", "Delete my dentist appointment", cfg, model=model, mcp_source=mcp)
    assert executed == [] and spec.calls[0]["result"] == "blocked" and "Not allowed" in text


def test_questions_agent_messages_opt_in(cfg):
    cfg.raw.setdefault("agents", {})["questions_read_messages"] = True
    mcp, _ = fake_macos()
    model, seen = scripted()
    run_agent("questions", "Did Terri text me?", cfg, model=model, mcp_source=mcp)
    assert "messages_chat" in seen["tools"] and "mail_messages" not in seen["tools"]


def test_general_agent_has_no_mail_or_messages(cfg):
    mcp, _ = fake_macos()
    model, seen = scripted()
    run_agent("general", "hello", cfg, model=model, mcp_source=mcp)
    assert "mail_messages" not in seen["tools"] and "messages_chat" not in seen["tools"]
    assert "calendar_events" in seen["tools"]


def test_general_agent_dry_run_does_not_execute_writes(cfg):
    mcp, executed = fake_macos()
    model, _ = scripted(("calendar_events", {"action": "read"}),
                        ("calendar_events", {"action": "update", "id": "ev1", "startDate": "2026-11-17 15:00:00"}))
    text, spec = run_agent("general", "Move my dentist appointment to next week", cfg, model=model, mcp_source=mcp)
    assert executed == [("calendar_events", "read", "")]            # the update never reached macOS
    assert [c["result"] for c in spec.calls] == ["ok", "dry_run"] and "DRY RUN" in text


def test_general_agent_live_executes_writes(cfg):
    cfg.raw["server"]["dry_run"] = False
    cfg.raw["server"]["dry_run_routes"] = []
    mcp, executed = fake_macos()
    model, _ = scripted(("reminders_tasks", {"action": "create", "title": "Renew registration"}))
    text, spec = run_agent("general", "add renew registration to my reminders", cfg, model=model, mcp_source=mcp)
    assert executed == [("reminders_tasks", "create", "Renew registration")] and spec.calls[0]["result"] == "ok"


def test_general_agent_creates_contact(cfg):
    cfg.raw["server"]["dry_run"] = False
    cfg.raw["server"]["dry_run_routes"] = []
    mcp, executed = fake_macos()
    model, _ = scripted(("contacts_people", {"action": "create", "search": "John Smith 555-1212"}))
    text, spec = run_agent("general", "make a new contact named John Smith with phone number 555-1212",
                           cfg, model=model, mcp_source=mcp)
    assert executed == [("contacts_people", "create", "John Smith 555-1212")] and spec.calls[0]["result"] == "ok"


def test_general_agent_blocks_contact_delete(cfg):
    cfg.raw["server"]["dry_run"] = False
    mcp, executed = fake_macos()
    model, _ = scripted(("contacts_people", {"action": "delete", "search": "Instinct"}))
    text, spec = run_agent("general", "delete Instinct from my contacts", cfg, model=model, mcp_source=mcp)
    assert executed == [] and spec.calls[0]["result"] == "blocked"


def test_instructions_include_date_profile_and_prompt(cfg):
    from ringrouter.agents import instructions, spec_for
    text = instructions(spec_for("general", cfg))
    assert "Current date and time:" in text and "speech-to-text transcript" in text
    assert "ask Instinct to find a time" in text


def test_handler_notifies_and_reports_calls(cfg, fake_fm):
    mcp, _ = fake_macos()
    model, _ = scripted(("calendar_events", {"action": "read"}))
    ctx = Context(cfg=cfg, notifier=ConsoleNotifier(echo=False), store=Store(":memory:"), fm=fake_fm,
                  agent_runner=lambda kind, req, c: run_agent(kind, req, c, model=model, mcp_source=mcp))
    res = h_agent(route("What's on my calendar Thursday?", cfg), ctx, "questions")
    assert res.state == "done" and "calendar_events.read=ok" in res.summary
    assert ctx.notifier.sent[-1].title == "Answer" and "Dentist" in ctx.notifier.sent[-1].text


def test_handler_reports_missing_key(cfg, fake_fm):
    ctx = Context(cfg=cfg, notifier=ConsoleNotifier(echo=False), store=Store(":memory:"), fm=fake_fm)
    res = h_agent(route("Move my dentist appointment to next week", cfg), ctx, "general")
    assert res.state == "failed" and "API key" in ctx.notifier.sent[-1].text
