"""AuthManager — resolves credentials for agent calls using 5 strategies."""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

# OAuth token cache: (agent_id, user_id) → (access_token, expiry_ts)
_oauth_cache: dict[tuple[str, str], tuple[str, float]] = {}

_TOKEN_STRATEGIES = frozenset({"HTTP_BEARER", "X_API_KEY"})


class AuthResolutionError(Exception):
    """Raised when no auth strategy succeeds and HITL is required."""


def token_ref(strategy: dict[str, Any]) -> str | None:
    """The vault variable a token strategy reads, or None for any other kind."""
    kind = str(strategy.get("type") or "").upper()
    if kind not in _TOKEN_STRATEGIES:
        return None
    config = strategy.get("config") or {}
    if not isinstance(config, dict):
        return None
    if kind == "HTTP_BEARER":
        ref = config.get("token_vault_ref") or config.get("key_vault_ref")
    elif kind == "X_API_KEY":
        ref = config.get("key_vault_ref") or config.get("header_value")
    else:
        return None
    return ref if isinstance(ref, str) and ref else None


async def read_credential(
    vault: Any, user_id: str, agent_id: str, var: str, *, scoped_only: bool = False
) -> str | None:
    """The caller's credential ``var`` for the agent ``agent_id``.

    Spine AD-15: credentials live under ``agent:<DID>:env:<VAR>`` in the
    caller's own vault — the key the connect route writes. A bare ``<VAR>``
    row (what an AUTH_FORM resume stored before story 1.6b) is still read as
    a fallback, read-only: nothing is copied or deleted here.

    ``scoped_only`` (connections, story 1.7): no bare fallback. A bare row is
    not this connection's token — it may be another agent's — so it must not
    keep a revoked connection callable, nor be sent to its platform.
    """
    scoped = await vault.get_agent_env(user_id, agent_id, var)
    if scoped or scoped_only:
        return scoped or None
    return await vault.get_user_secret(user_id, var)


class AuthManager:
    """
    Resolves auth credentials for an agent.

    Strategy order (first success wins):
    1. NONE — agent requires no auth
    2. X_API_KEY / HTTP_BEARER — from VaultClient (user secret or global)
    3. OAUTH2 — cached token or refresh
    4. OAUTH2_DCR — Dynamic Client Registration
    5. HITL — raise AuthResolutionError to trigger interrupt
    """

    def __init__(self, vault: Any) -> None:
        self._vault = vault

    async def resolve(
        self,
        agent_id: str,
        user_id: str,
        auth_strategies: list[dict[str, Any]],
        *,
        scoped_only: bool = False,
    ) -> dict[str, str]:
        """
        Return HTTP headers dict with resolved credentials.

        ``scoped_only`` reads token strategies from the agent-scoped vault key
        only (see ``read_credential``); PreFlight sets it for connections.

        Raises AuthResolutionError if all strategies fail (triggers HITL).
        """
        if not auth_strategies:
            return {}

        for strategy in auth_strategies:
            # The Registry serves strategy types lower-case (``http_bearer``)
            # and the branches below match upper-case names, so a registered
            # agent's token was never read. Normalised for the two token
            # strategies only: the OAuth branches keep their exact match.
            strategy_type = str(strategy.get("type") or "")
            if strategy_type.upper() in _TOKEN_STRATEGIES:
                strategy_type = strategy_type.upper()
            config = strategy.get("config", {})
            try:
                headers = await self._try_strategy(
                    strategy_type, config, agent_id, user_id, scoped_only=scoped_only
                )
                if headers is not None:
                    return headers
            except Exception:
                logger.debug("Auth strategy %r failed for %s", strategy_type, agent_id)
                continue

        raise AuthResolutionError(f"All auth strategies exhausted for agent {agent_id}")

    async def _try_strategy(
        self,
        strategy_type: str,
        config: dict[str, Any],
        agent_id: str,
        user_id: str,
        *,
        scoped_only: bool = False,
    ) -> dict[str, str] | None:
        if strategy_type == "X_API_KEY":
            key_ref = token_ref({"type": strategy_type, "config": config})
            if key_ref:
                secret = await read_credential(
                    self._vault, user_id, agent_id, key_ref, scoped_only=scoped_only
                )
                if secret:
                    header_name = config.get("header_name", "X-Api-Key")
                    return {header_name: secret}

        elif strategy_type == "HTTP_BEARER":
            key_ref = token_ref({"type": strategy_type, "config": config})
            if key_ref:
                token = await read_credential(
                    self._vault, user_id, agent_id, key_ref, scoped_only=scoped_only
                )
                if token:
                    return {"Authorization": f"Bearer {token}"}

        elif strategy_type == "OAUTH2":
            return await self._oauth2(config, agent_id, user_id)

        elif strategy_type == "OAUTH2_DCR":
            return await self._oauth2_dcr(config, agent_id, user_id)

        elif strategy_type in ("PLATFORM_ENV", "platform_env"):
            # Credentials are already in SuperAgent os.environ.
            # MCPHandler passes {**os.environ} to the subprocess — no headers needed.
            return {}

        return None

    async def _oauth2(
        self,
        config: dict[str, Any],
        agent_id: str,
        user_id: str,
    ) -> dict[str, str] | None:
        """Return bearer token from cache or refresh."""
        cache_key = (agent_id, user_id)
        cached = _oauth_cache.get(cache_key)
        if cached:
            token, expiry = cached
            if time.monotonic() < expiry - 60:  # 60s buffer
                return {"Authorization": f"Bearer {token}"}

        # Refresh using stored refresh token
        import httpx

        refresh_token = await self._vault.get_user_secret(
            user_id, config.get("refresh_token_key", f"{agent_id}_refresh_token")
        )
        if not refresh_token:
            return None

        token_url = config.get("token_url", "")
        client_id = config.get("client_id", "")
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                token_url,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": client_id,
                },
            )
            resp.raise_for_status()
            data = resp.json()

        access_token = data["access_token"]
        expires_in = int(data.get("expires_in", 3600))
        _oauth_cache[cache_key] = (access_token, time.monotonic() + expires_in)
        return {"Authorization": f"Bearer {access_token}"}

    async def _oauth2_dcr(
        self,
        config: dict[str, Any],
        agent_id: str,
        user_id: str,
    ) -> dict[str, str] | None:
        """Dynamic Client Registration flow."""
        import httpx

        reg = await self._vault.get_agent_registration(agent_id, user_id)
        if not reg:
            return None

        client_id = reg.get("client_id")
        client_secret = reg.get("client_secret")
        token_url = config.get("token_url", "")

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
            )
            resp.raise_for_status()
            data = resp.json()

        return {"Authorization": f"Bearer {data['access_token']}"}
