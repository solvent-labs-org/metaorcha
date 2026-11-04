"""A client session to one connection's MCP server.

A stdio server starts with the minimal inherited environment (home, path,
shell, terminal, user) plus its own token variable, and nothing else from
this process. An http server gets its token in one header: ``Authorization:
Bearer <token>`` by default, or the raw token in a named header.

A stdio server whose command names a plugin's copy outside this home is never
started (``misplaced_copy``).
"""

from __future__ import annotations

import contextlib
import os
import re
import shlex
import sys
from typing import Any, NamedTuple, TextIO

import anyio
import httpx2
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp_types import CallToolResult, PaginatedRequestParams, TextContent, Tool

from metabot.connections import Connection
from metabot.paths import metabot_home

MAX_TOOL_PAGES = 20
# a plugin's installed copy, as enable names it in a server's command: <home>/plugins/<name>@<tree>
PLUGIN_COPY = re.compile(r"plugins/([a-z0-9][a-z0-9._-]{0,63})@([0-9a-f]{64})")
# where a list or an option splits a word, as --root=PATH and a:PATH: a path starts after the
# last of them before the copy, wherever the program splits the word
SPLITS = frozenset("=:,")
# what a shell expands where it stands unquoted
EXPANDS = frozenset("$`*?[{")
# a path holding one of these may be split or expanded wherever a program reads a server's word
# again, as a shell behind env, an unquoted $0 or env -S does: no server names a copy by one,
# nor under a home whose path holds one
ODD = SPLITS | EXPANDS | frozenset("\"'\\;&|<>() \t\n\r\f\v")
SHELLS = frozenset({"sh", "bash", "dash", "zsh", "ksh", "mksh", "ash", "yash"})
# the home's path while a word is read, so that what the home's path holds is never what splits
# a word; no argument holds a NUL
HOME_HELD = "/\0home\0"
# what a shell reads otherwise than shlex: a comment, a here-document, a group, a substitution,
# an extglob, an expansion in braces; a line that holds one names no copy
UNREAD = ("#", "<<", "(", ")", "{", "}", "`")


def _real(path: str | os.PathLike[str]) -> str | None:
    """Where ``path`` leads, each part of it followed; None when no system call can follow it
    (a part is gone, a link loops, a NUL is in it, a ``..`` comes after a file or a folder that
    cannot be entered), as it then names no file."""
    try:
        os.stat(path)  # 3.11 and 3.12 take a ".." past a part they can lstat as text
        return os.path.realpath(path, strict=True)
    except (OSError, ValueError, RuntimeError):
        return None


def _script_at(command: list[str]) -> int | None:
    """Where the line a shell runs with ``-c`` is in ``command``: its first word after the
    options. None when ``command`` runs no such line, or has an option this does not read."""
    if not command or os.path.basename(command[0]) not in SHELLS:
        return None
    line, at = False, 1
    while at < len(command) and command[at][:1] in "-+":
        word = command[at]
        if word in ("--", "-", "+"):  # the options end; the line is the next word
            at += 1
            break
        if word.startswith("--") or not word[1:].isalpha() or {"o", "O"} & set(word[1:-1]):
            return None  # a long option, or a shell option's name, may take the next word
        line = line or (word[0] == "-" and "c" in word)
        at += 2 if word[-1] in "oO" else 1  # -o and -O name a shell option
    return at if line and at < len(command) else None


def _shell_words(line: str) -> list[str] | None:
    """The words a shell reads in ``line``, its quotes taken off and its operators apart; None
    when it holds what this does not read as a shell does (``UNREAD``), or a quote not closed.
    Only a blank or a newline splits two words, as in the shell."""
    if any(part in line for part in UNREAD):
        return None
    lexer = shlex.shlex(line, posix=True, punctuation_chars=";&|<>")
    lexer.whitespace = " \t\n"
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        return list(lexer)
    except ValueError:
        return None


def _whole(word: str, found: re.Match[str], copy: str | None, home: str) -> bool:
    """Whether ``word`` names the copy ``found`` by a whole path that is ``copy``, from the
    word's start or after the last place a list or an option splits it before the copy, and
    holds nothing a program that reads it again can split or expand, as the home's own path
    does not (``HOME_HELD`` in ``word``): through a link or past a ``..`` too."""
    start = max((i + 1 for i, ch in enumerate(word[: found.start()]) if ch in SPLITS), default=0)
    path = word[start : found.end()]
    if ODD.intersection(path):
        return False
    path = path.replace(HOME_HELD, home)
    return copy is not None and path.startswith("/") and _real(path) == copy


def misnamed_copy(command: list[str]) -> tuple[str, str] | None:
    """The name and tree of a plugin's copy a word of ``command`` names other than by a whole
    path to the one in this home, if any. A shell's ``-c`` line is read as the shell reads it,
    every other word as one word, as a program gets it; the home's path, as it is written, is
    read as if it held nothing that splits a word (what it holds is ``refused_copy``'s)."""
    home = metabot_home()
    script = _script_at(command)
    for at, word in enumerate(command):
        held = word.replace(f"{home}/plugins/", f"{HOME_HELD}/plugins/")
        words = _shell_words(held) if at == script else [held]
        for found in PLUGIN_COPY.finditer(held):
            if words is None:  # no shell runs it, and it names no copy whole
                return found[1], found[2]
        for read in words or ():
            for found in PLUGIN_COPY.finditer(read):
                copy = _real(home / "plugins" / f"{found[1]}@{found[2]}")
                if not _whole(read, found, copy, str(home)):
                    return found[1], found[2]
    return None


