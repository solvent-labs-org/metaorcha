"""The ``cursor`` engine: the user's own ``cursor-agent`` CLI in print mode.

Shell commands run inside the agent's own sandbox (``--sandbox enabled``);
MetaBot never passes ``--force``. Pre-tool hooks do not run in print mode,
so the engine's own tools are bounded by that sandbox and recorded as steps;
MetaBot's gate applies to the switchboard's tools.

The switchboard reaches the agent as a one-server plugin written into the
run's private directory. ``--approve-mcps`` is passed only then, and it
approves every configured server for that run, the user's own included.
So a project that declares its own servers in ``.cursor/mcp.json`` is refused
when connections are on: a repository's server would run with the person's
privileges and outside MetaBot's gate, with nobody asked.

Tested against cursor-agent 2026.10.01.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from metabot import __version__
from metabot.engines.base import CliEngine, EngineJob
from metabot.events import ErrorEvent, Event, Final, Started, TextEvent, ToolCall, ToolResult, Usage
from metabot.paths import write_private

PLUGIN_NAME = "metabot"
SERVER_NAME = "metabot"
SWITCHBOARD_PROVIDER = f"plugin-{PLUGIN_NAME}-{SERVER_NAME}"
DROPPED_ARGS = frozenset({"parsingResult", "toolCallId", "requestId", "conversationId"})


def _tool_body(tool_call: Any) -> tuple[str, dict[str, Any]]:
    """The one ``<kind>ToolCall`` object inside a tool_call event."""
    if isinstance(tool_call, dict):
        for key, value in tool_call.items():
            if key.endswith("ToolCall") and isinstance(value, dict):
                return key, value
        for key, value in tool_call.items():
            if isinstance(value, dict):
                return key, value
    return "unknown", {}


def _call(call_id: str, kind: str, body: dict[str, Any]) -> ToolCall:
    args = body.get("args")
    if not isinstance(args, dict):
        args = {}
    if kind == "mcpToolCall":
        inner = args.get("args")
        tool = f"mcp/{args.get('providerIdentifier')}/{args.get('toolName')}"
        return ToolCall(call_id, tool, inner if isinstance(inner, dict) else {"value": inner})
    return ToolCall(call_id, kind, {k: v for k, v in args.items() if k not in DROPPED_ARGS})


def _ok(result: Any) -> bool:
    if not isinstance(result, dict) or not isinstance(result.get("success"), dict):
        return False
    success = result["success"]
    if success.get("isError") is True:
        return False
    return success.get("exitCode", 0) in (0, None)


def project_declares_servers(cwd: Path) -> bool:
    """True when the project's own ``.cursor/mcp.json`` declares a server, or cannot be read."""
    path = Path(cwd) / ".cursor" / "mcp.json"
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    if not isinstance(data, dict):
        return True
    return data.get("mcpServers", {}) != {}


class CursorEngine(CliEngine):
    name = "cursor"
    binary = "cursor-agent"
    tool_prefix = "cursor"
    loads_plugins = True
    skip_tools = (f"mcp/{SWITCHBOARD_PROVIDER}/",)

    def prepare(self, job: EngineJob) -> EngineJob:
        if not job.switchboard:
            return job
        if job.work_dir is None:
            raise ValueError("connections need a private work directory for the plugin")
        if project_declares_servers(job.cwd):
            raise ValueError(
                "this project declares its own MCP servers in .cursor/mcp.json; with "
                "connections, cursor-agent approves every server, and these would run outside "
                "MetaBot's gate. Run with --no-connections, or use another engine"
            )
        plugin = job.work_dir / "cursor-plugin" / PLUGIN_NAME
        manifest = {
            "name": PLUGIN_NAME,
            "version": __version__,
            "description": "MetaBot switchboard for one run",
        }
        servers = {
            "mcpServers": {
                SERVER_NAME: {"command": job.switchboard[0], "args": job.switchboard[1:]}
            }
        }
        write_private(plugin / ".cursor-plugin" / "plugin.json", json.dumps(manifest) + "\n")
        write_private(plugin / ".mcp.json", json.dumps(servers) + "\n")
        return replace(job, plugin_dirs=[*job.plugin_dirs, plugin])

    def argv(self, job: EngineJob) -> list[str]:
        argv = [self.binary, "-p", "--output-format", "stream-json"]
        argv += ["--workspace", str(job.cwd), "--trust", "--sandbox", "enabled"]
        if job.read_only:
            argv += ["--mode", "ask"]
        if job.model:
            argv += ["--model", job.model]
        for plugin_dir in job.plugin_dirs:
            argv += ["--plugin-dir", str(plugin_dir)]
        if job.switchboard:
            argv.append("--approve-mcps")
        if job.resume:
            argv += ["--resume", job.resume]
        return [*argv, "--", job.prompt]

    def parse_event(self, obj: dict[str, Any], state: dict[str, Any]) -> list[Event]:
        kind = obj.get("type")
        if kind == "system":
            if obj.get("subtype") == "init":
                state["session_id"] = obj.get("session_id")
                return [Started(obj.get("session_id"), obj.get("model"))]
            return []
        if kind == "assistant":
            message = obj.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            events: list[Event] = []
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                    state["last_text"] = str(block["text"])
                    events.append(TextEvent(str(block["text"])))
            return events
        if kind == "tool_call":
            call_id = str(obj.get("call_id", ""))
            tool_kind, body = _tool_body(obj.get("tool_call"))
            started: set[str] = state.setdefault("started", set())
            events = []
            if call_id not in started:
                started.add(call_id)
                events.append(_call(call_id, tool_kind, body))
            if obj.get("subtype") == "completed":
                result = body.get("result")
                events.append(ToolResult(call_id, result, _ok(result)))
            return events
        if kind == "result":
            usage = obj.get("usage")
            events = [Usage(usage if isinstance(usage, dict) else {})]
            if obj.get("is_error"):
                events.append(ErrorEvent(f"cursor: {obj.get('subtype')}: {obj.get('result', '')}"))
            text = state.get("last_text") or str(obj.get("result") or "")
            events.append(Final(text, obj.get("session_id") or state.get("session_id")))
            return events
        return []  # the prompt echoed back, thinking, and anything newer
