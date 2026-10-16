"""The switchboard: one MCP server in front of every connection.

The engine sees one server, ``metabot``. Its tools are the user's
connections' tools, named ``<connection>__<tool>`` and labelled with their
class. Every call is classified with the one classifier, sent to the
decider unless it only reads, forwarded only when allowed, and redacted
with the connection's token before it is recorded or returned, so the
engine never sees a token even if a server echoes one.

A browser connection's calls are classed by the origin each acts on
(``origins``), and the board remembers where each one's page was last sent.

A revoked connection fails closed: the call is refused before any request
reaches its server, even when it was revoked in the middle of a run.

For an engine that cannot load a plugin, the switchboard also serves the
enabled plugins' skills as two read tools, ``skills__list`` and
``skills__load``. A run without connections still gets them, and then no
connection is opened.

What a server prints on stderr reaches the run's log through a pipe that
replaces every token first (``serverlog``).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import secrets
import signal
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

import anyio
from emerge.preimage import output_preimage, redact_credentials
from mcp_types import CallToolResult, TextContent, Tool

from metabot.connections import Connection, ConnectionStore
from metabot.origins import (
    acts_on,
    browser_class,
    call_urls,
    moves_unsaid,
    origin_allowed,
    origin_of,
)
from metabot.paths import write_private
from metabot.plugins import skill_description
from metabot.scope_classes import ScopeClass, resolve_scope_class
from metabot.serverlog import RedactedLog
from metabot.upstream import Upstream

TOOL_NAME_RE = re.compile(r"[^A-Za-z0-9_-]")
TOOL_NAME_LIMIT = 64
PREVIEW_LIMIT = 2000
OPEN_TIMEOUT_S = 30.0
LINE_LIMIT = 64 * 1024 * 1024  # the longest message an engine may send
SKILLS_LIST = "skills__list"
SKILLS_LOAD = "skills__load"
SKILLS_FILE = "skills.json"  # in the run's folder, for ``metabot mcp serve``
SKILL_READ_LIMIT = 200_000

Approve = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
Record = Callable[[dict[str, Any]], Awaitable[None]]


def declared_class(tool: Tool) -> str | None:
    """A server's explicit hints can only tighten the class, never loosen it."""
    hints = tool.annotations
    if hints is None:
        return None
    if hints.destructive_hint is True:
        return "destructive"
    if hints.read_only_hint is False:
        return "write"
    return None


def _tightened(cls: ScopeClass, *more: str | None) -> ScopeClass:
    for each in more:
        parsed = ScopeClass.parse(each)
        if parsed is not None:
            cls = max(cls, parsed)
    return cls


def classify(
    conn: Connection,
    tool: Tool,
    args: Mapping[str, Any] | None = None,
    last_origin: str | None = None,
) -> ScopeClass:
    """A call's class: by the tool's name, tightened by its hints and the person's settings.

    A browser connection's call is classed by the origin it acts on first: the
    urls in ``args``, else ``last_origin``, where its page was last sent.
    """
    declared = declared_class(tool)
    override = conn.overrides.get(tool.name)
    if conn.origins is None:
        return resolve_scope_class(
            tool.name,
            manifest_classes={tool.name: declared} if declared else None,
            override=override,
        )
    allowed = all(origin_allowed(o, conn.origins) for o in acts_on(args, last_origin))
    return _tightened(browser_class(tool.name, allowed), declared, override)


def class_label(conn: Connection, tool: Tool) -> str:
    """A tool's class as the engine and the person are shown it, before any call."""
    if conn.origins is None:
        return classify(conn, tool).label
    on = browser_class(tool.name, True)
    on = _tightened(on, declared_class(tool), conn.overrides.get(tool.name))
    if on is ScopeClass.DESTRUCTIVE:
        return on.label
    return f"{on.label} on {', '.join(conn.origins)}; destructive elsewhere"


def tool_name(conn_name: str, capability: str, taken: set[str]) -> str:
    name = TOOL_NAME_RE.sub("_", f"{conn_name}__{capability}")[:TOOL_NAME_LIMIT]
    if name in taken:
        digest = hashlib.sha256(f"{conn_name}/{capability}".encode()).hexdigest()[:6]
        name = f"{name[: TOOL_NAME_LIMIT - 7]}_{digest}"
    return name


def _error(text: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=text)], is_error=True)


