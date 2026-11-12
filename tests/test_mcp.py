"""The stdio MCP client against tiny fake servers (plain Python scripts)."""

import sys
import time

import pytest

from ringrouter import mcp

SERVER = r"""
import json, sys, time
mode = sys.argv[1]
print("starting up (a log line, not JSON)", flush=True)
for line in sys.stdin:
    m = json.loads(line)
    if "id" not in m:
        continue
    if m["method"] == "initialize":
        if mode == "hang_on_start":
            time.sleep(60)
        print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": {}}), flush=True)
    elif m["method"] == "tools/call":
        if mode == "hang_on_call":
            time.sleep(60)
        if mode == "exit":
            sys.exit(0)
        err = mode == "tool_error"
        print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": {
            "isError": err, "content": [{"type": "text", "text": "boom" if err else "done: " + m["params"]["name"]}]}}),
            flush=True)
"""


def cmd(mode):
    return [sys.executable, "-c", SERVER, mode]


def test_call_returns_text_and_skips_log_lines():
    assert mcp.call_tool(cmd("ok"), "notes_items", {"action": "read"}, timeout_s=20) == "done: notes_items"


@pytest.mark.parametrize("mode", ["hang_on_start", "hang_on_call"])
def test_hung_server_times_out(mode):
    t0 = time.monotonic()
    with pytest.raises(TimeoutError):
        mcp.call_tool(cmd(mode), "messages_chat", {"action": "create"}, timeout_s=1.5)
    assert time.monotonic() - t0 < 10        # killed, not left to finish its 60 s sleep


def test_tool_error_and_early_exit_are_mcp_errors():
    with pytest.raises(mcp.MCPError, match="boom"):
        mcp.call_tool(cmd("tool_error"), "x", {}, timeout_s=20)
    with pytest.raises(mcp.MCPError, match="exited"):
        mcp.call_tool(cmd("exit"), "x", {}, timeout_s=20)
