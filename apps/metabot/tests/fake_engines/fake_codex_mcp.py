"""A stand-in codex: prints ``codex exec --json`` lines, and makes one call through the
switchboard it is given, over MCP, as codex does.

``FAKE_CODEX_MODE`` says when it says it made the call: ``print-first``, before it sends
it; ``in-flight``, once the connection's server has it (``FAKE_CODEX_COUNT`` is that
server's count file); ``done-first``, once the call is answered. Then it waits to be
stopped. ``FAKE_CODEX_TOOL`` names the tool, ``FAKE_CODEX_ARGS`` its arguments.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


def _option(argv: list[str], key: str) -> Any:
    for i, arg in enumerate(argv[:-1]):
        if arg == "-c" and argv[i + 1].startswith(key + "="):
            return json.loads(argv[i + 1][len(key) + 1 :])
    return None


def _say(line: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(line) + "\n")
    sys.stdout.flush()


def main() -> int:
    argv = sys.argv[1:]
    command = _option(argv, "mcp_servers.metabot.command")
    args = _option(argv, "mcp_servers.metabot.args") or []
    mode, tool = os.environ["FAKE_CODEX_MODE"], os.environ["FAKE_CODEX_TOOL"]
    arguments = json.loads(os.environ.get("FAKE_CODEX_ARGS", "{}"))
    _say({"type": "thread.started", "thread_id": "th-1"})
    _say({"type": "turn.started"})
    board = subprocess.Popen(
        [command, *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True,
    )  # fmt: skip
    assert board.stdin is not None and board.stdout is not None

    def send(message: dict[str, Any]) -> None:
        board.stdin.write(json.dumps(message) + "\n")  # type: ignore[union-attr]
        board.stdin.flush()  # type: ignore[union-attr]

    def answer() -> dict[str, Any]:
        while True:
            message = json.loads(board.stdout.readline())  # type: ignore[union-attr]
            if "id" in message:
                return message

    send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "fake-codex", "version": "0"}}})  # fmt: skip
    answer()
    send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": tool, "arguments": arguments}}  # fmt: skip
    item = {
        "id": "item_1", "type": "mcp_tool_call", "server": "metabot", "tool": tool,
        "arguments": arguments, "result": None, "error": None, "status": "in_progress",
    }  # fmt: skip
    if mode == "print-first":
        _say({"type": "item.started", "item": item})
        send(call)
    elif mode == "in-flight":
        send(call)
        count = Path(os.environ["FAKE_CODEX_COUNT"])
        for _ in range(600):
            if count.exists() and count.read_text().strip():
                break
            time.sleep(0.02)
        _say({"type": "item.started", "item": item})
    else:  # done-first
        send(call)
        reply = answer()
        _say({"type": "item.started", "item": item})
        _say({"type": "item.completed", "item": {**item, "status": "completed",
              "result": reply.get("result")}})  # fmt: skip
    time.sleep(60)  # until the run stops it
    return 0


if __name__ == "__main__":
    sys.exit(main())
