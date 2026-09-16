"""Session routes — create, message, resume, status."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse

from ..auth.models import TokenPayload
from ..dependencies import require_auth
from .models import (
    CreateSessionBody,
    CreateSessionResponse,
    MessageRequest,
    PaginatedSessionsResponse,
    ResumeRequest,
    SessionStatusResponse,
    SessionStopResponse,
    TranscriptListResponse,
)
from .sse_relay import proxy_superagent_sse

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/sessions", tags=["sessions"])

_SESSION_TTL = 86400 * 30  # 30 days
_MAX_PAGE_SIZE = 50


async def _assert_session_owner(
    session_id: str, user_id: str, redis: Any, sa: Any | None = None
) -> None:
    """Raise 404/403 if the session does not belong to this user.

    Falls back to the superagent DB when the Redis ownership key is absent
    (e.g. after a Redis restart) and re-seeds it on success.
    """
    owner = await redis.get(f"gateway:session:{session_id}")
    if owner is None:
        if sa is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Session not found"
            )
        resp = await sa.get(f"/sessions/{session_id}")
        if resp.status_code == 404:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Session not found"
            )
        resp.raise_for_status()
        owner = resp.json()["user_id"]
        # Re-seed Redis so subsequent requests are fast
        await redis.set(f"gateway:session:{session_id}", owner, ex=_SESSION_TTL)
    if owner != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Session does not belong to this user",
        )


async def _get_session_credentials(
    session_id: str, redis: Any
) -> dict[str, dict[str, str]]:
    """
    Assemble session-scoped credentials from Redis keys:
        gateway:creds:session:{session_id}:{agent_id}:{var_name} → value
    Returns dict[agent_id, dict[var_name, value]].
    """
    pattern = f"gateway:creds:session:{session_id}:*"
    keys = await redis.keys(pattern)
    result: dict[str, dict[str, str]] = {}
    for key in keys:
        # key format: gateway:creds:session:{sid}:{agent_id}:{var_name}
        parts = key.split(":", 5)
        if len(parts) < 6:
            continue
        agent_id, var_name = parts[4], parts[5]
        value = await redis.get(key)
        if value is not None:
            result.setdefault(agent_id, {})[var_name] = value
    return result


@router.post("", response_model=CreateSessionResponse, status_code=201)
async def create_session(
    request: Request,
    payload: Annotated[TokenPayload, Depends(require_auth)],
    body: CreateSessionBody = Body(default_factory=CreateSessionBody),
) -> CreateSessionResponse:
    sa = request.app.state.superagent
    b = body
    resp = await sa.post(
        "/sessions",
        json={"user_id": payload.user_id, "title": b.title},
    )
    resp.raise_for_status()
    session_id: str = resp.json()["session_id"]
    # Index session → user ownership in Redis
    await request.app.state.redis.set(
        f"gateway:session:{session_id}", payload.user_id, ex=_SESSION_TTL
    )
    return CreateSessionResponse(session_id=session_id)


@router.get("", response_model=PaginatedSessionsResponse)
async def list_sessions(
    request: Request,
    payload: Annotated[TokenPayload, Depends(require_auth)],
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=_MAX_PAGE_SIZE),
) -> PaginatedSessionsResponse:
    sa = request.app.state.superagent
    resp = await sa.get(
        "/sessions",
        params={
            "user_id": payload.user_id,
            "page": page,
            "page_size": page_size,
        },
    )
    resp.raise_for_status()
    return PaginatedSessionsResponse.model_validate(resp.json())


@router.get("/{session_id}/transcript", response_model=TranscriptListResponse)
async def get_session_transcript(
    session_id: str,
    request: Request,
    payload: Annotated[TokenPayload, Depends(require_auth)],
) -> TranscriptListResponse:
    sa = request.app.state.superagent
    await _assert_session_owner(
        session_id, payload.user_id, request.app.state.redis, sa
    )
    resp = await sa.get(
        f"/sessions/{session_id}/transcript",
        params={"user_id": payload.user_id},
    )
    if resp.status_code == 404:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Session not found"
        )
    resp.raise_for_status()
    return TranscriptListResponse.model_validate(resp.json())


@router.get("/{session_id}/audit")
async def get_session_audit(
    session_id: str,
    request: Request,
    payload: Annotated[TokenPayload, Depends(require_auth)],
) -> dict[str, Any]:
    """Proxy the SuperAgent Verified Runs audit package for this session."""
    sa = request.app.state.superagent
    await _assert_session_owner(
        session_id, payload.user_id, request.app.state.redis, sa
    )
    resp = await sa.get(
        f"/sessions/{session_id}/audit",
        params={"user_id": payload.user_id},
    )
    if resp.status_code == 404:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Session not found"
        )
    resp.raise_for_status()
    return resp.json()


@router.post("/{session_id}/message")
async def send_message(
    session_id: str,
    body: MessageRequest,
    request: Request,
    payload: Annotated[TokenPayload, Depends(require_auth)],
) -> StreamingResponse:
    redis = request.app.state.redis
    await _assert_session_owner(
        session_id, payload.user_id, redis, request.app.state.superagent
    )
    session_credentials = await _get_session_credentials(session_id, redis)
    sa_body = {
        "user_id": payload.user_id,
        "message": body.message,
        "session_credentials": session_credentials,
        "artifact_ids": body.artifact_ids,
    }
    if body.model:
        sa_body["model"] = body.model
    if body.custom_instructions:
        sa_body["custom_instructions"] = body.custom_instructions
    if body.acceptance_criteria:
        sa_body["acceptance_criteria"] = body.acceptance_criteria

    async def gen() -> AsyncIterator[str]:
        async for chunk in proxy_superagent_sse(
            request.app.state.superagent,
            f"/sessions/{session_id}/message",
            sa_body,
        ):
            yield chunk

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.post("/{session_id}/resume")
async def resume_session(
    session_id: str,
    body: ResumeRequest,
    request: Request,
    payload: Annotated[TokenPayload, Depends(require_auth)],
) -> StreamingResponse:
    redis = request.app.state.redis
    await _assert_session_owner(
        session_id, payload.user_id, redis, request.app.state.superagent
    )
    session_credentials = await _get_session_credentials(session_id, redis)
    # Forward the resume value dict directly — SuperAgent runner passes it verbatim
    # to Command(resume=value) which becomes the return value of interrupt().
    sa_body = {
        "user_id": payload.user_id,
        "value": body.value,
        "session_credentials": session_credentials,
    }

    async def gen() -> AsyncIterator[str]:
        async for chunk in proxy_superagent_sse(
            request.app.state.superagent,
            f"/sessions/{session_id}/resume",
            sa_body,
        ):
            yield chunk

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.get("/{session_id}/artifacts")
async def list_session_artifacts(
    session_id: str,
    request: Request,
    payload: Annotated[TokenPayload, Depends(require_auth)],
) -> list[dict]:
    """Return all artifacts (USER_UPLOAD + AGENT_OUTPUT) for a session."""
    await _assert_session_owner(session_id, payload.user_id, request.app.state.redis)
    from ..config import settings

    try:
        from common.database.src.generated_client import Prisma

        db = Prisma(datasource={"url": settings.database_url})
        await db.connect()
        rows = await db.artifact.find_many(
            where={"session_id": session_id, "status": "READY"},
            order={"created_at": "asc"},
        )
        await db.disconnect()
    except Exception:
        logger.exception("list_session_artifacts: DB error for session %s", session_id)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE) from None

    return [
        {
            "artifact_id": r.id,
            "filename": r.filename,
            "mime_type": r.mime_type,
            "size_bytes": r.size_bytes,
            "source": r.source,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]


@router.get("/{session_id}/status", response_model=SessionStatusResponse)
async def session_status(
    session_id: str,
    request: Request,
    payload: Annotated[TokenPayload, Depends(require_auth)],
) -> SessionStatusResponse:
    await _assert_session_owner(
        session_id,
        payload.user_id,
        request.app.state.redis,
        request.app.state.superagent,
    )
    sa = request.app.state.superagent
    resp = await sa.get(f"/sessions/{session_id}/status")
    resp.raise_for_status()
    data = resp.json()
    return SessionStatusResponse(**data)


@router.post("/{session_id}/stop", response_model=SessionStopResponse)
async def stop_session_execution(
    session_id: str,
    request: Request,
    payload: Annotated[TokenPayload, Depends(require_auth)],
) -> SessionStopResponse:
    await _assert_session_owner(
        session_id,
        payload.user_id,
        request.app.state.redis,
        request.app.state.superagent,
    )
    sa = request.app.state.superagent
    resp = await sa.post(f"/sessions/{session_id}/stop")
    resp.raise_for_status()
    return SessionStopResponse.model_validate(resp.json())
