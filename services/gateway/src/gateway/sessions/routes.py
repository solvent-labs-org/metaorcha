"""Session routes — create, message, resume, status."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse

from ..offices.context import (
    OfficeContext,
    personal_office_id,
    require_office,
)
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
_SESSION_OFFICE_KEY = "gateway:session-office:"


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


async def _session_office(session_id: str, redis: Any, sa: Any) -> str | None:
    cached = await redis.get(f"{_SESSION_OFFICE_KEY}{session_id}")
    if cached is not None:
        return cached.decode() if isinstance(cached, bytes) else str(cached)
    resp = await sa.get(f"/sessions/{session_id}")
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    data = resp.json()
    office = data.get("office_id") or personal_office_id(data["user_id"])
    await redis.set(f"{_SESSION_OFFICE_KEY}{session_id}", office, ex=_SESSION_TTL)
    return office


async def assert_session_access(
    request: Request, session_id: str, ctx: OfficeContext
) -> None:
    """The session must be the caller's and belong to the request's office.

    Story 2.0 (FR-32/33): a session is fixed to one office when it is created.
    Naming another office — or no longer being a member of the session's
    office, which ``require_office`` already refuses — reads as 404. A session
    created before offices existed belongs to its owner's personal office.
    """
    redis = request.app.state.redis
    sa = request.app.state.superagent
    await _assert_session_owner(session_id, ctx.user_id, redis, sa)
    office = await _session_office(session_id, redis, sa)
    if office != ctx.office_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Session not found"
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
    ctx: Annotated[OfficeContext, Depends(require_office)],
    body: CreateSessionBody = Body(default_factory=CreateSessionBody),
) -> CreateSessionResponse:
    payload = ctx.payload
    sa = request.app.state.superagent
    b = body
    resp = await sa.post(
        "/sessions",
        json={"user_id": payload.user_id, "title": b.title, "office_id": ctx.office_id},
    )
    resp.raise_for_status()
    session_id: str = resp.json()["session_id"]
    # Index session → user ownership and its office in Redis
    redis = request.app.state.redis
    await redis.set(f"gateway:session:{session_id}", payload.user_id, ex=_SESSION_TTL)
    await redis.set(
        f"{_SESSION_OFFICE_KEY}{session_id}", ctx.office_id, ex=_SESSION_TTL
    )
    return CreateSessionResponse(session_id=session_id)


@router.get("", response_model=PaginatedSessionsResponse)
async def list_sessions(
    request: Request,
    ctx: Annotated[OfficeContext, Depends(require_office)],
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=_MAX_PAGE_SIZE),
) -> PaginatedSessionsResponse:
    sa = request.app.state.superagent
    resp = await sa.get(
        "/sessions",
        params={
            "user_id": ctx.user_id,
            "office_id": ctx.office_id,
            # A session with no office is its owner's personal office's.
            "include_unassigned": ctx.is_personal,
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
    ctx: Annotated[OfficeContext, Depends(require_office)],
) -> TranscriptListResponse:
    payload = ctx.payload
    sa = request.app.state.superagent
    await assert_session_access(request, session_id, ctx)
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
    ctx: Annotated[OfficeContext, Depends(require_office)],
) -> dict[str, Any]:
    payload = ctx.payload
    """Proxy the SuperAgent Verified Runs audit package for this session."""
    sa = request.app.state.superagent
    await assert_session_access(request, session_id, ctx)
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
    ctx: Annotated[OfficeContext, Depends(require_office)],
) -> StreamingResponse:
    payload = ctx.payload
    redis = request.app.state.redis
    await assert_session_access(request, session_id, ctx)
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
    ctx: Annotated[OfficeContext, Depends(require_office)],
) -> StreamingResponse:
    payload = ctx.payload
    redis = request.app.state.redis
    await assert_session_access(request, session_id, ctx)
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
    ctx: Annotated[OfficeContext, Depends(require_office)],
) -> list[dict]:
    """Return all artifacts (USER_UPLOAD + AGENT_OUTPUT) for a session."""
    await assert_session_access(request, session_id, ctx)
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
    ctx: Annotated[OfficeContext, Depends(require_office)],
) -> SessionStatusResponse:
    await assert_session_access(request, session_id, ctx)
    sa = request.app.state.superagent
    resp = await sa.get(f"/sessions/{session_id}/status")
    resp.raise_for_status()
    data = resp.json()
    return SessionStatusResponse(**data)


@router.post("/{session_id}/stop", response_model=SessionStopResponse)
async def stop_session_execution(
    session_id: str,
    request: Request,
    ctx: Annotated[OfficeContext, Depends(require_office)],
) -> SessionStopResponse:
    await assert_session_access(request, session_id, ctx)
    sa = request.app.state.superagent
    resp = await sa.post(f"/sessions/{session_id}/stop")
    resp.raise_for_status()
    return SessionStopResponse.model_validate(resp.json())
