"""Auth endpoints: register, login, refresh, logout, sandbox guest, and OAuth callbacks."""

from __future__ import annotations

import hashlib
import logging
import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import bcrypt as _bcrypt
from fastapi import APIRouter, Depends, HTTPException, Request, status
from internal_commons.interrupts.resume import ResumePayload
from internal_commons.interrupts.types import InterruptType
from pydantic import BaseModel

from ..config import settings
from ..dependencies import require_auth
from .jwt import create_access_token, create_refresh_token
from .models import LoginRequest, RefreshRequest, RegisterRequest, TokenResponse

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])


def _hash_password(password: str) -> str:
    return _bcrypt.hashpw(password.encode(), _bcrypt.gensalt()).decode()


def _verify_password(password: str, password_hash: str) -> bool:
    return _bcrypt.checkpw(password.encode(), password_hash.encode())


async def _issue_tokens(
    user_id: str,
    email: str,
    db: Any,
    redis: Any,
) -> TokenResponse:
    """Create access + refresh tokens, persist refresh token record."""
    access_token, jti = create_access_token(user_id, email)
    raw_refresh, refresh_hash = create_refresh_token()
    expires_at = datetime.now(UTC) + timedelta(
        days=settings.jwt_refresh_token_expire_days
    )
    await db.refreshtoken.create(
        data={
            "user_id": user_id,
            "token_hash": refresh_hash,
            "expires_at": expires_at,
        }
    )
    return TokenResponse(access_token=access_token, refresh_token=raw_refresh)


@router.get("/guest", response_model=TokenResponse)
async def sandbox_guest(request: Request) -> TokenResponse:
    """No-auth first session for hosted sandbox (SANDBOX_MODE=true only).

    Issues a short-lived guest JWT. Guest users may send one message per account
    (enforced by SandboxGuardMiddleware). No refresh token — call again for a new guest.
    """
    if os.getenv("SANDBOX_MODE", "").lower() != "true":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    db = request.app.state.db
    guest_id = uuid.uuid4().hex[:12]
    email = f"guest-{guest_id}@sandbox.orcha.local"
    user = await db.user.create(
        data={
            "email": email,
            "display_name": "Sandbox Guest",
            "password_hash": None,
        }
    )
    if settings.payment_mode == "mock":
        try:
            await db.user.update(
                where={"id": user.id},
                data={"credits_usd": 5000.0},
            )
        except Exception:
            logger.exception("Failed to seed guest credits for user=%s", user.id)

    access_token, _jti = create_access_token(user.id, email, guest=True)
    logger.info("Sandbox guest session issued: user=%s", user.id)
    return TokenResponse(access_token=access_token, refresh_token="")


@router.get("/local", response_model=TokenResponse)
async def local_login(request: Request) -> TokenResponse:
    """Frictionless login for a self-hosted local instance (LOCAL_MODE=true only).

    Unlike the throwaway, message-capped sandbox guest, this find-or-creates a
    single fixed local user so a single-operator instance keeps its chat history
    and wallet balance across restarts. Issues a full (persistent) access +
    refresh token pair.
    """
    if os.getenv("LOCAL_MODE", "").lower() != "true":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    db = request.app.state.db
    email = "local@orcha.local"
    user = await db.user.find_unique(where={"email": email})
    if user is None:
        user = await db.user.create(
            data={
                "email": email,
                "display_name": "Local User",
                "password_hash": None,
            }
        )
        logger.info("Local-mode user created: user=%s", user.id)
        # Seed mock credits on creation ONLY — never reset an existing balance,
        # so a returning local operator keeps whatever they've spent/earned.
        if settings.payment_mode == "mock":
            try:
                await db.user.update(
                    where={"id": user.id},
                    data={"credits_usd": 5000.0},
                )
            except Exception:
                logger.exception(
                    "Failed to seed local-user credits for user=%s", user.id
                )

    return await _issue_tokens(user.id, user.email, db, request.app.state.redis)


