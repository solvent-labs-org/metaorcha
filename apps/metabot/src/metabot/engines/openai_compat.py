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
from urllib.parse import urlsplit, urlunsplit

import httpx2

from metabot.config import Config, load_config
from metabot.engines.base import EngineJob
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
from metabot.paths import metabot_home, private_dir, safe_id, write_private
from metabot.scope_classes import ScopeClass
from metabot.shell import TIMEOUT_EXIT, run_shell, stop_group
from metabot.shell_policy import classify_shell
from metabot.spend import spend_of
from metabot.steps import ID_LIMIT, ascii_text
from metabot.toolcalls import (
    call_shaped,
    calls_in_fences,
    calls_in_text,
    leading_calls,
    parse_args,
    resolve_name,
    step_in_text,
)

MAX_TURNS = 20
HTTP_TIMEOUT_S = 300.0
SHELL_TIMEOUT_S = 120.0
READ_LIMIT = 200_000
OUTPUT_LIMIT = 20_000
LIST_LIMIT = 500
NUDGE_LIMIT = 2_000  # how much of the request is said again after each step, in JSON mode
CALL_ALONE_ASKS = 2  # how often a run asks a model to make a call it wrote inside its answer

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
    def __init__(self, message: str, status: int | None = None, unsent: bool = False) -> None:
        super().__init__(message)
        self.status = status  # the HTTP status the server answered with, if it answered
        self.unsent = unsent  # the model ran nothing: the request never went, or was refused


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

    A call never sees the variables named in *hidden*: the model's own key
    is for the request to the model, not for the commands it asks to run.
    """

    def __init__(self, hidden: frozenset[str] = frozenset()) -> None:
        self.left: set[int] = set()
        self.hidden = hidden

    async def run(self, command: str, cwd: Path) -> tuple[str, bool]:
        env = {k: v for k, v in os.environ.items() if k not in self.hidden} if self.hidden else None
        done = await run_shell(
            command, cwd, SHELL_TIMEOUT_S, limit=OUTPUT_LIMIT, merge_stderr=True, env=env
        )
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
    skip_tools: tuple[str, ...] = ()  # each call is recorded; the switchboard's step replaces it
    supports_hooks = False
    own_tools_gated = True  # MetaBot runs each of its tools, and only once the gate allows it
    read_only_mode = "no write tools offered"
    also_sees = "only what the run sends it"

    def __init__(
        self,
        base_url: str | None = None,
        model: str | None = None,
        key_env: str | None = None,
        config: Config | None = None,
        tool_mode: str | None = None,
        save_sessions: bool = True,
        endpoint: str | None = None,
    ) -> None:
        cfg = config if config is not None else load_config()
        self.base_url = (base_url or cfg.openai_base_url).rstrip("/")
        self._probed_url = cfg.openai_base_url.rstrip("/")  # where doctor --models probes
        self.endpoint = endpoint  # the endpoint's name in config.json; None for the default
        self.model = model or cfg.openai_model
        self.key_env = key_env if key_env is not None else cfg.openai_key_env
        # native: the model's own tool calls, repaired when it writes them as text;
        # json: one step at a time by schema. None: what the last probe found.
        self.tool_mode = tool_mode
        self.save_sessions = save_sessions

    @property
    def receipt_meta(self) -> dict[str, Any]:
        """Where the prompt went, for the receipt to sign: the server's address.

        A name or password in the address, a query and a fragment are left out,
        so no secret that rides in the address reaches the record.
        """
        parts = urlsplit(self.base_url)
        address = urlunsplit((parts.scheme, parts.netloc.rpartition("@")[2], parts.path, "", ""))
        where: dict[str, Any] = {"base_url": address.rstrip("/")}
        if self.endpoint is not None:
            where = {"name": self.endpoint, **where}
        return {"endpoint": where}

    def redaction_map(self) -> dict[str, str]:
        """``{VAR: key}`` for the model's key, so no copy of it reaches the record."""
        key = os.environ.get(self.key_env) if self.key_env else None
        return {self.key_env: key} if self.key_env and key else {}

    # ── HTTP ────────────────────────────────────────────────────────────────

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        """One chat request. Cancelling it closes the connection at once, so a
        cancelled run never waits on the model (and the server can stop)."""
        headers = {"Content-Type": "application/json"}
        if self.key_env:
            key = os.environ.get(self.key_env)
            if not key:
                raise EngineHTTPError(f"{self.key_env} is not set", unsent=True)
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
            # before the request went out: no connection, or a proxy that would not make one
            unsent = isinstance(
                exc, (httpx2.ConnectError, httpx2.ConnectTimeout, httpx2.ProxyError)
            )
            raise EngineHTTPError(
                f"cannot reach {self.base_url}: {reason}", unsent=unsent
            ) from None
        if response.status_code >= 400:
            body = response.text[:500]
            raise EngineHTTPError(
                f"{self.base_url} answered {response.status_code}: {body}",
                response.status_code,
                unsent=turned_away(response.status_code),
            )
        return json.loads(response.content.decode("utf-8"))

    # ── sessions (for resume) ───────────────────────────────────────────────

    @staticmethod
    def _session_path(session_id: str) -> Path:
        return private_dir(metabot_home() / "sessions") / f"{safe_id(session_id, 'session')}.json"

    def _load_session(self, session_id: str) -> list[dict[str, Any]]:
        try:
            data = json.loads(self._session_path(session_id).read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError):
            return []
        return data if isinstance(data, list) else []

    def _save_session(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        if not self.save_sessions:
            return
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
        for tool in board.tools(read_only=job.read_only) if board is not None else []:
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
            if not board.offers(name, read_only=job.read_only):  # a write it was never offered
                return f"no tool named {name}", False
            result = await board.call(name, args, call_id)
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
        shells = _Shells(frozenset([self.key_env]) if self.key_env else frozenset())
        try:
            async with contextlib.aclosing(self._loop(job, board, approve, shells)) as events:
                async for event in events:
                    yield event
        finally:
            await shells.close()  # nothing a run started outlives it

    def _mode(self, model: str) -> str:
        if self.tool_mode is not None:
            return self.tool_mode
        from metabot.models import cached_mode

        return "json" if cached_mode(self.base_url, model) == "json" else "native"

    def _probe_hint(self, model: str) -> str:
        """For a model never probed: the command that finds how it calls tools.

        ``doctor --models`` probes the default server, and only on this machine,
        so a model on any other server is not sent to it.
        """
        from metabot.models import cached_mode
        from metabot.prices import is_local

        if self.tool_mode is not None or cached_mode(self.base_url, model) is not None:
            return ""
        if self.base_url != self._probed_url or not is_local(self.base_url):
            return ""
        return f". metabot doctor --models {model} checks how it calls tools"

    async def _loop(
        self, job: EngineJob, board: Any, approve: Approve, shells: _Shells
    ) -> AsyncGenerator[Event, None]:
        model = job.model or self.model
        mode = self._mode(model)
        session_id = job.resume or str(uuid.uuid4())
        tools = self._tools(job, board)
        names = [t["function"]["name"] for t in tools]
        messages = self._load_session(session_id) if job.resume else []
        if not messages:
            system = SYSTEM.format(cwd=job.cwd)
            if mode == "json":
                system += _json_prompt(tools)
            messages = [{"role": "system", "content": system}]
        messages.append({"role": "user", "content": job.prompt})
        yield Started(session_id, model)
        last_text = ""
        schema = mode == "json"  # dropped once, if the server refuses it
        steps = 0
        asked_alone = 0
        used: set[str] = set()  # the call ids of this run's record
        for turn in range(MAX_TURNS):
            payload: dict[str, Any] = {"model": model, "messages": messages}
            if mode != "json":
                payload["tools"] = tools
            elif schema:
                payload["response_format"] = _step_format(tools)
            # A request that went out and never said what it spent leaves the cost unknown.
            try:
                response = await self._post(payload)
            except EngineHTTPError as exc:
                if schema and exc.status == 400:
                    schema = False
                    yield Notice(
                        f"openai: {self.base_url} refused the response schema; asking without it"
                    )
                    continue
                yield ErrorEvent(f"openai: {exc}", exc.unsent, lost=not exc.unsent)
                yield Final(last_text, session_id)
                self._save_session(session_id, messages)
                return
            except (ValueError, RecursionError) as exc:  # an answer that does not read
                yield ErrorEvent(f"openai: {exc}", lost=True)
                yield Final(last_text, session_id)
                self._save_session(session_id, messages)
                return
            usage = response.get("usage") if isinstance(response, dict) else None
            usage = usage if isinstance(usage, dict) else {}
            yield Usage(usage, lost=not spend_of(usage).tokens)  # every turn's, before its calls
            try:
                message = _message(response)
            except (KeyError, IndexError, TypeError) as exc:
                yield ErrorEvent(f"openai: {exc}")
                yield Final(last_text, session_id)
                self._save_session(session_id, messages)
                return
            content = message.get("content") or ""
            if mode == "json":
                step = step_in_text(content, names)
                messages.append({"role": "assistant", "content": content})
                if step is None or step[0] == "answer":
                    if step is None:
                        yield ErrorEvent(
                            "openai: the model's step did not read as a tool or an answer"
                        )
                    text = content if step is None else _answer_text(step[1])
                    if text:
                        yield TextEvent(text)
                    yield Final(text, session_id)
                    self._save_session(session_id, messages)
                    return
                call_id, steps = f"step-{steps}", steps + 1
                output, ok = None, False
                async for event in self._one_call(
                    call_id, step[0], step[1], job, board, approve, shells
                ):
                    if isinstance(event, ToolResult):
                        output, ok = event.output, event.ok
                    yield event
                failed = "" if ok else "\n(this call did not succeed)"
                result = f"Result of {step[0]}:\n{str(output)[:OUTPUT_LIMIT]}{failed}"
                messages.append({"role": "user", "content": result + _nudge(job.prompt)})
                continue
            calls = message.get("tool_calls") or []
            if not calls and content:
                read = calls_in_text(content, names)
                broke = False
                if read is None and (lead := leading_calls(content, names)):
                    read, broke = lead, True
                if read:
                    calls = [
                        {
                            "id": f"text-{turn}-{i}",
                            "type": "function",
                            "function": {"name": tool, "arguments": json.dumps(args)},
                        }
                        for i, (tool, args, _asked) in enumerate(read)
                    ]
                    content = ""
                    rest = "; the call after them broke off and was not made" if broke else ""
                    yield Notice(
                        f"openai: read {_count(len(calls), 'tool call')} the model wrote as text"
                        + rest
                    )
                elif call_shaped(content):
                    messages.append({"role": "assistant", "content": content})
                    yield ErrorEvent(
                        "openai: the model answered with a tool call MetaBot could not read"
                        + self._probe_hint(model)
                    )
                    yield Final(content, session_id)
                    self._save_session(session_id, messages)
                    return
                elif fenced := calls_in_fences(content, names):
                    # A call inside a longer answer is never made; the model is asked once
                    # more for the call alone, and a run that keeps doing it fails.
                    messages.append({"role": "assistant", "content": content})
                    if asked_alone >= CALL_ALONE_ASKS:
                        yield ErrorEvent(
                            "openai: the model kept writing tool calls inside its answer; "
                            "none was made" + self._probe_hint(model)
                        )
                        yield Final(content, session_id)
                        self._save_session(session_id, messages)
                        return
                    asked_alone += 1
                    messages.append({"role": "user", "content": _call_alone(fenced)})
                    yield Notice(
                        f"openai: the model wrote {_count(len(fenced), 'tool call')} inside its "
                        "answer; none was made, and it was asked for the call alone"
                    )
                    continue
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
                asked = str(fn.get("name") or "")
                given = str(call.get("id") or f"call-{turn}-{i}")
                call_id = _unique(given, used)  # in the record; the model hears its own
                name = asked
                if asked not in names:
                    name = resolve_name(asked, names) or asked
                    if name != asked:
                        yield Notice(f"openai: the model asked for {asked!r}; ran {name}")
                args = parse_args(fn.get("arguments"))
                output = None
                if args is None:
                    yield ToolCall(call_id, name, {"raw": str(fn.get("arguments"))[:2000]})
                    output = "error: bad arguments: they are not a JSON object"
                    yield ToolResult(call_id, output, False)
                else:
                    async for event in self._one_call(
                        call_id, name, args, job, board, approve, shells
                    ):
                        if isinstance(event, ToolResult):
                            output = event.output
                        yield event
                messages.append(
                    {"role": "tool", "tool_call_id": given, "content": str(output)[:OUTPUT_LIMIT]}
                )
        yield ErrorEvent(f"openai: stopped after {MAX_TURNS} turns without an answer")
        yield Final(last_text, session_id)
        self._save_session(session_id, messages)

    async def _one_call(
        self, call_id: str, name: str, args: dict[str, Any], job: EngineJob, board: Any,
        approve: Approve, shells: _Shells,
    ) -> AsyncGenerator[Event, None]:  # fmt: skip
        """A call as two events: what was asked, recorded first, then what came of it.

        A call the switchboard takes is recorded again by it, under the same id, in place
        of the call asked: once made, with what its connection saw; cut off once sent, as
        one that may have run.
        """
        yield ToolCall(call_id, name, args)
        output, ok = await self._call(call_id, name, args, job, board, approve, shells)
        yield ToolResult(call_id, output, ok)


def _message(response: Any) -> dict[str, Any]:
    """The answer's message, or TypeError when its shape is not one a chat answer has."""
    message = response["choices"][0]["message"]
    if not isinstance(message, dict):
        raise TypeError("the answer's message is not an object")
    if not isinstance(message.get("content") or "", str):
        raise TypeError("the answer's content is not text")
    calls = message.get("tool_calls") or []
    if not isinstance(calls, list) or not all(
        isinstance(call, dict) and isinstance(call.get("function") or {}, dict) for call in calls
    ):
        raise TypeError("the answer's tool calls are not objects")
    return message


def _unique(given: str, used: set[str]) -> str:
    """A call's id in the run's record: the one the model gave, as the record keeps it,
    printable and cut at its limit, or, when the run has that one already, numbered, so
    each call, its approval and its step stay its own."""
    call_id, n = ascii_text(given), 1
    while call_id in used:
        n += 1
        call_id = ascii_text(given, limit=ID_LIMIT - len(f"-{n}")) + f"-{n}"
    used.add(call_id)
    return call_id


def turned_away(status: int) -> bool:
    """A status that turns a request away before any model runs it: one in the 400s other
    than 408. A 5xx or a 408 may come once the model has run, as a gateway's timeout does."""
    return 400 <= status < 500 and status != 408


def _nudge(prompt: str) -> str:
    """After a step's result: what was asked, so a small model keeps to it."""
    return (
        f"\n\nThe request: {prompt[:NUDGE_LIMIT]}\n"
        "Reply with the next step, or with answer as soon as you can answer the request."
    )


def _call_alone(tools: list[str]) -> str:
    """What a model hears when it wrote a tool call inside a longer answer."""
    named = ", ".join(dict.fromkeys(tools))
    return (
        f"Your answer held a tool call inside its text ({named}), so it was not made. "
        "To call a tool, reply with the call alone: one JSON object, "
        '{"name": "<tool>", "arguments": {...}}, and no other text. '
        "When the request is done, reply with your answer and no tool call."
    )


def _count(n: int, thing: str) -> str:
    return f"{n} {thing}" if n == 1 else f"{n} {thing}s"


def _answer_text(args: dict[str, Any]) -> str:
    """The answer in a constrained step: its ``text``, else the first text it holds."""
    text = args.get("text")
    if isinstance(text, str):
        return text
    return next((v for v in args.values() if isinstance(v, str)), "")


def _json_prompt(tools: list[dict[str, Any]]) -> str:
    """The tools, for a model that answers one JSON step at a time."""
    lines = [
        "",
        "",
        'Reply with one JSON object, the next step: {"tool": NAME, "arguments": {...}}.',
        "The tools:",
    ]
    for tool in tools:
        fn = tool["function"]
        keys = ", ".join((fn.get("parameters") or {}).get("properties") or {})
        lines.append(f"- {fn['name']} {{{keys}}}: {fn.get('description') or ''}".rstrip())
    lines.append("- answer {text}: your final answer to the user.")
    return "\n".join(lines)


_ANSWER = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}


def _step_format(tools: list[dict[str, Any]]) -> dict[str, Any]:
    """A ``response_format`` that holds the model to one step: a tool, or the answer."""

    def branch(name: str, params: dict[str, Any]) -> dict[str, Any]:
        arguments = {"type": "object", **params, "additionalProperties": False}
        return {
            "type": "object",
            "properties": {"tool": {"const": name}, "arguments": arguments},
            "required": ["tool", "arguments"],
            "additionalProperties": False,
        }

    branches = [branch(t["function"]["name"], t["function"].get("parameters") or {}) for t in tools]
    branches.append(branch("answer", _ANSWER))
    return {
        "type": "json_schema",
        "json_schema": {"name": "step", "strict": True, "schema": {"anyOf": branches}},
    }
