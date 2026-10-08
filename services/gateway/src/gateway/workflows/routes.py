"""Workflow CRUD routes."""

from __future__ import annotations

import logging
from typing import Annotated, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.triggers.cron import CronTrigger
from fastapi import APIRouter, Depends, HTTPException, Request, status

from ..auth.models import TokenPayload
from ..config import settings
from ..dependencies import require_auth, require_member
from .models import (
    CreateRoutineRequest,
    CreateWorkflowRequest,
    UpdateWorkflowRequest,
    WorkflowResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/workflows", tags=["workflows"])


def _json_dict(value: Any) -> dict[str, Any]:
    value = getattr(value, "data", value)  # prisma.Json wraps its payload
    return dict(value) if isinstance(value, dict) else {}


def _to_response(record: object) -> WorkflowResponse:
    parameters = _json_dict(getattr(record, "parameters", None))
    allow = parameters.get("scope_allow")
    return WorkflowResponse(
        id=record.id,  # type: ignore[attr-defined]
        name=record.name,  # type: ignore[attr-defined]
        description=record.description,  # type: ignore[attr-defined]
        goal_template=record.goal_template,  # type: ignore[attr-defined]
        status=record.status,  # type: ignore[attr-defined]
        agents_used=record.agents_used,  # type: ignore[attr-defined]
        steps=record.steps,  # type: ignore[attr-defined]
        run_count=record.run_count,  # type: ignore[attr-defined]
        created_at=record.created_at,  # type: ignore[attr-defined]
        updated_at=record.updated_at,  # type: ignore[attr-defined]
        schedule_cron=getattr(record, "schedule_cron", None),
        schedule_tz=getattr(record, "schedule_tz", None),
        schedule_enabled=bool(getattr(record, "schedule_enabled", False)),
        model=parameters.get("model")
        if isinstance(parameters.get("model"), str)
        else None,
        scope_allow=[str(a) for a in allow] if isinstance(allow, list) else [],
        criteria=_json_dict(getattr(record, "criteria", None)),
        criteria_operands=_json_dict(getattr(record, "criteria_operands", None)),
    )


def _reject(field: str, reason: str) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={"field": field, "reason": reason},
    )


def _check_schedule(cron: str, tz: str) -> None:
    try:
        zone = ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        raise _reject("timezone", f"unknown time zone: {tz}") from None
    try:
        CronTrigger.from_crontab(cron, timezone=zone)
    except ValueError as exc:
        raise _reject("cron", f"invalid cron expression: {exc}") from None


@router.post("", response_model=WorkflowResponse, status_code=201)
async def create_workflow(
    body: CreateWorkflowRequest,
    request: Request,
    payload: Annotated[TokenPayload, Depends(require_auth)],
) -> WorkflowResponse:
    # Fetch captured workflow from SuperAgent session state
    sa = request.app.state.superagent
    resp = await sa.get(f"/sessions/{body.session_id}/status")
    resp.raise_for_status()
    status_data = resp.json()
    captured = status_data.get("captured_workflow")
    if captured is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No captured workflow found in this session. "
            "Use the save_as_workflow system tool first.",
        )
    db = request.app.state.db
    record = await db.workflowtemplate.create(
        data={
            "user_id": payload.user_id,
            "name": body.name,
            "description": body.description or captured.get("goal_template", "")[:200],
            "goal_template": captured["goal_template"],
            "parameters": captured.get("parameters", {}),
            "steps": captured.get("steps", []),
            "agents_used": captured.get("agents_used", []),
            "created_from_session": body.session_id,
            "status": "active",
        }
    )
    return _to_response(record)


