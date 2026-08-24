"""SuperAgent API routes.

POST /sessions                  → { session_id }
POST /sessions/{id}/message     → SSE stream
POST /sessions/{id}/resume      → SSE stream
PATCH /sessions/{id}/context      → merge lead_gen_options / email_campaign_context
GET  /sessions/{id}/status      → { status, pending_interrupt }
GET  /sessions/{id}/audit       → Verified Runs evidence package
GET  /health                    → { status: "ok" }
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from .models import (
    AgentEnvStatusResponse,
    ConversationSessionSummaryDTO,
    CreateSessionRequest,
    CreateSessionResponse,
    HealthResponse,
    MessageRequest,
    PaginatedSessionsResponse,
    ResumeRequest,
    RunAuditResponse,
    SessionContextPatchRequest,
    SessionContextPatchResponse,
    SessionDetailResponse,
    SessionStatusResponse,
    SessionStopResponse,
    StoreAgentEnvRequest,
    TranscriptEntryDTO,
    TranscriptListResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _get_runner(request: Request) -> Any:
    runner = getattr(request.app.state, "runner", None)
    if runner is None:
        raise HTTPException(status_code=503, detail="SuperAgent runner not initialised")
    return runner


def _coerce_estimated_token_count(value: object) -> int:
    """Graph state may expose counts as int, str, or other; Pydantic expects int."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return 0
    return 0


# ── Routes ─────────────────────────────────────────────────────────────────────


@router.post("/sessions", response_model=CreateSessionResponse)
async def create_session(body: CreateSessionRequest) -> CreateSessionResponse:
    from ..persistence.transcript_store import upsert_conversation_session

    session_id = str(uuid.uuid4())
    try:
        await upsert_conversation_session(session_id, body.user_id, body.title)
    except Exception:
        logger.exception("create_session: DB upsert failed for %s", session_id)
        raise HTTPException(
            status_code=503, detail="Could not persist session"
        ) from None
    logger.info("Session created: %s for user %s", session_id, body.user_id)
    return CreateSessionResponse(session_id=session_id)


_MAX_PAGE_SIZE = 50


@router.get("/sessions", response_model=PaginatedSessionsResponse)
async def list_sessions(
    user_id: str = Query(..., description="Owner user id (set by Gateway from JWT)"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=_MAX_PAGE_SIZE),
) -> PaginatedSessionsResponse:
    from ..persistence.transcript_store import list_sessions_paginated

    rows, total = await list_sessions_paginated(user_id, page, page_size)
    items = [
        ConversationSessionSummaryDTO(
            session_id=r.id,
            title=r.title,
            updated_at=r.updated_at.isoformat() if r.updated_at else "",
        )
        for r in rows
    ]
    has_next = page * page_size < total
    return PaginatedSessionsResponse(
        items=items,
        page=page,
        page_size=page_size,
        total=total,
        has_next=has_next,
    )


@router.get("/sessions/{session_id}", response_model=SessionDetailResponse)
async def get_session(session_id: str) -> SessionDetailResponse:
    from ..persistence.transcript_store import get_session_meta

    meta = await get_session_meta(session_id)
    if meta is None or meta[1] is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return SessionDetailResponse(session_id=session_id, user_id=meta[1])


@router.get("/sessions/{session_id}/transcript", response_model=TranscriptListResponse)
async def get_transcript(
    session_id: str,
    user_id: str = Query(..., description="Caller user id for ownership check"),
) -> TranscriptListResponse:
    from ..persistence.transcript_store import (
        entry_to_dto,
        load_transcript_rows,
        verify_session_owner,
    )

    if not await verify_session_owner(session_id, user_id):
        raise HTTPException(status_code=404, detail="Session not found")
    rows = await load_transcript_rows(session_id)
    entries = [TranscriptEntryDTO(**entry_to_dto(r)) for r in rows]
    return TranscriptListResponse(entries=entries)


