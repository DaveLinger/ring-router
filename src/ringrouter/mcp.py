"""Minimal stdio MCP client for mcp-macos (one process per call; simple and stateless)."""

from __future__ import annotations

import json
import subprocess


class MCPError(Exception):
    pass


def macos_mcp_env(cfg) -> dict:
    """Environment for mcp-macos: limit calendars to [calendars] sources (patched EventKit helper)."""
    import os  # noqa: PLC0415
    env = dict(os.environ)
    sources = cfg.section("calendars").get("sources") or []
    if sources:
        env["MCP_MACOS_CALENDAR_SOURCES"] = ",".join(sources)
    return env


def call_tool(command: list[str], name: str, arguments: dict, timeout_s: int = 60, env: dict | None = None) -> str:
    p = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, text=True, env=env)
    try:
        def rpc(i, method, params=None, notify=False):
            msg = {"jsonrpc": "2.0", "method": method, "params": params or {}}
            if not notify:
                msg["id"] = i
            p.stdin.write(json.dumps(msg) + "\n")
            p.stdin.flush()
            if notify:
                return None
            while True:
                line = p.stdout.readline()
                if not line:
                    raise MCPError("mcp server exited")
                r = json.loads(line)
                if r.get("id") == i:
                    if "error" in r:
                        raise MCPError(r["error"].get("message", str(r["error"])))
                    return r["result"]

        rpc(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                              "clientInfo": {"name": "ring-router", "version": "0.1"}})
        rpc(0, "notifications/initialized", notify=True)
        result = rpc(2, "tools/call", {"name": name, "arguments": arguments})
        text = "\n".join(c.get("text", "") for c in result.get("content", []))
        if result.get("isError"):
            raise MCPError(text)
        return text
    finally:
        p.terminate()
