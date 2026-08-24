"""SandboxGuard — lightweight request guard for the hosted sandbox.

Active only when SANDBOX_MODE=true. Enforces two limits via Redis:

1. Daily message cap (SANDBOX_MAX_DAILY_MESSAGES, default 500)
   Counts POST requests to /api/v1/sessions/*/message per UTC day.
   Returns 429 when exceeded so the sandbox doesn't run up an open tab.

2. Per-IP rate: handled upstream by nginx (10 req/min). This guard is
   belt-and-suspenders at the app layer.

Usage: mounted as ASGI middleware in main.py when SANDBOX_MODE=true.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime

from jose import JWTError
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from .auth.jwt import decode_access_token

logger = logging.getLogger(__name__)

_MAX_DAILY = int(os.getenv("SANDBOX_MAX_DAILY_MESSAGES", "500"))
_GUEST_MAX = int(os.getenv("SANDBOX_GUEST_MAX_MESSAGES", "1"))
# Guests who bring their own model key (session-scoped __llm__ creds) cost us
# no LLM spend — they get a higher cap.
_GUEST_BYOK_MAX = int(os.getenv("SANDBOX_GUEST_BYOK_MAX_MESSAGES", "10"))

_CAP_BODY = {
    "detail": "Sandbox daily limit reached. The demo resets at midnight UTC. "
    "Run locally for unlimited access: https://github.com/solvent-labs-org/metaorcha",
    "code": "SANDBOX_DAILY_LIMIT",
}

_GUEST_CAP_BODY = {
    "detail": "Guest limit reached. Bring your own model key for a higher limit, "
    "sign up, or run locally for unlimited access: https://github.com/solvent-labs-org/metaorcha",
    "code": "SANDBOX_GUEST_LIMIT",
}


def _is_message_request(path: str, method: str) -> bool:
    """True for POST /api/v1/sessions/<id>/message."""
    if method != "POST":
        return False
    parts = path.strip("/").split("/")
    # api / v1 / sessions / <id> / message
    return (
        len(parts) == 5
        and parts[0] == "api"
        and parts[1] == "v1"
        and parts[2] == "sessions"
        and parts[4] == "message"
    )


class SandboxGuardMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        if not _is_message_request(request.url.path, request.method):
            return await call_next(request)

        redis = getattr(request.app.state, "redis", None)
        if redis is None:
            return await call_next(request)

        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
            try:
                payload = decode_access_token(token)
                if payload.is_guest:
                    session_id = request.url.path.strip("/").split("/")[3]
                    guest_cap = _GUEST_MAX
                    try:
                        byok_key = f"gateway:creds:session:{session_id}:__llm__:api_key"
                        if await redis.get(byok_key):
                            guest_cap = _GUEST_BYOK_MAX
                    except Exception:
                        logger.debug(
                            "SandboxGuard: BYOK check failed — default cap",
                            exc_info=True,
                        )
                    guest_key = f"sandbox:guest:{payload.user_id}:messages"
                    try:
                        guest_count = await redis.incr(guest_key)
                        if guest_count == 1:
                            await redis.expire(guest_key, 86400)
                        if guest_count > guest_cap:
                            return JSONResponse(
                                status_code=429, content=_GUEST_CAP_BODY
                            )
                    except Exception:
                        logger.debug(
                            "SandboxGuard: guest limit check failed — bypassing",
                            exc_info=True,
                        )
            except (ValueError, JWTError):
                pass

        today = datetime.now(UTC).strftime("%Y-%m-%d")
        key = f"sandbox:messages:{today}"

        try:
            count = await redis.incr(key)
            if count == 1:
                await redis.expire(key, 90000)  # 25h — covers DST edge
            if count > _MAX_DAILY:
                logger.warning(
                    "Sandbox daily message cap reached: %d/%d", count, _MAX_DAILY
                )
                return JSONResponse(status_code=429, content=_CAP_BODY)
        except Exception:
            logger.debug(
                "SandboxGuard: Redis unavailable — bypassing cap", exc_info=True
            )

        return await call_next(request)
