"""PreFlightManager — health check + auth resolution before handler dispatch."""

from __future__ import annotations

import logging
import os
import re
import secrets
import time
from typing import Any

from internal_commons.interrupts import (
    AgentOAuthCallbackMetadata,
    AuthFormSubmissionMetadata,
    InterruptEvent,
    InterruptType,
)

from .auth_manager import AuthManager, AuthResolutionError, read_credential, token_ref
from .connections import (
    CONNECTIONS_DISABLED,
    connections_enabled,
    credential_missing,
    is_connection,
)
from .manifest_cache import MANIFEST_CACHE
from .oauth_grants import (
    capability_grant_key as _redis_cap_key,
)
from .oauth_grants import (
    has_grant as _redis_has_grant,
)

logger = logging.getLogger(__name__)

# Health check cache: agent_id → (is_healthy, ts)
_health_cache: dict[str, tuple[bool, float]] = {}
_HEALTH_TTL = 30.0  # seconds

_PLACEHOLDER_RE = re.compile(r"\$\{([^}]+)\}")

# AD-13: ``platform_env`` (a secret read from the SuperAgent process env) is a
# platform-tool mechanism. Only ``did:orcha:system:*`` manifests may use it; a
# user-registered agent naming ``VAULT_KEY`` as its env_key gets nothing.
PLATFORM_DID_PREFIX = "did:orcha:system:"
ENV_UNRESOLVED = "env_unresolved"


def platform_env_allowed(agent_id: str) -> bool:
    return isinstance(agent_id, str) and agent_id.startswith(PLATFORM_DID_PREFIX)


def _oauth_grant_key(agent_id: str, capability_id: str) -> str:
    """In-memory session-state key (mirrors Redis capability_grant_key)."""
    from .oauth_grants import capability_grant_key

    return capability_grant_key(agent_id, capability_id)


def _oauth_config_keys_from_strategies(
    agent_id: str,
    auth_strategies: list[dict[str, Any]],
) -> list[str]:
    """Build config-level OAuth grant keys for all OAuth strategies in scope."""
    from .oauth_grants import config_grant_key

    keys: list[str] = []
    for strategy in auth_strategies:
        stype = (strategy.get("type") or "").upper()
        if stype not in ("OAUTH2", "OAUTH2_DCR"):
            continue
        cfg = strategy.get("config", {}) or {}
        provider = cfg.get("provider_hint", "")
        scopes = cfg.get("scopes", []) or []
        keys.append(config_grant_key(agent_id, provider, list(scopes)))
    return keys


class PreFlightError(Exception):
    """Raised when pre-flight checks fail unrecoverably (hard failure)."""


class AuthInterruptRequired(Exception):
    """
    Raised by PreFlightManager when auth cannot be resolved automatically
    and user interaction is needed to proceed.

    This is a SOFT, RESUMABLE pause — not a hard failure.
    The calling execution node must catch this and call langgraph.interrupt()
    with the attached InterruptEvent payload.

    Distinct from PreFlightError, which signals a hard, non-resumable failure
    (e.g. agent is unhealthy, manifest is corrupt).
    """

    def __init__(self, event: InterruptEvent) -> None:
        self.event = event
        super().__init__(event.message)


