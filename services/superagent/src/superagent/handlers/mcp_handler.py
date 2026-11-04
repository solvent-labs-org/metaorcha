"""MCPHandler — stateless MCP tool calls over SSE or STDIO transport."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from .base import AgentHandler

try:
    from mcp import ClientSession
    from mcp.client.sse import sse_client
    from mcp.client.stdio import StdioServerParameters, stdio_client
except ImportError:
    ClientSession = None  # type: ignore[assignment,misc]
    sse_client = None  # type: ignore[assignment]
    StdioServerParameters = None  # type: ignore[assignment,misc]
    stdio_client = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

_LEGACY_EMERGE_PREFIX = "/app/common/emerge-tools"


# AD-13: the only host variables a stdio subprocess inherits. PATH and HOME so
# the interpreter and its caches are found; locale so output decodes.
STDIO_ENV_ALLOWLIST: tuple[str, ...] = ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE")


def build_stdio_env(
    resolved_env: dict[str, str] | None,
    host_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """The subprocess environment: allow-listed host keys + the connection's vars."""
    source = os.environ if host_env is None else host_env
    env = {k: source[k] for k in STDIO_ENV_ALLOWLIST if k in source}
    env.update(resolved_env or {})
    return env


def _resolve_stdio_cmd_args(cmd_args: list[Any]) -> list[str]:
    """Resolve Chomper and other emerge-tools script paths for local vs Docker layouts."""
    from ..config import settings

    root = Path(settings.emerge_tools_dir).expanduser()
    try:
        root = root.resolve()
    except OSError:
        root = root.absolute()

    out: list[str] = []
    for raw in cmd_args:
        if not isinstance(raw, str):
            out.append(str(raw))
            continue
        s = raw
        if s.startswith(_LEGACY_EMERGE_PREFIX):
            rel = s.removeprefix(_LEGACY_EMERGE_PREFIX).lstrip("/")
            out.append(str((root / rel).resolve()))
            continue
        p = Path(s)
        if not p.is_absolute():
            candidate = (root / s).resolve()
            if candidate.is_file():
                out.append(str(candidate))
                continue
        out.append(s)
    return out


class MCPHandler(AgentHandler):
    """
    Handles MCP protocol agent calls.

    Session lifecycle per call: initialize → call_tool → result.
    Transport: SSE (HTTP endpoint) or STDIO (command + args).
    """

    async def call_tool(
        self,
        agent_id: str,
        capability_id: str,
        args: dict[str, Any],
        transport: dict[str, Any],
    ) -> Any:
        """Execute an MCP tool call and return the raw result."""
        transport_type = transport.get("type", "sse").upper()
        if transport_type == "STDIO":
            return await self._call_stdio(capability_id, args, transport)
        return await self._call_sse(agent_id, capability_id, args, transport)

    async def _call_sse(
        self,
        agent_id: str,
        capability_id: str,
        args: dict[str, Any],
        transport: dict[str, Any],
    ) -> Any:
        """MCP over SSE transport."""
        if sse_client is None:
            return await self._call_sse_raw(agent_id, capability_id, args, transport)

        endpoint = transport.get("endpoint", "")
        headers = self._auth_headers.copy()

        logger.debug(
            "MCP SSE call — agent=%s capability=%s endpoint=%r",
            agent_id,
            capability_id,
            endpoint,
        )

        async with (
            sse_client(endpoint, headers=headers) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            result = await session.call_tool(capability_id, args)
            return self._extract_content(result)

    async def _call_sse_raw(
        self,
        agent_id: str,
        capability_id: str,
        args: dict[str, Any],
        transport: dict[str, Any],
    ) -> Any:
        """Fallback raw HTTP MCP call when mcp SDK not available."""
        import httpx

        endpoint = transport.get("endpoint", "")
        async with httpx.AsyncClient(headers=self._auth_headers) as client:
            resp = await client.post(
                f"{endpoint}/tools/call",
                json={"name": capability_id, "arguments": args},
                timeout=60.0,
            )
            resp.raise_for_status()
            return resp.json()

    async def _call_stdio(
        self,
        capability_id: str,
        args: dict[str, Any],
        transport: dict[str, Any],
    ) -> Any:
        """MCP over STDIO transport."""
        if stdio_client is None:
            logger.warning("mcp SDK not installed — STDIO transport unavailable")
            return "Error: MCP SDK not installed"

        command = transport.get("command", "")
        cmd_args = _resolve_stdio_cmd_args(list(transport.get("args", [])))
        # resolved_env is set by PreFlightManager._resolve_stdio_env —
        # ${VAR} placeholders from emerge.yaml are already expanded at this point.
        # StdioServerParameters.env replaces the entire environment when non-None;
        # build_stdio_env passes only the allow-listed host keys (so node/python
        # are found) plus the connection's own variables — never VAULT_KEY or
        # any other service secret (AD-13).
        resolved_env: dict[str, str] | None = transport.get("resolved_env")
        subprocess_env = build_stdio_env(resolved_env)

        logger.debug(
            "MCP STDIO call — capability=%s command=%r args=%r explicit_env_keys=%s",
            capability_id,
            command,
            cmd_args,
            sorted(resolved_env.keys()) if resolved_env else [],
        )

        params = StdioServerParameters(
            command=command, args=cmd_args, env=subprocess_env
        )

        async with (
            stdio_client(params) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            result = await session.call_tool(capability_id, args)
            return self._extract_content(result)

    @staticmethod
    def _extract_content(result: Any) -> Any:
        """Extract content from MCP CallToolResult."""
        if hasattr(result, "content"):
            content = result.content
            if isinstance(content, list) and len(content) == 1:
                item = content[0]
                if hasattr(item, "text"):
                    return item.text
            return content
        return result
