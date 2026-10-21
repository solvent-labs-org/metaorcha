"""The ``claude`` engine: the user's own ``claude`` CLI in print mode.

Every tool call the engine makes passes a pre-tool-use hook first. The hook
asks MetaBot's gate, so the user's own allow rules or permission mode cannot
let a write or a destructive call through without the gate's decision. With
``--permission-prompts none``, anything else that would prompt is denied: a
headless run never waits on a prompt nobody can see.

``--strict-mcp-config`` is always passed: the only MCP server is the
switchboard, or none. Servers from the person's config, a plugin or the
project's ``.mcp.json`` never load, so none is gated by its tool names alone.

``--setting-sources ""`` is always passed: neither the person's nor the
repository's settings files load. A repository's committed
``.claude/settings.json`` or ``settings.local.json`` would otherwise run its
hooks and key helper with no gate decision, and its ``env`` could send the
model calls, with the person's login, to another address. Managed settings
that an administrator installs still load. MetaBot passes what a run needs
itself: the gate hook, the plugins and the MCP config. From the person's own
settings file it carries a few keys by name: ``model``, the effort for each
model in ``modelSettings``, the telemetry and error-reporting opt-outs in
``env``, and ``permissions.deny``, which can stop a call but never allow one.
Nothing else in that file is kept or passed on, so a key or a helper in it
is never copied.

Tested against ``claude`` 2.1.302.
"""

from __future__ import annotations

import json
import shlex
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from metabot.engines.base import CliEngine, EngineJob
from metabot.events import ErrorEvent, Event, Final, Started, TextEvent, ToolCall, ToolResult, Usage

HOOK_TIMEOUT_S = 900  # an approval can wait this long for a human
SILENT = frozenset({"rate_limit_event", "stream_event", "keep_alive"})
SETTINGS_LIMIT = 1 << 20  # a settings file bigger than this is not read
CARRIED_ENV = (
    "DISABLE_TELEMETRY",
    "DISABLE_ERROR_REPORTING",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
)


def user_settings_path(env: Mapping[str, str]) -> Path | None:
    """The person's own settings file, where the engine looks for it.

    ``CLAUDE_CONFIG_DIR`` moves it, taken as given: the engine expands no
    ``~``. A relative one would point into the run's folder, a folder the
    repository controls, so it gives no file. With
    ``CLAUDE_CODE_USE_COWORK_PLUGINS`` set, the engine's file is
    ``cowork_settings.json``.
    """
    name = "cowork_settings.json" if env.get("CLAUDE_CODE_USE_COWORK_PLUGINS") else "settings.json"
    raw = env.get("CLAUDE_CONFIG_DIR")
    if raw is None:
        return Path.home() / ".claude" / name
    folder = Path(raw)
    return folder / name if folder.is_absolute() else None


def _read_settings(path: Path) -> dict[str, Any]:
    try:
        if not path.is_file():  # a FIFO or a device could keep a read waiting
            return {}
        with path.open("rb") as fh:
            raw = fh.read(SETTINGS_LIMIT + 1)
        if len(raw) > SETTINGS_LIMIT:
            return {}
        data = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def carried_settings(env: Mapping[str, str], *, model_set: bool) -> dict[str, Any]:
    """The person's own choices a run keeps, each read by name.

    ``model``, unless the run sets one; ``effortLevel`` for each model in
    ``modelSettings``, where the engine keeps the effort chosen with
    ``/effort``; the telemetry and error-reporting opt-outs in ``env``; and
    ``permissions.deny``. A top-level ``effortLevel`` is left out: the engine
    reads it from a person's file and from a flag in different ways. A file
    that is missing, too big, not text or not a JSON object carries nothing.
    """
    path = user_settings_path(env)
    data = _read_settings(path) if path else {}
    kept: dict[str, Any] = {}
    if isinstance(data.get("model"), str) and not model_set:
        kept["model"] = data["model"]
    models = data.get("modelSettings")
    if isinstance(models, dict):
        efforts = {
            name: {"effortLevel": entry["effortLevel"]}
            for name, entry in models.items()
            if isinstance(name, str)
            and isinstance(entry, dict)
            and isinstance(entry.get("effortLevel"), str)
        }
        if efforts:
            kept["modelSettings"] = efforts
    env_block = data.get("env")
    if isinstance(env_block, dict):
        kept_env = {k: env_block[k] for k in CARRIED_ENV if isinstance(env_block.get(k), str)}
        if kept_env:
            kept["env"] = kept_env
    permissions = data.get("permissions")
    deny = permissions.get("deny") if isinstance(permissions, dict) else None
    if isinstance(deny, list):
        rules = [rule for rule in deny if isinstance(rule, str)]
        if rules:
            kept["permissions"] = {"deny": rules}
    return kept


