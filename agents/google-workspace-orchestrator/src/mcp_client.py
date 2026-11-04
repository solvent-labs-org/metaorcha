"""Streamable-HTTP MCP client — JSON-RPC POST to /mcp (workspace-mcp)."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx

from .token_store import log_ref

logger = logging.getLogger(__name__)

_JSONRPC = "2.0"
# Align with streamable-http MCP servers (FastMCP / python-sdk transport).
_INIT_PROTOCOL = "2025-11-25"

_MCP_HEADERS: dict[str, str] = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


def _session_headers(session_id: str | None, protocol_version: str | None) -> dict[str, str]:
    h: dict[str, str] = {}
    if session_id:
        h["mcp-session-id"] = session_id
    if protocol_version:
        h["mcp-protocol-version"] = protocol_version
    return h


class WorkspaceMCPClient:
    def __init__(self, base_mcp_url: str, timeout: float = 120.0) -> None:
        self._url = base_mcp_url.rstrip("/")
        if not self._url.endswith("/mcp"):
            self._url = f"{self._url}/mcp"
        self._timeout = timeout
        self._id = 0
        self._lock = asyncio.Lock()
        self._session_id: str | None = None
        self._protocol_version: str | None = None
        # Separate "have we completed the handshake" flag so that a stateless
        # server (WORKSPACE_MCP_STATELESS_MODE=true / EXTERNAL_OAUTH21_PROVIDER)
        # that omits the optional Mcp-Session-Id header is still treated as
        # initialized and we don't re-initialize on every request.
        self._initialized: bool = False

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    @staticmethod
    def _auth_header(bearer: str | None) -> dict[str, str]:
        if not bearer:
            return {}
        return {"Authorization": f"Bearer {bearer}"}

    async def ping(self) -> bool:
        """Return True when the workspace-mcp HTTP server is reachable.

        Uses the OAuth 2.1 resource-metadata discovery endpoint which workspace-mcp
        always exposes without authentication.  Falls back to the bare base URL on
        any path-related error.  A sub-500 status code means the server is up.
        """
        base = self._url[: -len("/mcp")] if self._url.endswith("/mcp") else self._url
        for path in ("/.well-known/oauth-protected-resource", "/"):
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    resp = await client.get(f"{base}{path}")
                    logger.debug("mcp_ping url=%s%s status=%d", base, path, resp.status_code)
                    if resp.status_code < 500:
                        return True
            except Exception as exc:
                logger.debug("mcp_ping_error url=%s%s error=%s", base, path, exc)
        return False

    def _merge_request_headers(
        self,
        bearer: str | None,
        *,
        include_session: bool,
    ) -> dict[str, str]:
        merged: dict[str, str] = {**_MCP_HEADERS, **self._auth_header(bearer)}
        if include_session:
            merged.update(_session_headers(self._session_id, self._protocol_version))
        return merged

    @staticmethod
    def _parse_json_or_sse_body(resp: httpx.Response) -> dict[str, Any]:
        if resp.status_code == 202:
            return {}
        text = resp.text
        if not text.strip():
            return {}
        try:
            return resp.json()
        except json.JSONDecodeError:
            for line in text.splitlines():
                line = line.strip()
                if line.startswith("data:"):
                    raw = line[5:].strip()
                    if raw and raw != "[DONE]":
                        return json.loads(raw)
            logger.warning("mcp_non_json_response preview=%s", text[:300])
            raise

    async def _post(
        self,
        payload: dict[str, Any],
        bearer: str | None,
        *,
        include_session: bool = True,
    ) -> tuple[dict[str, Any], httpx.Headers]:
        headers = self._merge_request_headers(bearer, include_session=include_session)
        method_name = (payload.get("params") or {}).get("name") or payload.get("method", "?")
        logger.info(
            "mcp_post url=%s method=%s has_bearer=%s bearer_ref=%s session_id=%s",
            self._url,
            method_name,
            bool(bearer),
            log_ref(bearer) if bearer else "<none>",
            self._session_id,
        )
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(self._url, json=payload, headers=headers)
            logger.info(
                "mcp_post_response method=%s status=%d has_bearer=%s",
                method_name,
                resp.status_code,
                bool(bearer),
            )
            if resp.status_code >= 400:
                logger.warning(
                    "mcp_http_error method=%s status=%d has_bearer=%s preview=%s",
                    method_name,
                    resp.status_code,
                    bool(bearer),
                    resp.text[:800],
                )
            resp.raise_for_status()
            return self._parse_json_or_sse_body(resp), resp.headers

    def _reset_session(self) -> None:
        self._session_id = None
        self._protocol_version = None
        self._initialized = False

    async def _ensure_session(self, bearer: str | None) -> None:
        if self._initialized:
            return
        init_payload = {
            "jsonrpc": _JSONRPC,
            "id": self._next_id(),
            "method": "initialize",
            "params": {
                "protocolVersion": _INIT_PROTOCOL,
                "capabilities": {},
                "clientInfo": {"name": "orcha-workspace-agent", "version": "0.1.0"},
            },
        }
        body, hdrs = await self._post(init_payload, bearer, include_session=False)
        err = body.get("error")
        if err:
            raise RuntimeError(str(err))
        # Mcp-Session-Id is optional per the MCP streamable-HTTP spec.
        # Stateless servers (WORKSPACE_MCP_STATELESS_MODE / EXTERNAL_OAUTH21_PROVIDER)
        # intentionally omit it; subsequent requests simply won't carry a session header.
        self._session_id = hdrs.get("mcp-session-id") or None
        result = body.get("result") or {}
        negotiated = result.get("protocolVersion")
        self._protocol_version = str(negotiated) if negotiated else _INIT_PROTOCOL
        self._initialized = True
        logger.info(
            "mcp_initialized session_id=%s protocol=%s",
            self._session_id,
            self._protocol_version,
        )

        notif = {"jsonrpc": _JSONRPC, "method": "notifications/initialized"}
        await self._post(notif, bearer, include_session=True)

    async def initialize(self, bearer: str | None) -> dict[str, Any]:
        """Perform (or refresh) MCP handshake; returns a minimal result shape."""
        async with self._lock:
            self._reset_session()
            await self._ensure_session(bearer)
            return {
                "result": {
                    "protocolVersion": self._protocol_version,
                }
            }

    async def _tools_list_locked(self, bearer: str | None) -> list[dict[str, Any]]:
        await self._ensure_session(bearer)
        payload = {"jsonrpc": _JSONRPC, "id": self._next_id(), "method": "tools/list", "params": {}}
        data, _ = await self._post(payload, bearer)
        err = data.get("error")
        if err:
            raise RuntimeError(str(err))
        res = data.get("result") or {}
        tools = list(res.get("tools") or [])
        logger.info("mcp_tools_list has_bearer=%s tool_count=%d", bool(bearer), len(tools))
        return tools

    async def tools_list(self, bearer: str | None) -> list[dict[str, Any]]:
        async with self._lock:
            try:
                return await self._tools_list_locked(bearer)
            except httpx.HTTPStatusError as exc:
                if exc.response is not None and exc.response.status_code in (400, 404):
                    self._reset_session()
                    return await self._tools_list_locked(bearer)
                raise

    async def _tools_call_locked(
        self,
        name: str,
        arguments: dict[str, Any],
        bearer: str | None,
    ) -> dict[str, Any]:
        await self._ensure_session(bearer)
        payload = {
            "jsonrpc": _JSONRPC,
            "id": self._next_id(),
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        logger.info(
            "mcp_tool_call name=%s has_bearer=%s args_keys=%s",
            name,
            bool(bearer),
            sorted(arguments.keys()) if isinstance(arguments, dict) else [],
        )
        data, _ = await self._post(payload, bearer)
        err = data.get("error")
        if err:
            logger.warning("mcp_tool_error name=%s error=%s", name, str(err)[:300])
        else:
            logger.info("mcp_tool_ok name=%s", name)
        return data

    async def tools_call(
        self,
        name: str,
        arguments: dict[str, Any],
        bearer: str | None,
    ) -> dict[str, Any]:
        async with self._lock:
            try:
                return await self._tools_call_locked(name, arguments, bearer)
            except httpx.HTTPStatusError as exc:
                if exc.response is not None and exc.response.status_code in (400, 404):
                    logger.warning(
                        "mcp_tool_session_reset name=%s status=%d — retrying",
                        name,
                        exc.response.status_code,
                    )
                    self._reset_session()
                    return await self._tools_call_locked(name, arguments, bearer)
                raise
