"""MCP (Model Context Protocol) adapter implementation."""

import asyncio
import logging
from collections.abc import Mapping
from typing import Any

import httpx

from .base import BaseAdapter, CapabilityData, HarvestResult

logger = logging.getLogger(__name__)


class MCPAdapter(BaseAdapter):
    """
    Adapter for MCP (Model Context Protocol) agents.

    Transport routing:
    - SSE              → mcp.client.sse.sse_client  (stateful SSE handshake via MCP SDK)
    - HTTP/streamable-http → raw JSON-RPC POST (MCPAdapter._retry_request)

    Routing is determined by the ``transport_type`` constructor argument, which
    must match the ``type`` field in the agent's Transport record — not inferred
    from the endpoint URL.

    ``headers`` authenticate the listing calls (``initialize`` and the three
    ``*/list``) of a server that gates them behind a token, as a connection's
    does. They are held for this harvest only: never stored, never logged, and
    never sent on a ``tools/call``, because a call made with the user's token
    can act on their account.
    """

    def __init__(
        self,
        endpoint: str,
        transport_type: str = "http",
        timeout: float = 10.0,
        max_retries: int = 3,
        headers: Mapping[str, str] | None = None,
    ):
        super().__init__(endpoint=endpoint, timeout=timeout, max_retries=max_retries)
        self._is_sse = transport_type.lower() == "sse"
        self._headers = dict(headers or {})

    async def harvest(self) -> HarvestResult:
        """Harvest MCP capabilities.

        For SSE endpoints: uses the MCP SDK's sse_client to establish the full
        SSE handshake (GET /sse → endpoint event → initialize → list_*).

        For HTTP/streamable-http endpoints: sends raw JSON-RPC POST requests.
        """
        if self._is_sse:
            return await self._harvest_via_sse_sdk()
        return await self._harvest_via_http()

    # ── SSE harvesting via MCP SDK ────────────────────────────────────────────

    async def _harvest_via_sse_sdk(self) -> HarvestResult:
        """Use mcp.client.sse to connect, initialize, and list capabilities."""
        try:
            from mcp import ClientSession
            from mcp.client.sse import sse_client
        except ImportError as exc:
            raise ConnectionError(
                "mcp SDK not installed — cannot harvest SSE MCP agent"
            ) from exc

        capabilities: list[CapabilityData] = []
        errors: list[str] = []

        try:
            async with (
                sse_client(self.endpoint, headers=self._headers or None) as (
                    read,
                    write,
                ),
                ClientSession(read, write) as session,
            ):
                await session.initialize()

                # tools/list
                try:
                    tools_result = await session.list_tools()
                    for tool in tools_result.tools:
                        schema = None
                        if hasattr(tool, "inputSchema") and tool.inputSchema:
                            schema = (
                                tool.inputSchema.model_dump()
                                if hasattr(tool.inputSchema, "model_dump")
                                else dict(tool.inputSchema)
                            )
                        capabilities.append(
                            CapabilityData(
                                type="tool",
                                id=tool.name,
                                name=tool.name,
                                description=tool.description or "",
                                input_schema=schema,
                            )
                        )
                except Exception as exc:
                    errors.append(f"tools/list failed: {exc}")
                    logger.warning(
                        "SSE tools/list failed for %s: %s", self.endpoint, exc
                    )

                # resources/list
                try:
                    res_result = await session.list_resources()
                    for resource in res_result.resources:
                        capabilities.append(
                            CapabilityData(
                                type="resource",
                                id=str(resource.uri),
                                name=resource.name,
                                description=resource.description or "",
                                uri_template=str(resource.uri),
                                mime_type=resource.mimeType,
                            )
                        )
                except Exception as exc:
                    errors.append(f"resources/list failed: {exc}")
                    logger.debug(
                        "SSE resources/list failed for %s: %s", self.endpoint, exc
                    )

                # prompts/list
                try:
                    prompts_result = await session.list_prompts()
                    for prompt in prompts_result.prompts:
                        raw_args = getattr(prompt, "arguments", None) or []
                        arguments = [
                            {
                                "name": a.name,
                                "description": getattr(a, "description", ""),
                                "required": getattr(a, "required", False),
                            }
                            for a in raw_args
                        ] or None
                        capabilities.append(
                            CapabilityData(
                                type="prompt",
                                id=prompt.name,
                                name=prompt.name,
                                description=prompt.description or "",
                                arguments=arguments,
                            )
                        )
                except Exception as exc:
                    errors.append(f"prompts/list failed: {exc}")
                    logger.debug(
                        "SSE prompts/list failed for %s: %s", self.endpoint, exc
                    )

        except Exception as exc:
            raise ConnectionError(
                f"Failed to connect to SSE MCP agent at {self.endpoint}: {exc}"
            ) from exc

        logger.info(
            "SSE harvest complete — endpoint=%s tools=%d resources=%d prompts=%d errors=%s",
            self.endpoint,
            sum(1 for c in capabilities if c.type == "tool"),
            sum(1 for c in capabilities if c.type == "resource"),
            sum(1 for c in capabilities if c.type == "prompt"),
            errors or "none",
        )
        return HarvestResult(capabilities=capabilities, errors=errors)

    # ── HTTP / streamable-http harvesting (raw JSON-RPC POST) ─────────────────

    async def _harvest_via_http(self) -> HarvestResult:
        """Harvest via raw JSON-RPC POST — for streamable-http / plain HTTP endpoints."""
        capabilities: list[CapabilityData] = []
        errors: list[str] = []

        try:
            results = await asyncio.gather(
                self._harvest_tools(),
                self._harvest_resources(),
                self._harvest_prompts(),
                return_exceptions=True,
            )
            for result in results:
                if isinstance(result, Exception):
                    errors.append(str(result))
                else:
                    capabilities.extend(result)

            return HarvestResult(capabilities=capabilities, errors=errors)

        except Exception as exc:
            raise ConnectionError(f"Failed to harvest MCP agent: {exc}") from exc

    async def _extract_x402(
        self, tool_id: str, metadata: dict[str, Any]
    ) -> tuple[str | None, str | None]:
        """Return (price, asset) from tool metadata or X-Microtransaction header."""
        x402 = metadata.get("x402", {})
        if x402.get("price") and x402.get("asset"):
            return x402["price"], x402["asset"]

        # Probe the endpoint with a dry-run call and check for the payment header.
        try:
            payload = {
                "jsonrpc": "2.0",
                "id": 0,
                "method": "tools/call",
                "params": {"name": tool_id, "arguments": {}},
            }
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    self.endpoint, json=payload, timeout=self.timeout
                )
                header = response.headers.get("X-Microtransaction", "")
                if header:
                    parts = dict(
                        item.split("=", 1) for item in header.split(";") if "=" in item
                    )
                    return parts.get("price"), parts.get("asset")
        except Exception as exc:
            logger.debug("x402 header probe failed for %s: %s", tool_id, exc)

        return None, None

    async def _harvest_tools(self) -> list[CapabilityData]:
        payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        async with httpx.AsyncClient(headers=self._headers) as client:
            response = await self._retry_request(client, payload)
            tools = []
            if "result" in response and "tools" in response["result"]:
                for tool in response["result"]["tools"]:
                    metadata = tool.get("metadata", {})
                    x402 = metadata.get("x402", {})
                    tools.append(
                        CapabilityData(
                            type="tool",
                            id=tool["name"],
                            name=tool["name"],
                            description=tool.get("description", ""),
                            input_schema=tool.get("inputSchema"),
                            output_schema=tool.get("outputSchema"),
                            x402_price=x402.get("price"),
                            x402_asset=x402.get("asset"),
                        )
                    )
            return tools

    async def _harvest_resources(self) -> list[CapabilityData]:
        payload = {"jsonrpc": "2.0", "id": 2, "method": "resources/list", "params": {}}
        async with httpx.AsyncClient(headers=self._headers) as client:
            response = await self._retry_request(client, payload)
            resources = []
            if "result" in response and "resources" in response["result"]:
                for resource in response["result"]["resources"]:
                    resources.append(
                        CapabilityData(
                            type="resource",
                            id=resource["uri"],
                            name=resource.get("name", resource["uri"]),
                            description=resource.get("description", ""),
                            uri_template=resource["uri"],
                            mime_type=resource.get("mimeType"),
                        )
                    )
            return resources

    async def _harvest_prompts(self) -> list[CapabilityData]:
        payload = {"jsonrpc": "2.0", "id": 3, "method": "prompts/list", "params": {}}
        async with httpx.AsyncClient(headers=self._headers) as client:
            response = await self._retry_request(client, payload)
            prompts = []
            if "result" in response and "prompts" in response["result"]:
                for prompt in response["result"]["prompts"]:
                    prompts.append(
                        CapabilityData(
                            type="prompt",
                            id=prompt["name"],
                            name=prompt["name"],
                            description=prompt.get("description", ""),
                            arguments=prompt.get("arguments", []),
                        )
                    )
            return prompts

    async def _retry_request(
        self, client: httpx.AsyncClient, payload: dict[str, Any]
    ) -> dict[str, Any]:
        for attempt in range(self.max_retries):
            try:
                response = await client.post(
                    self.endpoint, json=payload, timeout=self.timeout
                )
                response.raise_for_status()
                return response.json()
            except httpx.HTTPError as exc:
                if attempt == self.max_retries - 1:
                    raise ConnectionError(
                        f"Failed after {self.max_retries} attempts: {exc}"
                    ) from exc
                await asyncio.sleep(2**attempt)
        raise ConnectionError("Retry failed")

    async def health_check(self) -> bool:
        try:
            payload = {
                "jsonrpc": "2.0",
                "id": 0,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "Orcha Registry", "version": "1.0.0"},
                },
            }
            async with httpx.AsyncClient() as client:
                response = await client.post(self.endpoint, json=payload, timeout=5)
                return response.status_code == 200
        except Exception:
            return False