def write_skills(run_dir: Path, skills: Mapping[str, Path]) -> None:
    data = {name: str(path) for name, path in skills.items()}
    write_private(run_dir / SKILLS_FILE, json.dumps(data, indent=2) + "\n")


def read_skills(run_dir: Path) -> dict[str, Path]:
    path = run_dir / SKILLS_FILE
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {str(k): Path(v) for k, v in data.items()} if isinstance(data, dict) else {}


@dataclass
class _Route:
    conn: Connection
    capability: str
    upstream: Upstream
    tool: Tool


class Switchboard:
    def __init__(
        self,
        store: ConnectionStore,
        *,
        approve: Approve,
        record: Record,
        errlog: TextIO | None = None,
        skills: Mapping[str, Path] | None = None,
        connections: bool = True,
        only: Collection[str] | None = None,
    ) -> None:
        self.store = store
        self.skills = dict(skills or {})
        self.connections = connections
        self.only = None if only is None else frozenset(only)  # connection ids; None is all
        self.approve = approve
        self.record = record
        self.errlog = errlog or sys.stderr
        self.routes: dict[str, _Route] = {}
        self.skipped: dict[str, str] = {}
        self._stack = contextlib.AsyncExitStack()
        self._log: RedactedLog | None = None
        self._prefix = f"sw-{secrets.token_hex(3)}"
        self._count = 0
        self._pages: dict[str, str | None] = {}  # a browser connection: its page's origin

    async def _open(self, conn: Connection) -> tuple[Upstream, list[Tool]]:
        assert self._log is not None
        upstream = Upstream(
            conn, self.store.secret(conn), errlog=self._log.writer, init_timeout=OPEN_TIMEOUT_S
        )
        # Entered on the board's own stack, in this task: an MCP client's task
        # group must be left by the task that entered it.
        await self._stack.enter_async_context(upstream)
        with anyio.fail_after(OPEN_TIMEOUT_S):
            tools = await upstream.list_tools()
        return upstream, tools

    async def __aenter__(self) -> Switchboard:
        await self._stack.__aenter__()
        try:
            secrets_by_var = self.store.redaction_map()
            self._log = RedactedLog(self.errlog, secrets_by_var)
            self._stack.callback(self._log.close)  # runs after every server has closed
            taken: set[str] = {SKILLS_LIST, SKILLS_LOAD}
            for conn in self.store.active() if self.connections else []:
                if self.only is not None and conn.id not in self.only:
                    continue
                try:
                    upstream, tools = await self._open(conn)
                except Exception as exc:  # one broken server must not hide the others
                    said = redact_credentials(str(exc), secrets_by_var)
                    self.skipped[conn.name] = f"{type(exc).__name__}: {said}"
                    self._log.note(f"metabot: connection {conn.name} skipped: {said}")
                    continue
                for tool in tools:
                    name = tool_name(conn.name, tool.name, taken)
                    taken.add(name)
                    self.routes[name] = _Route(conn, tool.name, upstream, tool)
        except BaseException:  # interrupted while opening: close what is already open
            await self._stack.aclose()
            raise
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self._stack.aclose()

    def has(self, name: str) -> bool:
        return name in self.routes or (bool(self.skills) and name in (SKILLS_LIST, SKILLS_LOAD))

    def _skill_tools(self) -> list[Tool]:
        if not self.skills:
            return []
        load = {
            "type": "object",
            "properties": {
                "name": {"type": "string", "enum": sorted(self.skills)},
                "file": {"type": "string", "description": "a path inside the skill's folder"},
            },
            "required": ["name"],
        }
        return [
            Tool(
                name=SKILLS_LIST,
                description="[read] The skills you can load, each with what it is for.",
                input_schema={"type": "object", "properties": {}},
            ),
            Tool(
                name=SKILLS_LOAD,
                description=(
                    "[read] A skill's instructions: load one before you follow it. With file, "
                    "a file inside the skill's folder that its instructions name."
                ),
                input_schema=load,
            ),
        ]

    def _skill(self, name: str, args: dict[str, Any]) -> tuple[str, bool]:
        if name == SKILLS_LIST:
            lines = []
            for skill_name, skill_path in sorted(self.skills.items()):
                about = skill_description(skill_path)
                lines.append(f"{skill_name}: {about}" if about else skill_name)
            return "\n".join(lines), True
        skill = args.get("name")
        path = self.skills.get(skill) if isinstance(skill, str) else None
        if path is None:
            return f"no skill named {skill!r}; {SKILLS_LIST} names them", False
        file = args.get("file")
        target = path
        if file:
            folder = path.parent.resolve()
            target = (folder / str(file)).resolve()
            if not target.is_relative_to(folder):
                return f"{file} is outside the skill's folder", False
        try:
            data = target.read_bytes()[:SKILL_READ_LIMIT]
        except OSError as exc:
            return f"cannot read {file or skill}: {exc.strerror or type(exc).__name__}", False
        if b"\0" in data:
            return f"{file} is not a text file", False
        return data.decode("utf-8", errors="replace"), True

    async def _skill_call(self, name: str, args: dict[str, Any]) -> CallToolResult:
        started = time.monotonic()
        call_id = self._call_id()
        text, ok = self._skill(name, args)
        result = CallToolResult(content=[TextContent(type="text", text=text)], is_error=not ok)
        await self.record(
            {
                "call_id": call_id,
                "tool": f"metabot/{name.replace('__', '_')}",
                "args": args,
                "output": result.model_dump(mode="json", by_alias=True, exclude_none=True),
                "success": ok,
                "latency_ms": max(0, int((time.monotonic() - started) * 1000)),
            }
        )
        return result

    def tools(self) -> list[Tool]:
        listed = self._skill_tools()
        for name, route in self.routes.items():
            label = class_label(route.conn, route.tool)
            description = f"[{label}] {route.tool.description or ''}".strip()
            listed.append(
                Tool(name=name, description=description, input_schema=route.tool.input_schema)
            )
        return listed

    def _where(self, conn: Connection) -> str:
        """Where a browser connection's page was last sent, for the person asked."""
        if conn.id not in self._pages:
            return "the page has not been sent anywhere yet"
        origin = self._pages[conn.id]
        return f"the page was last sent to {origin}" if origin else "the page's origin is not known"

    def _sent(self, conn: Connection, capability: str, args: Mapping[str, Any], ok: bool) -> None:
        """After a browser call, where its page is now, when that is known."""
        if conn.origins is None:
            return
        urls = call_urls(args)
        if urls:
            self._pages[conn.id] = origin_of(urls[0]) if ok and len(urls) == 1 else None
        elif moves_unsaid(capability):
            self._pages[conn.id] = None  # back, forward or another tab: it does not say where

    def _call_id(self) -> str:
        self._count += 1
        return f"{self._prefix}-{self._count}"

    async def _record(
        self, call_id: str, conn: Connection, capability: str, args: Any, output: Any, ok: bool,
        started: float,
    ) -> None:  # fmt: skip
        await self.record(
            {
                "call_id": call_id,
                "tool": f"{conn.id}#{capability}",
                "args": args,
                "output": output,
                "success": ok,
                "latency_ms": max(0, int((time.monotonic() - started) * 1000)),
            }
        )

    async def call(self, name: str, args: dict[str, Any]) -> CallToolResult:
        if self.skills and name in (SKILLS_LIST, SKILLS_LOAD):
            return await self._skill_call(name, args)
        route = self.routes.get(name)
        if route is None:
            return _error(f"metabot: no tool named {name}")
        started = time.monotonic()
        call_id = self._call_id()
        try:
            conn = self.store.get(route.conn.id)  # read fresh: a revoke mid-run counts
        except KeyError:
            conn = route.conn
            conn.state = "removed"
        secret = self.store.secret(conn)
        redact = {conn.redacted_as: secret} if secret else {}
        clean_args = redact_credentials(output_preimage(args), redact) if redact else args
        if not conn.active:
            reason = f"connection {conn.name} is {conn.state}"
            await self._record(
                call_id, conn, route.capability, clean_args, {"error": reason}, False, started
            )
            return _error(f"metabot: {reason}")
        cls = classify(conn, route.tool, args, self._pages.get(conn.id))
        if cls is not ScopeClass.READ:
            preview = f"{conn.name}.{route.capability} {json.dumps(clean_args, ensure_ascii=False)}"
            if conn.origins is not None and not call_urls(args):
                where = f" ({self._where(conn)})"
                preview = preview[: PREVIEW_LIMIT - len(where)] + where
            reply = await self.approve(
                {
                    "op": "approve",
                    "call_id": call_id,
                    "tool": f"{conn.id}#{route.capability}",
                    "cls": cls.label,
                    "preview": preview[:PREVIEW_LIMIT],
                }
            )
            if not reply.get("allow"):
                reason = str(reply.get("reason") or "declined")
                await self._record(
                    call_id, conn, route.capability, clean_args, {"error": reason}, False, started
                )
                return _error(f"metabot: {cls.label} call not allowed ({reason})")
        try:
            result = await route.upstream.call_tool(route.capability, args)
        except Exception as exc:  # a failed call is an error result, not a crash
            result = _error(f"metabot: the connection failed: {type(exc).__name__}: {exc}")
        self._sent(conn, route.capability, args, not result.is_error)
        dumped = result.model_dump(mode="json", by_alias=True, exclude_none=True)
        clean = redact_credentials(output_preimage(dumped), redact) if redact else dumped
        await self._record(
            call_id, conn, route.capability, clean_args, clean, not result.is_error, started
        )
        return CallToolResult.model_validate(clean)


