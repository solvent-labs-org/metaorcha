"""JWT helpers — issue and decode access + refresh tokens."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt

from ..config import settings
from .models import TokenPayload


def _now() -> datetime:
    return datetime.now(UTC)


def create_access_token(
    user_id: str,
    email: str,
    *,
    guest: bool = False,
    expire_minutes: int | None = None,
) -> tuple[str, str]:
    """Return (encoded_jwt, jti). JTI is used for token revocation."""
    jti = str(uuid.uuid4())
    if expire_minutes is not None:
        exp = _now() + timedelta(minutes=expire_minutes)
    elif guest:
        exp = _now() + timedelta(hours=24)
    else:
        exp = _now() + timedelta(days=settings.jwt_access_token_expire_days)
    payload: dict[str, Any] = {
        "sub": user_id,
        "email": email,
        "jti": jti,
        "iat": _now(),
        "exp": exp,
        "type": "access",
    }
    if guest:
        payload["guest"] = True
    encoded = jwt.encode(
        payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm
    )
    return encoded, jti


def create_refresh_token() -> tuple[str, str]:
    """Return (raw_token, sha256_hex_hash).

    The raw token is returned once to the client; the hash is stored in Postgres.
    """
    raw = str(uuid.uuid4())
    token_hash = hashlib.sha256(raw.encode()).hexdigest()
    return raw, token_hash


def decode_access_token(token: str) -> TokenPayload:
    """Decode and validate an access token.

    Raises ValueError on invalid, expired, or wrong-type tokens.
    """
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
        )
    except jwt.PyJWTError as exc:
        raise ValueError(f"Invalid token: {exc}") from exc
    if payload.get("type") != "access":
        raise ValueError("Not an access token")
    return TokenPayload(
        user_id=payload["sub"],
        email=payload["email"],
        jti=payload["jti"],
        is_guest=bool(payload.get("guest")),
    )
