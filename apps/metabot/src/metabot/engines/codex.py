"""The ``codex`` engine: the user's own ``codex`` CLI, ``codex exec --json``.

``codex`` runs its shell commands inside its own sandbox (``workspace-write`` by
default, ``read-only`` for a read-only job) and never asks for approval in a
headless run. MetaBot's gate applies to the switchboard's tools; the
engine's own commands are bounded by the sandbox and recorded as steps.

Settings go in as ``-c key=value`` overrides because ``exec resume`` accepts
neither ``-s`` nor ``-C``. The user's ``config.toml`` is skipped by default
(``codex_ignore_user_config``), so a personal setting cannot widen the
sandbox; sign-in still comes from the user's own ``codex`` home.

Tested against codex-cli 0.154.0.
"""

from __future__ import annotations

import json
from typing import Any

from metabot.config import Config, load_config
from metabot.engines.base import CliEngine, EngineJob
from metabot.events import (
    ErrorEvent,
    Event,
    Final,
    Notice,
    Started,
    TextEvent,
    ToolCall,
    ToolResult,
    Usage,
)

UNKNOWN_TEXT_LIMIT = 500


def _toml(value: Any) -> str:
    """A TOML literal for a string or a list of strings (JSON's syntax is valid TOML here)."""
    return json.dumps(value, ensure_ascii=False)


class CodexEngine(CliEngine):
    name = "codex"
    binary = "codex"
    tool_prefix = "codex"
    skip_tools = ("mcp/metabot/",)

    def __init__(self, config: Config | None = None) -> None:
        self.config = config if config is not None else load_config()

    def argv(self, job: EngineJob) -> list[str]:
        argv = [self.binary, "exec"]
        if job.resume:
            argv.append("resume")
        argv += ["--json", "--skip-git-repo-check"]
        if self.config.codex_ignore_user_config:
            argv.append("--ignore-user-config")
        sandbox = "read-only" if job.read_only else "workspace-write"
        argv += ["-c", f"sandbox_mode={_toml(sandbox)}"]
        argv += ["-c", f"approval_policy={_toml('never')}"]
        if job.model:
            argv += ["-m", job.model]
        if job.switchboard:
            argv += ["-c", f"mcp_servers.metabot.command={_toml(job.switchboard[0])}"]
            argv += ["-c", f"mcp_servers.metabot.args={_toml(job.switchboard[1:])}"]
            # The switchboard gates every call itself (and fails closed). Left at its
            # default, codex asks first, and under approval_policy=never that refuses
            # every call before MetaBot sees it (live, 2026-10-11).
            argv += ["-c", f"mcp_servers.metabot.default_tools_approval_mode={_toml('approve')}"]
        if job.resume:
            return [*argv, "--", job.resume, job.prompt]
        return [*argv, "-C", str(job.cwd), "--", job.prompt]

    def _call(self, item: dict[str, Any], state: dict[str, Any]) -> list[Event]:
        item_id = str(item.get("id", ""))
        kind = item.get("type")
        started: set[str] = state.setdefault("started", set())
        if item_id in started:
            return []
        started.add(item_id)
        if kind == "command_execution":
            return [ToolCall(item_id, "command_execution", {"command": item.get("command")})]
        if kind == "file_change":
            return [ToolCall(item_id, "file_change", {"changes": item.get("changes", [])})]
        if kind == "mcp_tool_call":
            args = item.get("arguments")
            tool = f"mcp/{item.get('server')}/{item.get('tool')}"
            return [ToolCall(item_id, tool, args if isinstance(args, dict) else {"value": args})]
        if kind == "web_search":
            return [ToolCall(item_id, "web_search", {"query": item.get("query")})]
        return []

    def _item_completed(self, item: dict[str, Any], state: dict[str, Any]) -> list[Event]:
        item_id = str(item.get("id", ""))
        kind = item.get("type")
        if kind == "agent_message":
            text = str(item.get("text", ""))
            state["last_text"] = text
            return [TextEvent(text)] if text else []
        if kind in ("reasoning", "todo_list"):
            return []
        if kind == "error":  # in codex's own words, "a non-fatal error notice"
            return [Notice(f"codex: {item.get('message', '')}")]
        events = self._call(item, state)
        if kind == "command_execution":
            code = item.get("exit_code")
            output = {"output": item.get("aggregated_output", ""), "exit_code": code}
            return [*events, ToolResult(item_id, output, code == 0)]
        if kind == "file_change":
            status = item.get("status")
            return [*events, ToolResult(item_id, {"status": status}, status == "completed")]
        if kind == "mcp_tool_call":
            ok = item.get("status") == "completed" and not item.get("error")
            result = item.get("result") if ok else {"error": item.get("error")}
            return [*events, ToolResult(item_id, result, ok)]
        if kind == "web_search":
            return [*events, ToolResult(item_id, {}, True)]
        text = json.dumps(item, ensure_ascii=False)[:UNKNOWN_TEXT_LIMIT]
        return [TextEvent(f"[codex {kind}] {text}")]

    def parse_event(self, obj: dict[str, Any], state: dict[str, Any]) -> list[Event]:
        kind = obj.get("type")
        if kind == "thread.started":
            state["thread_id"] = obj.get("thread_id")
            return [Started(obj.get("thread_id"), None)]
        if kind == "item.started":
            item = obj.get("item")
            return self._call(item, state) if isinstance(item, dict) else []
        if kind == "item.completed":
            item = obj.get("item")
            return self._item_completed(item, state) if isinstance(item, dict) else []
        if kind == "turn.completed":
            usage = obj.get("usage")
            final = Final(state.get("last_text", ""), state.get("thread_id"))
            return [Usage(usage if isinstance(usage, dict) else {}), final]
        if kind == "turn.failed":
            error = obj.get("error")
            message = error.get("message") if isinstance(error, dict) else error
            final = Final(state.get("last_text", ""), state.get("thread_id"))
            return [ErrorEvent(f"codex: turn failed: {message}"), final]
        if kind == "error":  # retries and warnings; a failure is turn.failed or the exit code
            return [Notice(f"codex: {obj.get('message', '')}")]
        return []  # turn.started, item.updated, and anything newer that carries no item