async def serve(
    run_dir: Path,
    control_path: Path,
    connections: bool = True,
    only: Collection[str] | None = None,
) -> None:
    """The stdio entry the engine starts: ``metabot mcp serve``."""
    from mcp.server.lowlevel import Server
    from mcp.server.stdio import stdio_server
    from mcp_types import ListToolsResult

    from metabot import __version__, control

    log_path = run_dir / "switchboard.log"
    if not log_path.exists():
        write_private(log_path, "")

    async def approve(msg: dict[str, Any]) -> dict[str, Any]:
        return await control.request(control_path, msg)

    async def record(step: dict[str, Any]) -> None:
        await control.request(control_path, {"op": "step", "step": step})

    with open(log_path, "a", encoding="utf-8") as errlog:
        # Watching for a stop signal starts before any server does, so a stop
        # while servers are starting closes them too.
        async with anyio.create_task_group() as stop:
            stop.start_soon(_stop_on_signal, stop.cancel_scope)
            try:
                async with Switchboard(
                    ConnectionStore(), approve=approve, record=record, errlog=errlog,
                    skills=read_skills(run_dir), connections=connections, only=only,
                ) as board:  # fmt: skip

                    async def on_list_tools(ctx: Any, params: Any) -> ListToolsResult:
                        return ListToolsResult(tools=board.tools())

                    async def on_call_tool(ctx: Any, params: Any) -> CallToolResult:
                        return await board.call(params.name, dict(params.arguments or {}))

                    server = Server(
                        "metabot", version=__version__, on_list_tools=on_list_tools,
                        on_call_tool=on_call_tool,
                    )  # fmt: skip
                    async with _stdin_lines() as lines, stdio_server(stdin=lines) as (read, write):
                        await server.run(read, write, server.create_initialization_options())
            finally:
                stop.cancel_scope.cancel()  # the input ended, or a signal came: stop watching


