"""The gate's runner side: how a tool call the engine is about to make is decided.

The engine runs ``metabot.gate_client`` before every tool call (G1). The client loads the
standard library alone and hands the call, as it came, to the run's control socket; the
runner answers it here. Reads pass; in a run with the switchboard, its own tools pass
because the switchboard gates them itself (without one, a server of the person's or the
project's could take the same name, so they are gated like any other tool); everything
else goes to the runner's approver, which holds the policy and the human. A request that
is not a gate request, and an approver that gives no decision, are answered with an error,
which the client turns into a blocked call (exit 2): a broken gate never fails open.

``metabot hook pre-tool-use`` is the same client, reached through the CLI.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from metabot.scope_classes import ScopeClass
from metabot.shell_policy import classify_engine_tool

SWITCHBOARD_PREFIX = "mcp__metabot__"
PREVIEW_LIMIT = 2000


def triage(payload: dict[str, Any], prefix: str, switchboard: bool = False) -> dict[str, Any]:
    """A decision made here, ``{"allow": ..., "reason": ...}``, or the question for the
    approver, ``{"op": "approve", "call_id", "tool", "cls", "preview"}``."""
    tool = str(payload.get("tool_name") or "")
    args = payload.get("tool_input")
    args = args if isinstance(args, dict) else {"value": args}
    if switchboard and tool.startswith(SWITCHBOARD_PREFIX):
        return {"allow": True, "reason": "the switchboard gates its own tools"}
    cwd = payload.get("cwd")
    cls = classify_engine_tool(tool, args, cwd if isinstance(cwd, str) and cwd else None)
    if cls is ScopeClass.READ:
        return {"allow": True, "reason": "read"}
    return {
        "op": "approve",
        "call_id": str(payload.get("tool_use_id") or "unknown"),
        "tool": f"{prefix}/{tool}",
        "cls": cls.label,
        "preview": json.dumps(args, ensure_ascii=False)[:PREVIEW_LIMIT],
    }


async def gate_call(
    msg: dict[str, Any], approve: Callable[[dict[str, Any]], Awaitable[Any]]
) -> dict[str, Any]:
    """The control socket's ``gate`` op: ``{"payload": object, "prefix": text,
    "switchboard": bool}`` in, ``{"allow": bool, "reason": text}`` or an error out."""
    payload, prefix = msg.get("payload"), msg.get("prefix")
    if not isinstance(payload, dict) or not isinstance(prefix, str) or not prefix:
        return {"error": "a gate request names its payload, an object, and its prefix"}
    question = triage(payload, prefix, msg.get("switchboard") is True)
    if question.get("op") != "approve":
        return question
    reply = await approve(question)
    if not isinstance(reply, dict) or not isinstance(reply.get("allow"), bool):
        return {"error": "the approver gave no decision"}
    return {"allow": reply["allow"], "reason": str(reply.get("reason") or question["cls"])}


def main(control_path: str, prefix: str = "claude-code", switchboard: bool = False) -> int:
    """``metabot hook pre-tool-use``: the gate client, reached through the CLI."""
    from metabot import gate_client

    return gate_client.main(
        ["--control", control_path, "--prefix", prefix, *(["--switchboard"] if switchboard else [])]
    )
