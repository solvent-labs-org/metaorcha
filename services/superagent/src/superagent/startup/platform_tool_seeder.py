"""PlatformToolSeeder — registers system MCP tools with the Registry at SuperAgent boot."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

import httpx
import yaml

from ..clients.registry_client import RegistryClient

logger = logging.getLogger(__name__)

# Bounded retry for the registry cold-start race: the SuperAgent can boot
# before the Registry is accepting connections, and per-tool skips are
# permanent until restart. Retry only transient failures (connection /
# timeout / 5xx); parse errors, missing platform_env keys and 4xx remain
# permanent skips that never fail boot.
_MAX_ATTEMPTS = 5
_INITIAL_BACKOFF_S = 2.0
_MAX_BACKOFF_S = 30.0


class PlatformToolSeeder:
    """Reads emerge-tools/manifests/, validates platform_env secrets, registers with Registry.

    Boot behaviour (spec §3):
    - Loads all *.yaml from emerge_tools_dir/manifests/
    - For each manifest: checks every platform_env auth strategy key exists in
      ``os.environ`` (populate secrets via ``services/superagent/.env`` + Settings
      and ``main.lifespan`` ``setdefault`` before seeding)
    - If any key is missing: logs WARNING and skips that tool (does NOT fail boot)
    - If all keys present: POSTs to Registry ``POST /api/v1/agents/register`` via RegistryClient
    - Logs a summary: "Seeded N/M platform tools successfully"
    """

    def __init__(self, registry_client: RegistryClient, emerge_tools_dir: str) -> None:
        self._client = registry_client
        self._manifests_dir = Path(emerge_tools_dir) / "manifests"

    async def seed(self) -> None:
        if not self._manifests_dir.exists():
            logger.warning(
                "emerge-tools manifests dir not found at %s — skipping platform tool seeding",
                self._manifests_dir,
            )
            return

        yaml_files = sorted(self._manifests_dir.glob("*.yaml"))
        if not yaml_files:
            logger.warning("No manifest files found in %s", self._manifests_dir)
            return

        total = len(yaml_files)
        seeded = 0

        # Permanent skips (parse failure, missing platform_env) are settled
        # once, up front — they are never retried.
        eligible: list[tuple[Path, dict]] = []
        for yaml_path in yaml_files:
            try:
                manifest = yaml.safe_load(yaml_path.read_text())
            except Exception:
                logger.exception(
                    "Failed to parse manifest %s — skipping", yaml_path.name
                )
                continue

            missing_keys = self._missing_platform_env_keys(manifest)
            if missing_keys:
                logger.warning(
                    "Skipping %s — missing platform_env key(s): %s",
                    yaml_path.name,
                    ", ".join(missing_keys),
                )
                continue

            eligible.append((yaml_path, manifest))

        pending = eligible
        backoff = _INITIAL_BACKOFF_S
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            last_attempt = attempt == _MAX_ATTEMPTS
            retry_later: list[tuple[Path, dict]] = []

            for yaml_path, manifest in pending:
                try:
                    await self._client.upsert_agent(manifest)
                    seeded += 1
                    logger.debug("Seeded platform tool: %s", yaml_path.name)
                except Exception as exc:
                    if self._is_transient(exc) and not last_attempt:
                        retry_later.append((yaml_path, manifest))
                    else:
                        logger.exception(
                            "Failed to register platform tool %s — skipping",
                            yaml_path.name,
                        )

            if not retry_later:
                break

            logger.warning(
                "Registry not ready — retrying %d platform tool(s) in %.0fs "
                "(attempt %d/%d)",
                len(retry_later),
                backoff,
                attempt,
                _MAX_ATTEMPTS,
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _MAX_BACKOFF_S)
            pending = retry_later

        logger.info("Seeded %d/%d platform tools successfully", seeded, total)

    @staticmethod
    def _is_transient(exc: Exception) -> bool:
        """True for failures worth retrying: registry cold-start or overload.

        Connection/timeout errors (registry not up yet) and 5xx responses are
        transient; 4xx means the manifest itself was rejected and will fail
        identically on every retry.
        """
        if isinstance(exc, httpx.HTTPStatusError):
            return exc.response.status_code >= 500
        return isinstance(exc, httpx.TransportError)

    @staticmethod
    def _missing_platform_env_keys(manifest: dict) -> list[str]:
        """Return env keys declared as platform_env but absent or empty in os.environ.

        Checks truthiness, not just presence — `.env` files commonly ship with
        `KEY=` (present, empty) for optional integrations, which must still count
        as unconfigured. Offering a tool the runtime can't actually authenticate
        is worse than not offering it: the LLM has no way to know the key is a
        placeholder and will pick it over a working alternative.
        """
        missing: list[str] = []
        strategies = manifest.get("security", {}).get("auth_strategies", []) or []
        for strategy in strategies:
            if strategy.get("type") == "platform_env":
                key = (strategy.get("config") or {}).get("env_key", "")
                if key and not os.environ.get(key):
                    missing.append(key)
        return missing