@router.post("/register", response_model=TokenResponse, status_code=201)
async def register(body: RegisterRequest, request: Request) -> TokenResponse:
    db = request.app.state.db
    existing = await db.user.find_unique(where={"email": body.email})
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already registered",
        )
    password_hash = _hash_password(body.password)
    user = await db.user.create(
        data={
            "email": body.email,
            "password_hash": password_hash,
            "display_name": body.display_name,
        }
    )
    logger.info("New user registered: %s", user.id)

    # Provision a smart wallet for the new user via wallet-service (non-blocking best-effort).
    # On failure the user account is still created; wallet can be provisioned lazily on /fund.
    try:
        import httpx

        async with httpx.AsyncClient() as _http:
            _resp = await _http.post(
                f"{settings.wallet_service_url}/wallet/create",
                json={
                    "chain": "base",
                    "display_name": body.display_name or body.email,
                },
                timeout=30.0,
            )
            _resp.raise_for_status()
            _wallet = _resp.json()

        await db.user.update(
            where={"id": user.id},
            data={
                "privy_wallet_id": _wallet["wallet_id"],
                "eoa_wallet_address": _wallet["eoa_address"],
                "wallet_address": _wallet["smart_wallet_address"],
            },
        )
        logger.info(
            "Smart wallet provisioned for user=%s smart=%s eoa=%s",
            user.id,
            _wallet["smart_wallet_address"],
            _wallet["eoa_address"],
        )
    except Exception:
        logger.exception(
            "Failed to provision smart wallet for user=%s — continuing without wallet",
            user.id,
        )

    # In payment_mode=mock (testing/dev), seed new users with 5000 USDC credits
    # so they can test the full pricing flow without real on-chain settlement.
    if settings.payment_mode == "mock":
        try:
            await db.user.update(
                where={"id": user.id},
                data={"credits_usd": 5000.0},
            )
            logger.info(
                "Seeded 5000 USDC credits for user=%s (payment_mode=mock)", user.id
            )
        except Exception:
            logger.exception("Failed to seed credits for user=%s — continuing", user.id)

    return await _issue_tokens(user.id, user.email, db, request.app.state.redis)


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, request: Request) -> TokenResponse:
    db = request.app.state.db
    user = await db.user.find_unique(where={"email": body.email})
    if not user or not user.password_hash:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
        )
    if not _verify_password(body.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
        )
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account inactive",
        )
    return await _issue_tokens(user.id, user.email, db, request.app.state.redis)


@router.post("/refresh", response_model=TokenResponse)
async def refresh_token(body: RefreshRequest, request: Request) -> TokenResponse:
    db = request.app.state.db
    token_hash = hashlib.sha256(body.refresh_token.encode()).hexdigest()
    record = await db.refreshtoken.find_unique(where={"token_hash": token_hash})
    if record is None or record.revoked:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh token",
        )
    if record.expires_at.replace(tzinfo=UTC) < datetime.now(UTC):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token expired",
        )
    # Rotate: revoke old token
    await db.refreshtoken.update(where={"id": record.id}, data={"revoked": True})
    user = await db.user.find_unique(where={"id": record.user_id})
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or inactive",
        )
    return await _issue_tokens(user.id, user.email, db, request.app.state.redis)


@router.post("/logout", status_code=204)
async def logout(
    request: Request,
    payload: Annotated[Any, Depends(require_auth)],
) -> None:
    redis = request.app.state.redis
    db = request.app.state.db
    # Add JTI to revocation set — expire the set after the access token lifetime
    ttl_seconds = settings.jwt_access_token_expire_days * 86400
    await redis.sadd("jwt:revoked", payload.jti)
    await redis.expire("jwt:revoked", ttl_seconds)
    # Revoke all refresh tokens for this user
    await db.refreshtoken.update_many(
        where={"user_id": payload.user_id, "revoked": False},
        data={"revoked": True},
    )


class _AgentOAuthResumeBody(BaseModel):
    agent_id: str
    status: str  # "ok" | "error"
    error_message: str | None = None


@router.post("/sessions/{session_id}/resume-agent-oauth")
async def resume_agent_oauth(
    session_id: str,
    body: _AgentOAuthResumeBody,
    request: Request,
) -> dict[str, bool]:
    """
    Called by the agent after it has exchanged the authorization code for a token
    and stored the credentials.  Resumes the LangGraph session.

    This endpoint is agent-to-gateway (server-to-server), not browser-facing.
    """
    sa = request.app.state.superagent

    resume_value: dict[str, Any] = {
        "status": body.status,
        "agent_id": body.agent_id,
    }
    if body.error_message:
        resume_value["error_message"] = body.error_message

    resume_payload = ResumePayload(
        interrupt_id=f"agent_oauth__{body.agent_id}",
        interrupt_type=InterruptType.AGENT_OAUTH_CALLBACK,
        value=resume_value,
    )

    try:
        resp = await sa.post(
            f"/sessions/{session_id}/resume",
            json={
                "user_id": "",  # SA validates ownership via session, not user_id here
                "value": resume_payload.value,
                "session_credentials": {},
            },
        )
        resp.raise_for_status()
    except Exception as exc:
        logger.error(
            "resume_agent_oauth: failed to resume session=%s — %s", session_id, exc
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not resume session",
        ) from exc

    return {"ok": True}