class PreFlightManager:
    """Orchestrates manifest fetch, health check, auth resolution, and env resolution."""

    def __init__(self, vault: Any) -> None:
        self._vault = vault
        self._auth = AuthManager(vault)

    async def run(
        self,
        agent_id: str,
        user_id: str,
        capability_id: str,
        tool_name: str,
        state: dict[str, Any],
        session_credentials: dict[str, dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        """
        Run pre-flight checks.

        Returns:
            dict with keys:
                "manifest"      — agent manifest dict
                "headers"       — resolved auth headers dict
                "resolved_env"  — resolved env dict for STDIO agents, else None

        Raises:
            AuthInterruptRequired — auth cannot be resolved; LangGraph must interrupt.
            PreFlightError        — hard failure (unhealthy agent, missing env creds, etc.).
        """
        # 1. Fetch manifest
        manifest = await MANIFEST_CACHE.get_manifest(agent_id)
        transport_type = manifest.get("transport", {}).get("type", "").upper()

        # 1.5 Connections (story 1.3): refused while the feature is off, and a
        #     missing token fails here, before the health probe below sends
        #     anything to the platform.
        if is_connection(manifest):
            await self._assert_connection_callable(
                agent_id, user_id, manifest, capability_id
            )

        # 2. Health check — skipped for STDIO agents (they are spawned as subprocesses,
        #    not long-running HTTP services; there is no endpoint to probe).
        if transport_type != "STDIO":
            await self._assert_healthy(agent_id)

        # 3. Find applicable auth strategies (capability-scoped priority)
        auth_strategies = self._extract_auth_strategies(manifest, capability_id)

        # 4. Resolve auth — raises AuthInterruptRequired if user action needed
        headers: dict[str, str] = {}

        # Build the candidate grant keys for this capability + strategy config.
        oauth_config_keys = _oauth_config_keys_from_strategies(
            agent_id, auth_strategies
        )
        scoped_key = _oauth_grant_key(agent_id, capability_id)
        global_key = _oauth_grant_key(agent_id, "")

        # Check Redis first (24-hour TTL, cross-session) then fall back to the
        # in-memory session state set immediately after a resume in the same
        # node execution.
        redis_grant_keys = [
            _redis_cap_key(agent_id, capability_id),
            _redis_cap_key(agent_id, ""),
        ] + oauth_config_keys
        has_redis_grant = await _redis_has_grant(user_id, redis_grant_keys)

        oauth_grants: dict[str, bool] = state.get("_agent_oauth_grants", {})
        has_config_grant = any(oauth_grants.get(k) for k in oauth_config_keys)
        has_inmem_grant = (
            bool(oauth_grants.get(scoped_key))
            or bool(oauth_grants.get(global_key))
            or has_config_grant
        )

        if has_redis_grant or has_inmem_grant:
            logger.info(
                "oauth_grant_hit agent=%s capability=%s redis=%s in_memory=%s",
                agent_id,
                capability_id or "*",
                has_redis_grant,
                has_inmem_grant,
            )
        else:
            try:
                headers = await self._auth.resolve(agent_id, user_id, auth_strategies)
            except AuthResolutionError:
                logger.info(
                    "Auth resolution failed for %s — raising AuthInterruptRequired",
                    agent_id,
                )
                session_id = state.get("session_id", "")
                interrupt_event = await self._build_auth_interrupt_event(
                    manifest=manifest,
                    agent_id=agent_id,
                    capability_id=capability_id,
                    tool_name=tool_name,
                    session_id=session_id,
                    auth_strategies=auth_strategies,
                )
                raise AuthInterruptRequired(interrupt_event) from None

        # 5. Resolve env vars for STDIO agents (transport.env + platform_env host keys)
        resolved_env: dict[str, str] | None = None
        if transport_type == "STDIO":
            raw_env: dict[str, str] = dict(
                manifest.get("transport", {}).get("env") or {}
            )
            # emerge.yaml platform secrets: security.auth_strategies[].type=platform_env
            # with config.env_key — value comes from SuperAgent process env (not transport.env).
            for strategy in (manifest.get("security", {}) or {}).get(
                "auth_strategies", []
            ) or []:
                st = (strategy.get("type") or "").lower()
                if st != "platform_env":
                    continue
                env_key = (strategy.get("config") or {}).get("env_key") or ""
                if not env_key:
                    continue
                if not platform_env_allowed(agent_id):
                    logger.warning(
                        "platform_env strategy %r ignored for agent=%s: platform "
                        "secrets are for %s* manifests only (AD-13)",
                        strategy.get("id", ""),
                        agent_id,
                        PLATFORM_DID_PREFIX,
                    )
                    continue
                val = os.environ.get(env_key)
                if val is not None and val != "":
                    raw_env[env_key] = val
                else:
                    logger.warning(
                        "platform_env strategy %r: %s not set in SuperAgent process env "
                        "(agent=%s) — MCP subprocess may lack this credential",
                        strategy.get("id", ""),
                        env_key,
                        agent_id,
                    )

            if raw_env:
                resolved_env = await self._resolve_stdio_env(
                    agent_id=agent_id,
                    user_id=user_id,
                    raw_env=raw_env,
                    session_credentials=session_credentials,
                )

        return {
            "manifest": manifest,
            "headers": headers,
            "resolved_env": resolved_env,
        }

    async def _assert_connection_callable(
        self,
        agent_id: str,
        user_id: str,
        manifest: dict[str, Any],
        capability_id: str,
    ) -> None:
        """Refuse a connection call that cannot succeed, before any request.

        Raises PreFlightError (a named hard failure, not an auth interrupt):
        ``connections_disabled`` while CONNECTIONS_ENABLED is off, or
        ``credential_missing`` when the connection has token strategies and
        the caller's vault holds none of their tokens.
        """
        if not connections_enabled():
            raise PreFlightError(CONNECTIONS_DISABLED)
        refs = [
            ref
            for ref in (
                token_ref(s)
                for s in self._extract_auth_strategies(manifest, capability_id)
            )
            if ref
        ]
        if not refs:
            return
        for ref in refs:
            if await read_credential(self._vault, user_id, agent_id, ref):
                return
        raise PreFlightError(credential_missing(refs[0]))

    # ── Auth interrupt construction ───────────────────────────────────────────

    async def _build_auth_interrupt_event(
        self,
        *,
        manifest: dict[str, Any],
        agent_id: str,
        capability_id: str,
        tool_name: str,
        session_id: str,
        auth_strategies: list[dict[str, Any]],
    ) -> InterruptEvent:
        """
        Construct the correct InterruptEvent based on what auth strategies
        were attempted and which type of auth is needed.
        """
        display_name: str = manifest.get("name", agent_id)
        nonce = secrets.token_urlsafe(8)
        interrupt_id = f"{agent_id}__{capability_id}__{nonce}"

        failed_types = {s.get("type", "").upper() for s in auth_strategies}

        if "OAUTH2" in failed_types or "OAUTH2_DCR" in failed_types:
            # Find the first OAuth strategy to get its config
            oauth_strategy = next(
                (
                    s
                    for s in auth_strategies
                    if s.get("type", "").upper() in ("OAUTH2", "OAUTH2_DCR")
                ),
                {},
            )
            cfg = oauth_strategy.get("config", {})
            transport_endpoint = manifest.get("transport", {}).get("endpoint", "")
            metadata = AgentOAuthCallbackMetadata(
                auth_url=await self._build_auth_url(
                    cfg, session_id, agent_id, nonce, transport_endpoint
                ),
                provider_name=cfg.get("provider_hint", display_name),
                scopes=cfg.get("scopes", []),
                agent_id=agent_id,
            )
            return InterruptEvent(
                interrupt_type=InterruptType.AGENT_OAUTH_CALLBACK,
                interrupt_id=interrupt_id,
                agent_id=agent_id,
                session_id=session_id,
                message=f"Authorization required for {display_name}",
                metadata=metadata.model_dump(),
            )

        # X_API_KEY or HTTP_BEARER — credential form submission
        strategy = auth_strategies[0] if auth_strategies else {}
        cfg = strategy.get("config", {})
        vault_key = (
            cfg.get("key_vault_ref") or cfg.get("token_vault_ref") or f"{agent_id}_key"
        )
        metadata = AuthFormSubmissionMetadata(
            form_title=f"Credential Required — {display_name}",
            field_label=cfg.get("header_name", "API Key"),
            vault_key=vault_key,
            is_secret=True,
            agent_display_name=display_name,
        )
        return InterruptEvent(
            interrupt_type=InterruptType.AUTH_FORM_SUBMISSION,
            interrupt_id=interrupt_id,
            agent_id=agent_id,
            session_id=session_id,
            message=f"Credential required for {display_name}",
            metadata=metadata.model_dump(),
        )

    async def _build_auth_url(
        self,
        cfg: dict[str, Any],
        session_id: str,
        agent_id: str,
        nonce: str,
        transport_endpoint: str = "",
    ) -> str:
        """Build an OAuth authorization URL from config.

        If client_id contains an unresolved placeholder (${VAR}) the superagent
        cannot expand — because the credential belongs to the agent, not the
        superagent — this method falls back to calling the agent's own OAuth
        connect endpoint, which knows its own credentials.
        """
        import urllib.parse

        def _resolve(value: str) -> str:
            # AD-13: a ``${VAR}`` is never expanded from the host environment.
            # Left in place, the placeholder routes to the agent's own connect
            # endpoint below, which holds its own credentials.
            return value

        base = cfg.get("authorization_url", "")
        if not base:
            return ""

        client_id = _resolve(cfg.get("client_id", ""))

        # Unresolved placeholder — delegate to the agent's own connect endpoint.
        if "${" in client_id and transport_endpoint:
            agent_url = await self._fetch_agent_auth_url(
                transport_endpoint, cfg, session_id
            )
            if agent_url:
                return agent_url

        params: dict[str, str] = {
            "client_id": client_id,
            "response_type": "code",
            "scope": " ".join(cfg.get("scopes", [])),
            "state": f"{session_id}:{agent_id}:{nonce}",
        }
        redirect = cfg.get("redirect_uri", "")
        if redirect:
            params["redirect_uri"] = _resolve(redirect)
        return f"{base}?{urllib.parse.urlencode(params)}"

    async def _fetch_agent_auth_url(
        self,
        transport_endpoint: str,
        cfg: dict[str, Any],
        session_id: str,
    ) -> str:
        """Call the agent's own OAuth connect endpoint to get a valid auth URL."""
        import httpx

        # Map scopes/provider_hint to the agent's connect path.
        scopes_str = " ".join(cfg.get("scopes", [])).lower()
        provider_hint = cfg.get("provider_hint", "").lower()

        if "gmail" in scopes_str:
            path = "gmail"
        elif "spreadsheets" in scopes_str or "drive.file" in scopes_str:
            path = "gsheets"
        elif provider_hint == "notion":
            path = "notion"
        elif provider_hint == "microsoft":
            path = "excel"
        else:
            logger.warning(
                "Cannot determine OAuth connect path for provider=%s scopes=%s",
                provider_hint,
                scopes_str[:80],
            )
            return ""

        connect_url = f"{transport_endpoint.rstrip('/')}/oauth/{path}/connect"
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(connect_url, params={"tenant_id": session_id})
                resp.raise_for_status()
                return resp.json().get("auth_url", "")
        except Exception:
            logger.warning(
                "Failed to fetch auth URL from agent connect endpoint %s",
                connect_url,
                exc_info=True,
            )
            return ""

    # ── Env resolution ────────────────────────────────────────────────────────

    async def _resolve_stdio_env(
        self,
        agent_id: str,
        user_id: str,
        raw_env: dict[str, str],
        session_credentials: dict[str, dict[str, str]] | None = None,
    ) -> dict[str, str]:
        """
        Resolve ``${VAR}`` placeholders in a STDIO agent's transport.env dict.

        Resolution order:
            1. Session-scoped credentials (injected per-turn by Gateway)
            2. User vault  (per-user stored credentials in UserSecret table)
            3. Host env    (process environment — for system-level / shared vars)

        Raises:
            PreFlightError: if any placeholder cannot be resolved from any source.
        """
        resolved: dict[str, str] = {}
        missing: list[str] = []

        for env_key, template in raw_env.items():
            placeholders = _PLACEHOLDER_RE.findall(template)

            if not placeholders:
                resolved[env_key] = template
                continue

            value = template
            for var_name in placeholders:
                # 0. Session-scoped credentials (highest priority)
                agent_session_creds = (session_credentials or {}).get(agent_id, {})
                if var_name in agent_session_creds:
                    secret: str | None = agent_session_creds[var_name]
                    logger.debug(
                        "Resolved ${%s} for agent=%s from session credentials",
                        var_name,
                        agent_id,
                    )
                    value = value.replace(f"${{{var_name}}}", secret)
                    continue

                # 1. User vault
                secret = await self._vault.get_agent_env(user_id, agent_id, var_name)
                if secret is not None:
                    logger.debug(
                        "Resolved ${%s} for agent=%s user=%s from vault",
                        var_name,
                        agent_id,
                        user_id,
                    )
                else:
                    # AD-13: no host-environment fallback. A ``${VAR}`` the
                    # vault cannot resolve fails the call as env_unresolved.
                    logger.error(
                        "%s: ${%s} for agent=%s user=%s is not in the vault "
                        "(the host environment is never consulted)",
                        ENV_UNRESOLVED,
                        var_name,
                        agent_id,
                        user_id,
                    )
                    missing.append(var_name)
                    continue

                value = value.replace(f"${{{var_name}}}", secret)

            resolved[env_key] = value

        if missing:
            raise PreFlightError(
                f"{ENV_UNRESOLVED}: agent {agent_id!r} requires credentials that "
                f"are not in the vault for user {user_id!r}. Missing env vars: "
                f"{missing}. Store them via POST /secrets/agent-env."
            )

        return resolved

    # ── Health check ──────────────────────────────────────────────────────────

    async def _assert_healthy(self, agent_id: str) -> None:
        cached = _health_cache.get(agent_id)
        if cached:
            is_healthy, ts = cached
            if time.monotonic() - ts < _HEALTH_TTL:
                if not is_healthy:
                    raise PreFlightError(f"Agent {agent_id} is unhealthy (cached)")
                return

        manifest = await MANIFEST_CACHE.get_manifest(agent_id)
        is_healthy = manifest.get("health_status", "").upper() == "HEALTHY"
        _health_cache[agent_id] = (is_healthy, time.monotonic())
        if not is_healthy:
            raise PreFlightError(f"Agent {agent_id} health_status is not HEALTHY")

    # ── Auth strategy extraction ───────────────────────────────────────────────

    @staticmethod
    def _extract_auth_strategies(
        manifest: dict[str, Any],
        capability_id: str,
    ) -> list[dict[str, Any]]:
        """
        Extract auth strategies from manifest, using capability-scoped priority.

        Resolution order:
        1. Strategies that explicitly scope to this capability_id (highest priority).
           If any exist, only these are returned — global strategies are NOT added.
        2. Global strategies — capability_ids is empty or absent.
           Returned when no scoped strategies match.

        Strategies scoped to OTHER capabilities are never evaluated.
        This prevents unnecessary auth prompts when invoking a capability
        that doesn't require a particular credential.
        """
        security = manifest.get("security", {})
        all_strategies: list[dict[str, Any]] = security.get("auth_strategies", [])

        if capability_id:
            scoped = [
                s
                for s in all_strategies
                if capability_id in (s.get("capability_ids") or [])
            ]
            if scoped:
                return scoped

        # No scoped strategies matched — return globals only
        return [s for s in all_strategies if not (s.get("capability_ids") or [])]