@router.get(
    "/sessions/{session_id}/audit",
    response_model=RunAuditResponse,
    response_model_exclude_none=True,
)
async def get_run_audit(
    session_id: str,
    user_id: str = Query(..., description="Caller user id for ownership check"),
) -> RunAuditResponse:
    from ..persistence.transcript_store import (
        load_transcript_rows,
        verify_session_owner,
    )
    from .audit import build_run_audit

    if not await verify_session_owner(session_id, user_id):
        raise HTTPException(status_code=404, detail="Session not found")
    rows = await load_transcript_rows(session_id)
    return build_run_audit(session_id, rows)


async def _load_artifact_refs(artifact_ids: list[str]) -> dict[str, Any]:
    """Fetch Artifact DB rows and build ArtifactRef dict keyed by artifact_id."""
    if not artifact_ids:
        return {}
    from ..graph.state import ArtifactRef

    try:
        from src.generated_client import Prisma

        db = Prisma()
        await db.connect()
        refs: dict[str, Any] = {}
        for artifact_id in artifact_ids:
            try:
                row = await db.artifact.find_unique(where={"id": artifact_id})
                if row and row.status == "READY":
                    refs[artifact_id] = ArtifactRef(
                        artifact_id=row.id,
                        filename=row.filename,
                        mime_type=row.mime_type,
                        size_bytes=row.size_bytes,
                        s3_bucket=row.s3_bucket,
                        s3_key=row.s3_key,
                    )
            except Exception:
                logger.warning("Could not load artifact %s — skipping", artifact_id)
        await db.disconnect()
        return refs
    except Exception:
        logger.exception("_load_artifact_refs failed")
        return {}


@router.post("/sessions/{session_id}/message")
async def send_message(
    session_id: str, body: MessageRequest, request: Request
) -> StreamingResponse:
    runner = _get_runner(request)
    initial_artifacts = await _load_artifact_refs(body.artifact_ids)

    async def gen() -> AsyncIterator[str]:
        try:
            async for event in runner.run_turn(
                session_id=session_id,
                user_id=body.user_id,
                user_message=body.message,
                session_credentials=body.session_credentials,
                initial_artifacts=initial_artifacts,
                lead_gen_options=body.lead_gen_options or None,
                email_campaign_context=body.email_campaign_context or None,
                model=body.model,
                custom_instructions=body.custom_instructions,
            ):
                yield f"data: {json.dumps(event)}\n\n"
        except Exception:
            logger.exception("send_message SSE gen failed for session %s", session_id)
            yield f"data: {json.dumps({'type': 'error', 'error': 'Unexpected server error'})}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.post("/sessions/{session_id}/resume")
async def resume_session(
    session_id: str, body: ResumeRequest, request: Request
) -> StreamingResponse:
    runner = _get_runner(request)

    # Named-human attribution (KY-A, WS6): stamp the resume payload with the
    # authenticated user identity forwarded by the Gateway (JWT-verified), so
    # HITL decisions are attributed server-side rather than by free-text
    # client fields alone.
    value = dict(body.value)
    value.setdefault("authoriser_user_id", body.user_id)

    async def gen() -> AsyncIterator[str]:
        try:
            async for event in runner.resume_from_interrupt(
                session_id=session_id,
                value=value,
                session_credentials=body.session_credentials,
            ):
                yield f"data: {json.dumps(event)}\n\n"
        except Exception:
            logger.exception("resume_session SSE gen failed for session %s", session_id)
            yield f"data: {json.dumps({'type': 'error', 'error': 'Unexpected server error'})}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.get("/sessions/{session_id}/status", response_model=SessionStatusResponse)
async def session_status(session_id: str, request: Request) -> SessionStatusResponse:
    runner = _get_runner(request)
    status_data = await runner.get_status(session_id)
    # Path session_id is authoritative; get_status may omit it (not_found) or include it.
    payload: dict[str, Any] = {**status_data, "session_id": session_id}
    payload.setdefault("lead_gen_options", {})
    payload.setdefault("email_campaign_context", {})
    payload["estimated_token_count"] = _coerce_estimated_token_count(
        payload.get("estimated_token_count", 0)
    )
    return SessionStatusResponse.model_validate(payload)


