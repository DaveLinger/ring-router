"""The general agent (Pydantic AI on OpenRouter + mcp-macos tools + web search).

Every transcript that matches no fixed route comes here: questions and requests alike.

Safety is enforced in code, not just in the prompt:
  * Tool filter: only the tools listed below are visible at all. Mail and Messages sending are
    never listed, so the agent can't email or text anyone.
  * Action guard: mcp-macos tools take an `action` argument (read/create/update/delete), so
    each tool also has an allowed-action set; anything else is refused before it reaches macOS.
  * Dry run: write actions return "DRY RUN: would ..." instead of executing.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from .llm import load_profile
from .prompts import prompt

log = logging.getLogger(__name__)
os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")   # no startup banner in the service log

READ = {"read", "search"}

# tool -> allowed actions. Tools not listed are invisible to the agent.
GENERAL_TOOLS: dict[str, set[str]] = {
    "calendar_events": READ | {"create", "update", "delete"},
    "calendar_calendars": READ,
    "reminders_tasks": READ | {"create", "update", "delete"},
    "reminders_lists": READ,
    "notes_items": READ | {"create", "update"},
    "notes_folders": READ,
    "contacts_people": READ | {"create", "update"},   # add "delete" to allow removing contacts
    # never: mail_messages; messages_chat only for reading, and only if [agents] read_messages = true
}


@dataclass
class AgentSpec:
    name: str                       # "general"
    prompt_name: str                # key in prompts.toml
    tools: dict[str, set[str]]
    dry_run: bool = True
    calls: list[dict] = field(default_factory=list)   # audit log of tool calls this run
    citations: list[dict] = field(default_factory=list)   # sources found by web_search this run
    rerouted: list[dict] = field(default_factory=list)    # commands handed back to the router


def spec_for(kind: str, cfg) -> AgentSpec:
    tools = dict(GENERAL_TOOLS)
    if cfg.section("agents").get("read_messages", False):
        tools["messages_chat"] = READ          # read only: the agent still can't send messages
    return AgentSpec("general", "general_agent", tools, dry_run=cfg.is_dry_run("agent"))


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
        try:
            out = await call_tool(name, args)
        except Exception as e:
            entry["result"], entry["output"] = "error", str(e)[:1500]
            raise
        entry["output"] = _short_output(out)
        return out

    return guard


def _short_output(out: Any, limit: int = 1500) -> str:
    """Tool result for the activity trace (MCP results can be text, dicts or content blocks)."""
    if isinstance(out, str):
        s = out
    else:
        try:
            s = json.dumps(out, default=str, ensure_ascii=False)
        except Exception:
            s = str(out)
    return s if len(s) <= limit else s[:limit] + f"\u2026 ({len(s)} chars)"


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


def default_searcher(cfg) -> Callable[[str], tuple[str, list[dict]]]:
    """web_search backend: one OpenRouter call with the web plugin, summarised with sources."""
    from .llm import OpenRouter  # noqa: PLC0415

    ac = cfg.section("agents")
    client = OpenRouter(cfg.secret("openrouter_api_key"))
    model_id = ac.get("web_search_model", "deepseek/deepseek-v4.1-flash")

    def search(query: str) -> tuple[str, list[dict]]:
        reply = client.chat(model_id, [
            {"role": "system", "content": "Answer the search query factually and concisely from current web "
                                          "results. Include specifics (names, addresses, prices, dates)."},
            {"role": "user", "content": query}], web=True, web_results=int(ac.get("web_results", 5)))
        return reply.text, reply.citations

    return search


def history_messages(history: list[dict]) -> list:
    """Plain {'role','content'} turns -> Pydantic AI message history."""
    from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart  # noqa: PLC0415

    out = []
    for m in history:
        if m["role"] == "user":
            out.append(ModelRequest(parts=[UserPromptPart(m["content"])]))
        else:
            out.append(ModelResponse(parts=[TextPart(m["content"])]))
    return out


def excerpt_of(command: str, transcript: str) -> bool:
    """True if `command` appears word-for-word in `transcript` (case/punctuation ignored)."""
    from .text import simple  # noqa: PLC0415

    c, t = simple(command), simple(transcript)
    return bool(c) and f" {c} " in f" {t} "


def run_agent(kind: str, request: str, cfg, *, history: list[dict] | None = None, model=None,
              mcp_source=None, searcher=None, rerouter=None) -> tuple[str, AgentSpec]:
    """Run one agent request synchronously. Returns (reply text, spec with the call audit log).

    history:  earlier {'role','content'} turns of the agent conversation (follow-ups).
    rerouter: callable(command) -> (route_name, summary) that runs a command through the router
              from the top. Enables the run_command tool.
    """
    from pydantic_ai import Agent, Tool  # noqa: PLC0415

    spec = spec_for(kind, cfg)
    search = searcher or default_searcher(cfg)

    def web_search(query: str) -> str:
        """Search the web for current information (places, prices, news, facts). Write a specific,
        self-contained query, e.g. 'cheapest Chinese restaurant Charleston WV'."""
        text, cites = search(query)
        spec.calls.append({"tool": "web_search", "action": "read", "args": {"query": query}, "result": "ok",
                           "output": _short_output(text), "sources": [c["url"] for c in cites]})
        for c in cites:
            if c["url"] not in {x["url"] for x in spec.citations}:
                spec.citations.append(c)
        sources = "\n".join(f"- {c['title']}: {c['url']}" for c in cites)
        return text + (f"\n\nSources:\n{sources}" if sources else "")

    def run_command(command: str) -> str:
        """Hand a voice command back to the router when the transcript contains one of the fixed
        commands (e.g. 'ask deepseek ...', 'tell Terri ...', 'make a note ...', 'remind me ...',
        'add ... to my shopping list', 'research ...') surrounded by background talk or noise.
        `command` must be copied EXACTLY from the transcript: only trim the noise around it, never
        add, change or reword words. Use at most once."""
        entry = {"tool": "run_command", "action": "reroute", "args": {"command": command}}
        if spec.rerouted:
            entry["result"] = "blocked"; spec.calls.append(entry)
            return "Refused: a command was already handed back once for this request."
        if not excerpt_of(command, request):
            entry["result"] = "blocked"; spec.calls.append(entry)
            return ("Refused: the command must be copied word-for-word from the transcript. "
                    "Only remove the surrounding noise; don't add or change words.")
        route_name, summary = rerouter(command)
        entry["output"] = f"{route_name}: {summary}"
        if route_name == "agent":
            entry["result"] = "blocked"; spec.calls.append(entry)
            return "That isn't one of the fixed commands, so handle the request yourself."
        entry["result"] = "ok"; spec.calls.append(entry)
        spec.rerouted.append({"command": command, "route": route_name, "summary": summary})
        return (f"Handed to the {route_name} route: {summary}. The user has been notified by that "
                f"route; reply with one short line saying what you handed off.")

    tools = [Tool(web_search)] + ([Tool(run_command)] if rerouter else [])
    agent = Agent(model or default_model(cfg), instructions=instructions(spec),
                  toolsets=[build_toolset(spec, mcp_source or default_mcp_source(cfg))],
                  tools=tools, retries=2, tool_timeout=180)
    result = agent.run_sync(request, message_history=history_messages(history or []) or None)
    return str(result.output).strip(), spec