def odd_home() -> str | None:
    """What in this home's path a program can split or expand, if anything: a server that
    names a copy there may start a file beside it."""
    odd = sorted(ODD.intersection(str(metabot_home())))
    return ", ".join(repr(ch) for ch in odd) or None


class Refusal(NamedTuple):
    """Why a server's command must not start a plugin's copy."""

    name: str
    tree: str
    misnamed: bool  # a word names the copy other than by a whole path to this home's
    odd: str | None  # what in this home's path a program can split or expand


def refused_copy(command: list[str]) -> Refusal | None:
    """Why ``command`` must not start, if it names a plugin's copy: a word names it other than
    by a whole path to the one in this home, or this home's path holds what a program can
    split or expand, under which no server names a copy."""
    odd = odd_home()
    misnamed = misnamed_copy(command)
    if misnamed is not None:
        return Refusal(*misnamed, True, odd)
    words = command if odd else []
    found = next((m for word in words for m in PLUGIN_COPY.finditer(word)), None)
    return Refusal(found[1], found[2], False, odd) if found else None


def misplaced_copy(command: list[str]) -> str | None:
    """Why a server must not start: its command names a plugin's copy other than this home's.

    0.2.0 saved a plugin's server under a home named by a relative path as it was typed, and a
    home can have moved since. Read in the folder the server starts in, such a word can name a
    file anyone put there, not the copy its hash was checked on. A whole path to this home's
    copy starts, also through a link or past a ``..``, when it holds nothing a program can split
    or expand; enabling that version again names it so, once the home's path holds none.
    """
    refusal = refused_copy(command)
    if refusal is None:
        return None
    home = metabot_home()
    said = (
        f"it names a copy of plugin {refusal.name} other than by a whole path to the one in "
        f"{home}/plugins"
        if refusal.misnamed
        else f"it names a copy of plugin {refusal.name} in {home}/plugins"
    )
    if refusal.odd:
        said += (
            f"; the home's path holds {refusal.odd}, which a program can split or expand: move "
            "the home to a path without them, then enable it again"
        )
    else:
        said += "; enable it again"
    return f"{said}: {_enable_again(refusal.tree)}"


def _enable_again(tree: str) -> str:
    """The line that enables the copy ``tree`` again, as the person enabled it."""
    from metabot.plugins import PluginError, enable_line, installed

    try:
        held = next((p for p in installed() if p.tree == tree), None)
    except (PluginError, OSError, ValueError):  # the line alone, which then says what is wrong
        held = None
    return enable_line(held) if held else f"metabot plugin enable {tree[:12]} --add-servers"


class Upstream:
    def __init__(
        self,
        conn: Connection,
        secret: str | None,
        *,
        timeout: float = 120.0,
        init_timeout: float = 30.0,
        errlog: TextIO | None = None,
    ) -> None:
        self.conn = conn
        self.secret = secret
        self.timeout = timeout
        self.init_timeout = init_timeout
        self.errlog = errlog
        self.session: ClientSession | None = None
        self._stack: contextlib.AsyncExitStack | None = None

    def _headers(self) -> dict[str, str]:
        if not self.secret:
            return {}
        if self.conn.header:
            return {self.conn.header: self.secret}
        return {"Authorization": f"Bearer {self.secret}"}

    async def _open(self, stack: contextlib.AsyncExitStack) -> ClientSession:
        conn = self.conn
        if conn.transport == "stdio":
            assert conn.command
            refused = misplaced_copy(conn.command)
            if refused:
                raise PermissionError(refused)
            env = {conn.token_env: self.secret} if conn.token_env and self.secret else {}
            params = StdioServerParameters(command=conn.command[0], args=conn.command[1:], env=env)
            read, write = await stack.enter_async_context(
                stdio_client(params, errlog=self.errlog or sys.stderr)
            )
        else:
            assert conn.url
            client = await stack.enter_async_context(
                httpx2.AsyncClient(headers=self._headers(), timeout=self.timeout)
            )
            read, write = await stack.enter_async_context(
                streamable_http_client(conn.url, http_client=client)
            )
        session = await stack.enter_async_context(ClientSession(read, write))
        with anyio.fail_after(self.init_timeout):
            await session.initialize()
        return session

    async def __aenter__(self) -> Upstream:
        stack = contextlib.AsyncExitStack()
        await stack.__aenter__()
        try:
            self.session = await self._open(stack)
        except BaseException:
            await stack.aclose()
            raise
        self._stack = stack
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._stack is not None:
            await self._stack.aclose()
            self._stack = None

    async def list_tools(self) -> list[Tool]:
        assert self.session is not None
        tools: list[Tool] = []
        cursor = None
        for _ in range(MAX_TOOL_PAGES):
            params = PaginatedRequestParams(cursor=cursor) if cursor else None
            page = await self.session.list_tools(params=params)
            tools.extend(page.tools)
            cursor = page.next_cursor
            if not cursor:
                break
        return tools

    async def call_tool(self, name: str, args: dict[str, Any]) -> CallToolResult:
        assert self.session is not None
        result = await self.session.call_tool(name, args, read_timeout_seconds=self.timeout)
        if isinstance(result, CallToolResult):
            return result
        text = f"the server asked for more input ({type(result).__name__}); not supported"
        return CallToolResult(content=[TextContent(type="text", text=text)], is_error=True)
