"""The ``openai`` engine: any chat-completions endpoint, local by default.

MetaBot runs the tool loop itself, so every tool goes through its gate: the
built-ins (read, list, write and a shell, confined to the job directory for
paths) and the switchboard's connections. A local model server is the
default, so this path costs nothing and nothing leaves the machine; a hosted
endpoint works the same way with a key read from an environment variable at
call time and never stored.
"""

from __future__ import annotations

import contextlib
import json
import os
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx2

from metabot.config import Config, load_config
from metabot.engines.base import EngineJob
from metabot.events import ErrorEvent, Event, Final, Started, TextEvent, ToolCall, ToolResult, Usage
from metabot.paths import metabot_home, private_dir, safe_id, write_private
from metabot.scope_classes import ScopeClass
from metabot.shell import TIMEOUT_EXIT, run_shell, stop_group
from metabot.shell_policy import classify_shell

MAX_TURNS = 20
HTTP_TIMEOUT_S = 300.0
SHELL_TIMEOUT_S = 120.0
READ_LIMIT = 200_000
OUTPUT_LIMIT = 20_000
LIST_LIMIT = 500
SWITCHBOARD_PREFIX = "mcp__metabot__"

SYSTEM = (
    "You are MetaBot, a local assistant working in the directory {cwd}. "
    "Use the tools to look at and change files there. Every tool call is recorded, "
    "and calls that change things may need the user's approval. Answer briefly."
)

Approve = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


def _fn(name: str, description: str, props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": props, "required": required},
        },
    }


_PATH = {"path": {"type": "string", "description": "a path inside the job directory"}}
BUILTINS = {
    "read_file": _fn("read_file", "Read a text file.", _PATH, ["path"]),
    "list_dir": _fn("list_dir", "List a directory.", _PATH, ["path"]),
    "write_file": _fn(
        "write_file",
        "Write a text file (creates or replaces it).",
        {**_PATH, "content": {"type": "string"}},
        ["path", "content"],
    ),
    "shell": _fn(
        "shell",
        "Run a shell command in the job directory.",
        {"command": {"type": "string"}},
        ["command"],
    ),
}
WRITERS = frozenset({"write_file", "shell"})


class EngineHTTPError(RuntimeError):
    pass


def _inside(cwd: Path, raw: Any) -> Path:
    if not isinstance(raw, str) or not raw:
        raise ValueError("a path is required")
    root = cwd.resolve()
    target = (root / raw).resolve()
    if target != root and root not in target.parents:
        raise ValueError(f"{raw} is outside the job directory")
    return target


class _Shells:
    """The shell calls of one run.

    Each call runs in its own process group (``metabot.shell``). A call that
    times out or is cancelled is stopped with everything it started, and
    whatever a call left running in the background is stopped when the run
    ends, as the command-line engines' process groups are.
    """

    def __init__(self) -> None:
        self.left: set[int] = set()

    async def run(self, command: str, cwd: Path) -> tuple[str, bool]:
        done = await run_shell(command, cwd, SHELL_TIMEOUT_S, limit=OUTPUT_LIMIT, merge_stderr=True)
        if done.timed_out:
            said = f"timed out after {SHELL_TIMEOUT_S:.0f} s"
            return json.dumps({"exit_code": TIMEOUT_EXIT, "timed_out": True, "output": said}), False
        if done.left_running:
            self.left.add(done.pgid)
        # JSON with the exit code, so a declared exit_zero can judge the step
        result = {"exit_code": done.exit_code, "output": done.stdout}
        return json.dumps(result, ensure_ascii=False), done.exit_code == 0

    async def close(self) -> None:
        for pgid in sorted(self.left):
            await stop_group(pgid)
        self.left.clear()


