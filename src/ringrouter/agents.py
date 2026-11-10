"""General agent and read-only questions agent (Pydantic AI on OpenRouter + mcp-macos tools).

Safety is enforced in code, not just in the prompt:
  * Tool filter: only the tools listed for that agent are visible at all. Mail and Messages
    sending are never listed, so the agents can't email or text anyone.
  * Action guard: mcp-macos tools take an `action` argument (read/create/update/delete), so
    each tool also has an allowed-action set; anything else is refused before it reaches macOS.
  * Dry run: write actions return "DRY RUN: would ..." instead of executing.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from .llm import load_profile
from .prompts import prompt

log = logging.getLogger(__name__)

READ = {"read", "search"}

# tool -> allowed actions. Tools not listed are invisible to the agent.
QUESTIONS_TOOLS: dict[str, set[str]] = {
    "calendar_events": READ,
    "calendar_calendars": READ,
    "reminders_tasks": READ,
    "reminders_lists": READ,
    "notes_items": READ,
    "notes_folders": READ,
    "contacts_people": READ,
    "messages_chat": READ,          # removed unless [agents] questions_read_messages = true
}

GENERAL_TOOLS: dict[str, set[str]] = {
    "calendar_events": READ | {"create", "update", "delete"},
    "calendar_calendars": READ,
    "reminders_tasks": READ | {"create", "update", "delete"},
    "reminders_lists": READ,
    "notes_items": READ | {"create", "update"},
    "notes_folders": READ,
    "contacts_people": READ | {"create", "update"},   # add "delete" to allow removing contacts
    # never: mail_messages, messages_chat (the agent can't email or text anyone)
}


@dataclass
class AgentSpec:
    name: str                       # "questions" | "general"
    prompt_name: str                # key in prompts.toml
    tools: dict[str, set[str]]
    dry_run: bool = True
    calls: list[dict] = field(default_factory=list)   # audit log of tool calls this run


def spec_for(kind: str, cfg) -> AgentSpec:
    ac = cfg.section("agents")
    if kind == "questions":
        tools = dict(QUESTIONS_TOOLS)
        if not ac.get("questions_read_messages", False):
            tools.pop("messages_chat")
        return AgentSpec("questions", "questions_agent", tools, dry_run=False)   # read-only: nothing to dry-run
    return AgentSpec("general", "general_agent", dict(GENERAL_TOOLS), dry_run=cfg.is_dry_run("agent"))


def instructions(spec: AgentSpec) -> str:
    now = datetime.now().astimezone()
    parts = [prompt("spoken_preamble"), load_profile(), prompt(spec.prompt_name),
             f"Current date and time: {now:%A %Y-%m-%d %H:%M} ({now.tzname()}). "
             "Dates passed to tools use local time 'YYYY-MM-DD HH:mm:ss'."]
    return "\n\n".join(p for p in parts if p)


def make_guard(spec: AgentSpec) -> Callable:
    """process_tool_call hook: enforce allowed actions and dry run; record every call."""

    async def guard(ctx, call_tool, name: str, args: dict[str, Any]):
        action = str(args.get("action", "read")).lower()
        allowed = spec.tools.get(name, set())
        entry = {"tool": name, "action": action, "args": {k: v for k, v in args.items() if k != "action"}}
        if action not in allowed:
            entry["result"] = "blocked"
            spec.calls.append(entry)
            return (f"Not allowed: the {spec.name} agent can't '{action}' with {name}. "
                    f"Allowed actions: {', '.join(sorted(allowed)) or 'none'}.")
        if spec.dry_run and action not in READ:
            entry["result"] = "dry_run"
            spec.calls.append(entry)
            return f"DRY RUN: would {action} via {name} with {json.dumps(entry['args'])}. Treat it as done."
        entry["result"] = "ok"
        spec.calls.append(entry)
        return await call_tool(name, args)

    return guard


def build_toolset(spec: AgentSpec, mcp_source: Any):
    """mcp_source: a StdioTransport/command for mcp-macos, or an in-process FastMCP server (tests)."""
    from pydantic_ai.mcp import MCPToolset  # noqa: PLC0415

    ts = MCPToolset(mcp_source, process_tool_call=make_guard(spec), tool_error_behavior="retry")
    return ts.filtered(lambda ctx, tool_def: tool_def.name in spec.tools)


def default_model(cfg):
    from pydantic_ai.models.openrouter import OpenRouterModel  # noqa: PLC0415
    from pydantic_ai.providers.openrouter import OpenRouterProvider  # noqa: PLC0415

    key = cfg.secret("openrouter_api_key")
    if not key:
        raise RuntimeError("OpenRouter API key not set (RING_ROUTER_OPENROUTER_API_KEY in .env)")
    model_id = cfg.section("agents").get("model", "anthropic/claude-sonnet-5.5")
    return OpenRouterModel(model_id, provider=OpenRouterProvider(api_key=key, app_title="ring-router"))


def default_mcp_source(cfg):
    from pydantic_ai.mcp import StdioTransport  # noqa: PLC0415

    cmd = cfg.section("macos_mcp").get("command", ["npx", "-y", "mcp-macos@4.1.0"])
    return StdioTransport(command=cmd[0], args=list(cmd[1:]))


def run_agent(kind: str, request: str, cfg, *, model=None, mcp_source=None) -> tuple[str, AgentSpec]:
    """Run one agent request synchronously. Returns (reply text, spec with the call audit log)."""
    from pydantic_ai import Agent  # noqa: PLC0415

    spec = spec_for(kind, cfg)
    agent = Agent(model or default_model(cfg), instructions=instructions(spec),
                  toolsets=[build_toolset(spec, mcp_source or default_mcp_source(cfg))],
                  retries=2, tool_timeout=60)
    result = agent.run_sync(request)
    return str(result.output).strip(), spec