@router.post("/sessions/{session_id}/stop", response_model=SessionStopResponse)
async def stop_session_execution(
    session_id: str, request: Request
) -> SessionStopResponse:
    runner = _get_runner(request)
    result = await runner.stop_session_execution(session_id)
    return SessionStopResponse.model_validate(result)


@router.patch(
    "/sessions/{session_id}/context",
    response_model=SessionContextPatchResponse,
)
async def patch_session_context(
    session_id: str, body: SessionContextPatchRequest, request: Request
) -> SessionContextPatchResponse:
    """Merge `lead_gen_options` / `email_campaign_context` into the checkpoint without chatting."""
    if body.lead_gen_options is None and body.email_campaign_context is None:
        return SessionContextPatchResponse(ok=True, merged={})
    runner = _get_runner(request)
    raw = await runner.patch_session_context(
        session_id,
        lead_gen_options=body.lead_gen_options,
        email_campaign_context=body.email_campaign_context,
    )
    if not raw.get("ok"):
        return SessionContextPatchResponse(
            ok=False,
            merged={},
            error=str(raw.get("error") or "patch_failed"),
        )
    merged = raw.get("merged") or {}
    return SessionContextPatchResponse(
        ok=True, merged=merged if isinstance(merged, dict) else {}
    )


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse()


# ── Agent credential management ───────────────────────────────────────────────
# These endpoints let the frontend store and check env-var credentials for
# STDIO MCP agents (e.g. an API key for a stdio-spawned MCP server).
# Values are AES-256-GCM encrypted in the UserSecret table; they are never
# returned via GET — only existence is reported.


@router.post("/secrets/agent-env", status_code=204)
async def store_agent_env(body: StoreAgentEnvRequest) -> None:
    """
    Encrypt and store env-var credentials for a STDIO MCP agent.

    Body::

        {
          "user_id": "user-123",
          "agent_id": "did:orcha:agent:finance-dashboard-agent",
          "credentials": { "NOTION_API_KEY": "secret_abc123" }
        }
    """
    from ..vault.client import VaultClient

    vault = VaultClient()
    for var_name, value in body.credentials.items():
        await vault.save_agent_env(body.user_id, body.agent_id, var_name, value)


@router.get(
    "/secrets/agent-env/{agent_id}/status",
    response_model=AgentEnvStatusResponse,
)
async def agent_env_status(
    agent_id: str,
    user_id: str,
    var_names: str,
) -> AgentEnvStatusResponse:
    """
    Check which env-var credentials are configured for an agent.

    Query params:
        user_id   — the user whose vault to check
        var_names — comma-separated list of env-var names to check
                    e.g. ``?var_names=NOTION_API_KEY,NOTION_DATABASE_ID``

    Returns ``"configured"`` or ``"missing"`` for each name.
    Values are never returned.
    """
    from ..vault.client import VaultClient

    vault = VaultClient()
    names = [n.strip() for n in var_names.split(",") if n.strip()]
    existence = await vault.list_agent_env_status(user_id, agent_id, names)
    return AgentEnvStatusResponse(
        agent_id=agent_id,
        status={k: ("configured" if v else "missing") for k, v in existence.items()},
    )


@router.delete("/secrets/agent-env/{agent_id}/{var_name}", status_code=204)
async def delete_agent_env(
    agent_id: str,
    var_name: str,
    user_id: str,
) -> None:
    """
    Delete a stored env-var credential for a STDIO MCP agent.

    Query params:
        user_id — the user whose vault entry to delete
    """
    from ..vault.client import VaultClient

    vault = VaultClient()
    await vault.delete_agent_env(user_id, agent_id, var_name)