class OpenAIEngine:
    name = "openai"
    tool_prefix = "metabot"
    skip_tools = (SWITCHBOARD_PREFIX,)
    supports_hooks = False
    own_tools_gated = True  # MetaBot runs each of its tools, and only once the gate allows it

    def __init__(
        self,
        base_url: str | None = None,
        model: str | None = None,
        key_env: str | None = None,
        config: Config | None = None,
    ) -> None:
        cfg = config if config is not None else load_config()
        self.base_url = (base_url or cfg.openai_base_url).rstrip("/")
        self.model = model or cfg.openai_model
        self.key_env = key_env if key_env is not None else cfg.openai_key_env

    # ── HTTP ────────────────────────────────────────────────────────────────

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        """One chat request. Cancelling it closes the connection at once, so a
        cancelled run never waits on the model (and the server can stop)."""
        headers = {"Content-Type": "application/json"}
        if self.key_env:
            key = os.environ.get(self.key_env)
            if not key:
                raise EngineHTTPError(f"{self.key_env} is not set")
            headers["Authorization"] = f"Bearer {key}"
        url = f"{self.base_url}/chat/completions"
        try:
            async with httpx2.AsyncClient(timeout=HTTP_TIMEOUT_S) as client:
                response = await client.post(
                    url, content=json.dumps(payload).encode("utf-8"), headers=headers
                )
        except httpx2.ReadTimeout:  # it took the call: a local model may still be loading
            raise EngineHTTPError(
                f"{self.base_url} gave no answer in {HTTP_TIMEOUT_S:g} seconds; a local model "
                "may still be loading, and a smaller one answers sooner"
            ) from None
        except httpx2.HTTPError as exc:
            reason = str(exc) or type(exc).__name__
            raise EngineHTTPError(f"cannot reach {self.base_url}: {reason}") from None
        if response.status_code >= 400:
            body = response.text[:500]
            raise EngineHTTPError(f"{self.base_url} answered {response.status_code}: {body}")
        return json.loads(response.content.decode("utf-8"))

    # ── sessions (for resume) ───────────────────────────────────────────────

    @staticmethod
    def _session_path(session_id: str) -> Path:
        return private_dir(metabot_home() / "sessions") / f"{safe_id(session_id, 'session')}.json"

    def _load_session(self, session_id: str) -> list[dict[str, Any]]:
        try:
            data = json.loads(self._session_path(session_id).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return data if isinstance(data, list) else []

    def _save_session(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        write_private(self._session_path(session_id), json.dumps(messages) + "\n")

    # ── built-in tools ──────────────────────────────────────────────────────

    async def _builtin(
        self, name: str, args: dict[str, Any], cwd: Path, shells: _Shells
    ) -> tuple[Any, bool]:
        if name == "read_file":
            path = _inside(cwd, args.get("path"))
            data = path.read_bytes()[:READ_LIMIT]
            if b"\0" in data:
                return "binary file", False
            return data.decode("utf-8", errors="replace"), True
        if name == "list_dir":
            path = _inside(cwd, args.get("path") or ".")
            entries = sorted(p.name + ("/" if p.is_dir() else "") for p in path.iterdir())
            return "\n".join(entries[:LIST_LIMIT]), True
        if name == "write_file":
            path = _inside(cwd, args.get("path"))
            content = args.get("content")
            if not isinstance(content, str):
                return "content must be text", False
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            return f"wrote {len(content)} characters to {path.relative_to(cwd.resolve())}", True
        if name == "shell":
            command = args.get("command")
            if not isinstance(command, str) or not command.strip():
                return "a command is required", False
            return await shells.run(command, cwd)
        return f"no tool named {name}", False

    def _class(self, name: str, args: dict[str, Any]) -> ScopeClass:
        if name in ("read_file", "list_dir"):
            return ScopeClass.READ
        if name == "write_file":
            return ScopeClass.WRITE
        if name == "shell":
            return classify_shell(str(args.get("command") or ""))
        return ScopeClass.DESTRUCTIVE

    # ── the loop ────────────────────────────────────────────────────────────

    def _tools(self, job: EngineJob, board: Any) -> list[dict[str, Any]]:
        tools = [t for n, t in BUILTINS.items() if not (job.read_only and n in WRITERS)]
        for tool in board.tools() if board is not None else []:
            schema = tool.input_schema or {"type": "object", "properties": {}}
            function = {
                "name": tool.name,
                "description": tool.description or "",
                "parameters": schema,
            }
            tools.append({"type": "function", "function": function})
        return tools

    async def _call(
        self, call_id: str, name: str, args: dict[str, Any], job: EngineJob, board: Any,
        approve: Approve, shells: _Shells,
    ) -> tuple[Any, bool]:  # fmt: skip
        """(output, ok) for one call; the switchboard records its own calls."""
        if board is not None and board.has(name):
            result = await board.call(name, args)
            text = "\n".join(getattr(c, "text", "") for c in result.content) or "(no output)"
            return text, not result.is_error
        if name not in BUILTINS or (job.read_only and name in WRITERS):
            return f"no tool named {name}", False
        cls = self._class(name, args)
        if cls is not ScopeClass.READ:
            reply = await approve(
                {
                    "op": "approve",
                    "call_id": call_id,
                    "tool": f"{self.tool_prefix}/{name}",
                    "cls": cls.label,
                    "preview": json.dumps(args, ensure_ascii=False)[:2000],
                }
            )
            if not reply.get("allow"):
                return f"not allowed: {reply.get('reason') or 'declined'}", False
        try:
            return await self._builtin(name, args, job.cwd, shells)
        except (OSError, ValueError) as exc:
            return f"error: {exc}", False

    async def run(
        self, job: EngineJob, *, board: Any = None, approve: Approve
    ) -> AsyncGenerator[Event, None]:
        shells = _Shells()
        try:
            async with contextlib.aclosing(self._loop(job, board, approve, shells)) as events:
                async for event in events:
                    yield event
        finally:
            await shells.close()  # nothing a run started outlives it

    async def _loop(
        self, job: EngineJob, board: Any, approve: Approve, shells: _Shells
    ) -> AsyncGenerator[Event, None]:
        model = job.model or self.model
        session_id = job.resume or str(uuid.uuid4())
        messages = self._load_session(session_id) if job.resume else []
        if not messages:
            messages = [{"role": "system", "content": SYSTEM.format(cwd=job.cwd)}]
        messages.append({"role": "user", "content": job.prompt})
        yield Started(session_id, model)
        tools = self._tools(job, board)
        last_text = ""
        for turn in range(MAX_TURNS):
            payload = {"model": model, "messages": messages, "tools": tools}
            try:
                response = await self._post(payload)
                message = response["choices"][0]["message"]
            except (EngineHTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
                yield ErrorEvent(f"openai: {exc}")
                yield Final(last_text, session_id)
                self._save_session(session_id, messages)
                return
            yield Usage(response.get("usage") or {})  # every turn's, before its calls run
            content = message.get("content") or ""
            calls = message.get("tool_calls") or []
            messages.append(
                {
                    "role": "assistant",
                    "content": content,
                    **({"tool_calls": calls} if calls else {}),
                }
            )
            if content:
                last_text = content
                yield TextEvent(content)
            if not calls:
                yield Final(content, session_id)
                self._save_session(session_id, messages)
                return
            for i, call in enumerate(calls):
                fn = call.get("function") or {}
                name = str(fn.get("name") or "")
                call_id = str(call.get("id") or f"call-{turn}-{i}")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                    if not isinstance(args, dict):
                        raise ValueError("arguments are not an object")
                except ValueError as exc:
                    yield ToolCall(call_id, name, {"raw": str(fn.get("arguments"))[:2000]})
                    output, ok = f"error: bad arguments: {exc}", False
                    yield ToolResult(call_id, output, ok)
                else:
                    recorded = f"{SWITCHBOARD_PREFIX}{name}" if board and board.has(name) else name
                    yield ToolCall(call_id, recorded, args)
                    output, ok = await self._call(call_id, name, args, job, board, approve, shells)
                    yield ToolResult(call_id, output, ok)
                messages.append(
                    {"role": "tool", "tool_call_id": call_id, "content": str(output)[:OUTPUT_LIMIT]}
                )
        yield ErrorEvent(f"openai: stopped after {MAX_TURNS} turns without an answer")
        yield Final(last_text, session_id)
        self._save_session(session_id, messages)
