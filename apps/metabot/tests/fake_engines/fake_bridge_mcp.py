"""A stand-in bridge (``metabot.bridge/1``): it makes one call through the switchboard its
job names, over MCP, and reports it as ``metabot/`` and the tool's name, as the protocol asks.

``FAKE_BRIDGE_TOOL`` names the tool, ``FAKE_BRIDGE_ARGS`` its arguments. With
``FAKE_BRIDGE_COUNT``, the connection's server's count file, it reports the call once that
server has it, then waits to be stopped. With ``FAKE_BRIDGE_WITHDRAW``, a file written when a
person is asked about the call, it gives up on the call once that file is there, as an MCP
client does (``notifications/cancelled``), waits, and reports it failed. Else it reports the
call and its answer, and ends.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


def _say(line: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(line) + "\n")
    sys.stdout.flush()


def main() -> int:
    job = json.loads(Path(sys.argv[-1]).read_text(encoding="utf-8"))
    tool = os.environ["FAKE_BRIDGE_TOOL"]
    arguments = json.loads(os.environ.get("FAKE_BRIDGE_ARGS", "{}"))
    _say({"type": "started", "session_id": "b-1", "model": job.get("model")})
    board = subprocess.Popen(
        job["switchboard"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
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
        "clientInfo": {"name": "fake-bridge", "version": "0"}}})  # fmt: skip
    answer()
    send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    send({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
          "params": {"name": tool, "arguments": arguments}})  # fmt: skip
    call = {"type": "tool_call", "call_id": "b1", "tool": f"metabot/{tool}", "args": arguments}
    withdraw = os.environ.get("FAKE_BRIDGE_WITHDRAW")
    if withdraw:
        for _ in range(1500):
            if Path(withdraw).exists():
                break
            time.sleep(0.02)
        send({"jsonrpc": "2.0", "method": "notifications/cancelled",
              "params": {"requestId": 2, "reason": "gave up"}})  # fmt: skip
        time.sleep(3)  # long enough for the person to answer, if they were still asked
        _say(call)
        _say({"type": "tool_result", "call_id": "b1", "output": {"error": "gave up"},
              "ok": False})  # fmt: skip
        _say({"type": "final", "text": "done", "session_id": "b-1"})
        return 0
    count = os.environ.get("FAKE_BRIDGE_COUNT")
    if count:
        for _ in range(600):
            if Path(count).exists() and Path(count).read_text().strip():
                break
            time.sleep(0.02)
        _say(call)
        time.sleep(60)  # until the run stops it
        return 0
    result = answer().get("result") or {}
    _say(call)
    _say({"type": "tool_result", "call_id": "b1", "output": result,
          "ok": not result.get("isError")})  # fmt: skip
    _say({"type": "final", "text": "done", "session_id": "b-1"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
