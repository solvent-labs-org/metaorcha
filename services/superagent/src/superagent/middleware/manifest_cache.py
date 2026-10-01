"""ManifestCache — 5-minute TTL cache of agent manifests from Registry."""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from ..config import settings

logger = logging.getLogger(__name__)

_TTL_SECONDS = 300  # 5 minutes


class ManifestCache:
    """In-process LRU-style cache for agent manifests."""

    def __init__(self) -> None:
        self._cache: dict[str, tuple[dict[str, Any], float]] = {}
        self._pinned: dict[str, dict[str, Any]] = {}  # boot-time seeds, no TTL
        self._client: httpx.AsyncClient | None = None

    def seed(self, agent_id: str, manifest: dict[str, Any]) -> None:
        """Pin a manifest permanently (no TTL).

        Called at boot for system MCP baseline agents so PreFlight never needs
        to reach Registry for them — avoiding health-check failures when the
        Registry is unreachable or hasn't propagated health_status yet.
        """
        self._pinned[agent_id] = manifest

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=settings.registry_service_url,
                timeout=httpx.Timeout(10.0),
            )
        return self._client

    async def get_manifest(self, agent_id: str) -> dict[str, Any]:
        """Return cached manifest or fetch from Registry."""
        if agent_id in self._pinned:
            return self._pinned[agent_id]

        cached = self._cache.get(agent_id)
        if cached:
            manifest, ts = cached
            if time.monotonic() - ts < _TTL_SECONDS:
                return manifest

        manifest = await self._fetch(agent_id)
        self._cache[agent_id] = (manifest, time.monotonic())
        return manifest

    async def _fetch(self, agent_id: str) -> dict[str, Any]:
        client = self._get_client()
        try:
            resp = await client.get(f"/api/v1/agents/{agent_id}")
            resp.raise_for_status()
            raw = resp.json()
            return self._normalise(raw)
        except Exception as exc:
            logger.warning("Failed to fetch manifest for %s: %s", agent_id, exc)
            return {"agent_id": agent_id, "capabilities": [], "security": {}}

    @staticmethod
    def _normalise(raw: dict[str, Any]) -> dict[str, Any]:
        """Flatten the Registry envelope into the shape superagent internals expect.

        Registry returns: {"status": "success", "data": {"identity": ..., "metadata": ...,
                           "protocol": {"transport": ...}, "security": ..., "capabilities": ...}}
        Superagent expects: flat keys — health_status, transport, security, capabilities.
        """
        data: dict[str, Any] = raw.get("data", raw)
        metadata: dict[str, Any] = data.get("metadata", {})
        protocol: dict[str, Any] = data.get("protocol", {})

        # Remap capability "id" → "capability_id" so pipeline._get_capability_schema works
        capabilities = [
            {**cap, "capability_id": cap.get("capability_id") or cap.get("id", "")}
            for cap in data.get("capabilities", [])
        ]

        return {
            # identity
            "agent_id": data.get("identity", {}).get("id", ""),
            "name": data.get("identity", {}).get("name", ""),
            "description": data.get("identity", {}).get("description", ""),
            "tags": data.get("identity", {}).get("tags") or [],
            # health — pulled from metadata
            "health_status": metadata.get("health_status", ""),
            "health_endpoint": metadata.get("health_endpoint", ""),
            # transport — one level up from protocol
            "transport": protocol.get("transport", {}),
            # security and capabilities passed through
            "security": data.get("security", {}),
            "capabilities": capabilities,
            "payment": data.get("payment", {}),
        }

    def invalidate(self, agent_id: str) -> None:
        self._cache.pop(agent_id, None)

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None


# Module-level singleton
MANIFEST_CACHE = ManifestCache()
