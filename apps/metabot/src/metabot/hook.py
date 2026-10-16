"""The pre-tool-use gate hook: the engine runs it before every tool call.

It reads the engine's hook JSON on stdin and prints a decision. Reads pass
here; in a run with the switchboard, its own tools pass here because the
switchboard gates them itself (without one, a server of the person's or the
project's could take the same name, so they are gated like any other tool);
everything else is decided by the runner, which holds the policy and the
human. Any failure blocks the call (exit 2), so a broken gate never fails
open.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from metabot import control
from metabot.scope_classes import ScopeClass
from metabot.shell_policy import classify_engine_tool

SWITCHBOARD_PREFIX = "mcp__metabot__"
PREVIEW_LIMIT = 2000
REQUEST_TIMEOUT_S = 870.0  # under the engine's own hook timeout (900 s)
BLOCKING_EXIT = 2


def _decision(allow: bool, reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow" if allow else "deny",
            "permissionDecisionReason": f"metabot: {reason}",
        }
    }


def pre_tool_use(
    payload: dict[str, Any], control_path: Path, prefix: str, switchboard: bool = False
) -> dict[str, Any]:
    tool = str(payload.get("tool_name") or "")
    args = payload.get("tool_input")
    args = args if isinstance(args, dict) else {"value": args}
    if switchboard and tool.startswith(SWITCHBOARD_PREFIX):
        return _decision(True, "the switchboard gates its own tools")
    cls = classify_engine_tool(tool, args)
    if cls is ScopeClass.READ:
        return _decision(True, "read")
    reply = control.request_sync(
        control_path,
        {
            "op": "approve",
            "call_id": str(payload.get("tool_use_id") or "unknown"),
            "tool": f"{prefix}/{tool}",
            "cls": cls.label,
            "preview": json.dumps(args, ensure_ascii=False)[:PREVIEW_LIMIT],
        },
        timeout=REQUEST_TIMEOUT_S,
    )
    if "error" in reply:
        raise RuntimeError(f"the runner refused the request: {reply['error']}")
    return _decision(bool(reply.get("allow")), str(reply.get("reason") or cls.label))


def main(control_path: str, prefix: str = "claude-code", switchboard: bool = False) -> int:
    try:
        payload = json.loads(sys.stdin.read())
        if not isinstance(payload, dict):
            raise ValueError("the hook input is not an object")
        print(json.dumps(pre_tool_use(payload, Path(control_path), prefix, switchboard)))
        return 0
    except Exception as exc:  # fail closed: exit 2 blocks the tool call
        print(f"metabot gate unavailable, call blocked: {exc}", file=sys.stderr)
        return BLOCKING_EXIT