def hook_settings(gate_hook: list[str]) -> dict[str, Any]:
    return {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "*",
                    "hooks": [
                        {
                            "type": "command",
                            "command": shlex.join(gate_hook),
                            "timeout": HOOK_TIMEOUT_S,
                        }
                    ],
                }
            ]
        }
    }


def mcp_servers(switchboard: list[str]) -> dict[str, Any]:
    return {"mcpServers": {"metabot": {"command": switchboard[0], "args": switchboard[1:]}}}


def _blocks(obj: dict[str, Any]) -> list[dict[str, Any]]:
    message = obj.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def _result_content(content: Any) -> Any:
    """A tool result as text when it is text, else the blocks with any payload dropped."""
    if isinstance(content, list):
        if all(isinstance(b, dict) and b.get("type") == "text" for b in content):
            return "\n".join(str(b.get("text", "")) for b in content)
        return [
            b if isinstance(b, dict) and b.get("type") == "text" else {"type": _kind(b)}
            for b in content
        ]
    return content


def _kind(block: Any) -> str:
    return str(block.get("type", "?")) if isinstance(block, dict) else type(block).__name__


def _message_usage(obj: dict[str, Any], state: dict[str, Any]) -> list[Event]:
    """A message's usage, the first time it is seen: claude repeats a message once per block.

    It counts as the run goes, so a cap can act on it; the result's total replaces it.
    """
    message = obj.get("message")
    usage = message.get("usage") if isinstance(message, dict) else None
    if not isinstance(usage, dict):
        return []
    seen: set[str] = state.setdefault("usage_ids", set())
    msg_id = message.get("id") if isinstance(message, dict) else None
    if isinstance(msg_id, str):
        if msg_id in seen:
            return []
        seen.add(msg_id)
    return [Usage({"usage": usage})]


class ClaudeEngine(CliEngine):
    name = "claude"
    binary = "claude"
    tool_prefix = "claude-code"
    loads_plugins = True
    skip_tools = ("mcp__metabot__",)
    supports_hooks = True

    def argv(self, job: EngineJob) -> list[str]:
        argv = [self.binary, "-p", "--output-format", "stream-json", "--verbose"]
        # No settings file loads, the repository's or the person's: one that is
        # committed could run hooks outside the gate or send the calls elsewhere.
        argv += ["--setting-sources", ""]
        argv += ["--permission-prompts", "none"]
        settings = carried_settings(self.env(), model_set=bool(job.model))
        if job.gate_hook:
            settings.update(hook_settings(job.gate_hook))
        if settings:
            argv += ["--settings", json.dumps(settings)]
        if job.model:
            argv += ["--model", job.model]
        if job.switchboard:
            argv += ["--mcp-config", json.dumps(mcp_servers(job.switchboard))]
        # The switchboard's server or none: a server from the person's config or
        # the project's .mcp.json would be gated by its tool names alone.
        argv += ["--strict-mcp-config"]
        for plugin_dir in job.plugin_dirs:
            argv += ["--plugin-dir", str(plugin_dir)]
        if job.read_only:
            argv += ["--permission-mode", "plan"]
        if job.resume:
            argv += ["--resume", job.resume]
        # Options that take several values would swallow a trailing prompt:
        # "--" ends them, and keeps a prompt that starts with "-" a prompt.
        return [*argv, "--", job.prompt]

    def parse_event(self, obj: dict[str, Any], state: dict[str, Any]) -> list[Event]:
        kind = obj.get("type")
        if kind == "system":
            if obj.get("subtype") == "init":
                return [Started(obj.get("session_id"), obj.get("model"))]
            return []  # hook chatter, token estimates: never the user's text
        if kind == "assistant":
            events: list[Event] = _message_usage(obj, state)
            for block in _blocks(obj):
                if block.get("type") == "tool_use":
                    args = block.get("input")
                    events.append(
                        ToolCall(
                            str(block.get("id", "")),
                            str(block.get("name", "")),
                            args if isinstance(args, dict) else {"value": args},
                        )
                    )
                elif block.get("type") == "text" and block.get("text"):
                    events.append(TextEvent(str(block["text"])))
            return events
        if kind == "user":
            return [
                ToolResult(
                    str(block.get("tool_use_id", "")),
                    _result_content(block.get("content")),
                    not block.get("is_error", False),
                )
                for block in _blocks(obj)
                if block.get("type") == "tool_result"
            ]
        if kind == "result":
            events = [
                Usage(
                    {
                        "usage": obj.get("usage") or {},
                        "total_cost_usd": obj.get("total_cost_usd"),
                        "num_turns": obj.get("num_turns"),
                    },
                    total=True,
                )
            ]
            if obj.get("is_error"):
                detail = obj.get("result") or ""
                events.append(ErrorEvent(f"claude: {obj.get('subtype')}: {detail}".rstrip(": ")))
            events.append(Final(str(obj.get("result") or ""), obj.get("session_id")))
            return events
        if kind in SILENT:
            return []
        text = obj.get("text")
        return [TextEvent(text)] if isinstance(text, str) and text else []
