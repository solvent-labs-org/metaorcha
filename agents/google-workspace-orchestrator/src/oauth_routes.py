"""OAuth callback — Google redirects to agent, agent exchanges token and resumes session."""

from __future__ import annotations

import logging
import re

import httpx
from fastapi import APIRouter, HTTPException, Query

from .config import settings
from .token_store import log_ref, put_tokens

logger = logging.getLogger(__name__)

# A session id is one URL path segment (sessions are UUIDs). The state
# arrives on the callback's query string, so anything else is refused before it
# reaches the resume URL.
_SESSION_ID = r"[A-Za-z0-9_-]{1,128}"

router = APIRouter(prefix="/auth", tags=["auth"])


def _parse_state(state: str) -> tuple[str | None, str | None]:
    """
    Parse the superagent OAuth state format: "<session_id>:<agent_id>:<nonce>".

    agent_id is a DID that contains colons (e.g. did:orcha:agent:name), so we
    cannot use a fixed maxsplit.  Convention: session_id is the first segment,
    nonce is the last segment, and everything in between is the agent_id.
    """
    parts = state.split(":")
    if len(parts) < 3:
        return None, None
    session_id = parts[0]
    agent_id = ":".join(parts[1:-1])
    if agent_id and re.fullmatch(_SESSION_ID, session_id):
        return session_id, agent_id
    return None, None


@router.get("/callback")
async def oauth_callback(
    code: str | None = Query(default=None),
    state: str | None = Query(default=None, description="OAuth state from preflight auth URL"),
    error: str | None = Query(default=None),
) -> dict[str, str]:
    if error:
        raise HTTPException(status_code=400, detail=f"OAuth error: {error}")
    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing code or state")
    if not settings.google_oauth_client_id or not settings.google_oauth_client_secret:
        raise HTTPException(status_code=503, detail="Google OAuth client not configured on agent")
    if not settings.google_oauth_redirect_uri:
        raise HTTPException(
            status_code=503,
            detail="GOOGLE_OAUTH_REDIRECT_URI must match the redirect_uri used in the authorize step",
        )

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code,
                "client_id": settings.google_oauth_client_id,
                "client_secret": settings.google_oauth_client_secret,
                "redirect_uri": settings.google_oauth_redirect_uri,
                "grant_type": "authorization_code",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if resp.status_code != 200:
            logger.error("token_exchange_failed body=%s", resp.text[:500])
            raise HTTPException(status_code=400, detail="Token exchange failed")

        data = resp.json()

    access = data.get("access_token")
    if not access:
        raise HTTPException(status_code=400, detail="No access_token in token response")
    refresh = data.get("refresh_token")
    expires_in = data.get("expires_in")
    session_id, agent_id = _parse_state(state)
    token_key = session_id or state
    # Never log token material — key + shape only.
    logger.info(
        "token_exchange_ok key=%s has_refresh=%s expires_in=%s scope=%s",
        log_ref(token_key),
        bool(refresh),
        expires_in,
        data.get("scope", "")[:120],
    )
    put_tokens(token_key, access_token=access, refresh_token=refresh, expires_in=expires_in)
    logger.info(
        "oauth_tokens_stored key=%s agent_id=%s has_refresh=%s expires_in=%s",
        log_ref(token_key),
        agent_id or "",
        bool(refresh),
        expires_in,
    )

    if not session_id or not agent_id:
        return {"status": "ok", "message": "Tokens stored; state had no resumable session metadata"}

    resume_url = (
        f"{settings.gateway_base_url.rstrip('/')}/auth/sessions/"
        f"{session_id}/resume-agent-oauth"
    )
    async with httpx.AsyncClient(timeout=20.0) as client:
        resume_resp = await client.post(
            resume_url,
            json={"agent_id": agent_id, "status": "ok"},
        )
        if resume_resp.status_code >= 300:
            logger.error(
                "oauth_resume_failed status=%s body=%s",
                resume_resp.status_code,
                resume_resp.text[:500],
            )
            raise HTTPException(status_code=502, detail="Token stored, but failed to resume session")

    return {"status": "ok", "message": "Tokens stored and session resumed"}
