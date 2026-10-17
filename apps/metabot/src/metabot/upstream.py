"""A client session to one connection's MCP server.

A stdio server starts with the minimal inherited environment (home, path,
shell, terminal, user) plus its own token variable, and nothing else from
this process. An http server gets its token in one header: ``Authorization:
Bearer <token>`` by default, or the raw token in a named header.
"""

from __future__ import annotations

import contextlib
import sys
from typing import Any, TextIO

import anyio
import httpx2
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp_types import CallToolResult, PaginatedRequestParams, TextContent, Tool

from metabot.connections import Connection

MAX_TOOL_PAGES = 20


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
