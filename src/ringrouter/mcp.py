"""Minimal stdio MCP client for mcp-macos (one process per call; simple and stateless)."""

from __future__ import annotations

import json
import queue
import subprocess
import threading
import time


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


def call_tool(command: list[str], name: str, arguments: dict, timeout_s: float = 60, env: dict | None = None) -> str:
    """Start the server, call one tool, return its text.

    Raises TimeoutError when the whole call (start-up included) takes longer than `timeout_s`:
    the server is killed and the caller can't know whether the action happened. A hung server is
    the expected failure on a headless Mac (a privacy prompt nobody can click), so the wait is
    never open-ended.
    """
    p = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, text=True, env=env)
    lines: queue.Queue = queue.Queue()

    def pump():   # a reader thread, so waiting for a reply can time out even if the pipe stays open
        try:
            for line in p.stdout:
                lines.put(line)
        except ValueError:      # stdout closed under us
            pass
        finally:
            lines.put(None)

    threading.Thread(target=pump, daemon=True, name="mcp-reader").start()
    deadline = time.monotonic() + timeout_s
    try:
        def rpc(i, method, params=None, notify=False):
            msg = {"jsonrpc": "2.0", "method": method, "params": params or {}}
            if not notify:
                msg["id"] = i
            try:
                p.stdin.write(json.dumps(msg) + "\n")
                p.stdin.flush()
            except OSError as e:
                raise MCPError("mcp server exited") from e
            if notify:
                return None
            while True:
                try:
                    line = lines.get(timeout=max(0.0, deadline - time.monotonic()))
                except queue.Empty:
                    raise TimeoutError(f"{name}: no reply within {timeout_s:g}s") from None
                if line is None:
                    raise MCPError("mcp server exited")
                try:
                    r = json.loads(line)
                except ValueError:
                    continue            # a log line on stdout, not a JSON-RPC message
                if isinstance(r, dict) and r.get("id") == i:
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
        try:
            p.wait(timeout=2)
        except subprocess.TimeoutExpired:
            p.kill()