@router.post("/routines", response_model=WorkflowResponse, status_code=201)
async def create_routine(
    body: CreateRoutineRequest,
    request: Request,
    payload: Annotated[TokenPayload, Depends(require_member)],
) -> WorkflowResponse:
    """Save a routine (story 2.1, FR-23, AD-18, AD-19, UX-DR4).

    Nothing is persisted unless every check passes, in this order: the
    connections feature is on (a routine needs a connection); the cron and
    time zone parse; then the SuperAgent — which owns the scope-class rules
    and the criteria vocabulary — accepts the connections, allows and
    criteria. A destructive allow, or one the class rules do not recognise,
    is refused with a reason naming the capability and its class.

    The routine is saved with its schedule recorded but not enabled: firing
    (slot claim, ``routine_firings``) is story 2.2.
    """
    if not settings.connections_enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="connections_disabled: a routine runs on connections, which "
            "are turned off on this deployment (CONNECTIONS_ENABLED)",
        )
    cron = " ".join(body.cron.split())
    _check_schedule(cron, body.timezone)

    sa = request.app.state.superagent
    checked = await sa.post(
        "/routines/validate",
        json={
            "connections": body.connections,
            "scope_allow": body.scope_allow,
            "criteria": body.criteria,
            "criteria_operands": body.criteria_operands,
        },
    )
    if checked.status_code == 422:
        detail = (checked.json() or {}).get("detail") or {}
        raise _reject(
            str(detail.get("field") or "routine"),
            str(detail.get("reason") or "rejected"),
        )
    if checked.status_code >= 400:
        logger.warning("routine save: validation unavailable (%s)", checked.status_code)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="the routine could not be checked; nothing was saved",
        )

    # Json columns must be wrapped: prisma-client-py reads a raw dict as a
    # nested-relation input, not a value.
    from common.database.src.generated_client.fields import Json

    db = request.app.state.db
    record = await db.workflowtemplate.create(
        data={
            "user_id": payload.user_id,
            "name": body.name,
            "description": body.description or body.goal[:200],
            "goal_template": body.goal,
            "parameters": Json({"scope_allow": body.scope_allow, "model": body.model}),
            "steps": Json([]),
            "agents_used": body.connections,
            "schedule_cron": cron,
            "schedule_tz": body.timezone,
            "schedule_enabled": False,
            "criteria": Json(body.criteria),
            "criteria_operands": Json(body.criteria_operands),
            "status": "inactive",
        }
    )
    return _to_response(record)


@router.get("", response_model=list[WorkflowResponse])
async def list_workflows(
    request: Request,
    payload: Annotated[TokenPayload, Depends(require_auth)],
) -> list[WorkflowResponse]:
    db = request.app.state.db
    records = await db.workflowtemplate.find_many(
        where={"user_id": payload.user_id},
        order={"created_at": "desc"},
    )
    return [_to_response(r) for r in records]


@router.get("/{workflow_id}", response_model=WorkflowResponse)
async def get_workflow(
    workflow_id: str,
    request: Request,
    payload: Annotated[TokenPayload, Depends(require_auth)],
) -> WorkflowResponse:
    db = request.app.state.db
    record = await db.workflowtemplate.find_unique(where={"id": workflow_id})
    if record is None or record.user_id != payload.user_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Workflow not found"
        )
    return _to_response(record)


@router.patch("/{workflow_id}", response_model=WorkflowResponse)
async def update_workflow(
    workflow_id: str,
    body: UpdateWorkflowRequest,
    request: Request,
    payload: Annotated[TokenPayload, Depends(require_auth)],
) -> WorkflowResponse:
    db = request.app.state.db
    existing = await db.workflowtemplate.find_unique(where={"id": workflow_id})
    if existing is None or existing.user_id != payload.user_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Workflow not found"
        )
    update_data = body.model_dump(exclude_none=True)
    record = await db.workflowtemplate.update(
        where={"id": workflow_id}, data=update_data
    )
    return _to_response(record)


@router.delete("/{workflow_id}", status_code=204)
async def delete_workflow(
    workflow_id: str,
    request: Request,
    payload: Annotated[TokenPayload, Depends(require_auth)],
) -> None:
    db = request.app.state.db
    existing = await db.workflowtemplate.find_unique(where={"id": workflow_id})
    if existing is None or existing.user_id != payload.user_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Workflow not found"
        )
    await db.workflowtemplate.delete(where={"id": workflow_id})