async def _stop_on_signal(scope: anyio.CancelScope) -> None:
    """End the serve loop on a stop signal the way the end of input ends it.

    The default for SIGTERM and SIGHUP is to die at once. That left every
    connection server running, since each starts in its own session and
    gets no signal from the engine's process group.
    """
    with anyio.open_signal_receiver(signal.SIGTERM, signal.SIGHUP, signal.SIGINT) as signals:
        async for _ in signals:
            scope.cancel()
            return


class _Lines:
    """The engine's messages a line at a time, read by the event loop itself."""

    def __init__(self, reader: asyncio.StreamReader) -> None:
        self.reader = reader

    def __aiter__(self) -> _Lines:
        return self

    async def __anext__(self) -> str:
        line = await self.reader.readline()
        if not line:
            raise StopAsyncIteration
        return line.decode("utf-8", errors="replace")


@contextlib.asynccontextmanager
async def _stdin_lines() -> AsyncIterator[Any]:
    """stdin as lines a cancel can stop; None, the library's own reader, if not a pipe.

    The library reads stdin on a worker thread, and a thread blocked in a read
    cannot be cancelled: a stop signal would wait for the engine's next line,
    with every connection server still running.
    """
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=LINE_LIMIT)
    pipe = os.fdopen(os.dup(0), "rb", buffering=0)
    try:
        transport, _ = await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), pipe
        )
    except (OSError, ValueError):  # a regular file, say: the library reads it
        pipe.close()
        yield None
        return
    try:
        yield _Lines(reader)
    finally:
        transport.close()
