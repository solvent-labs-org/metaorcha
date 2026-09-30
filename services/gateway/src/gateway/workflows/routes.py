"""Workflow CRUD routes."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status

from ..auth.models import TokenPayload
from ..dependencies import require_auth
from .models import (
    ComposeWorkflowRequest,
    CreateWorkflowRequest,
    UpdateWorkflowRequest,
    WorkflowResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/workflows", tags=["workflows"])


def _to_response(record: object) -> WorkflowResponse:
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
    )


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


@router.post("/compose", response_model=WorkflowResponse, status_code=201)
async def compose_workflow(
    body: ComposeWorkflowRequest,
    request: Request,
    payload: Annotated[TokenPayload, Depends(require_auth)],
) -> WorkflowResponse:
    names = ", ".join(body.agent_ids)
    goal = f"Use these agents together: {names}"
    db = request.app.state.db
    record = await db.workflowtemplate.create(
        data={
            "user_id": payload.user_id,
            "name": body.name,
            "description": body.description or goal[:200],
            "goal_template": goal,
            "parameters": {},
            "steps": [{"kind": "a2a_compose", "agents": body.agent_ids}],
            "agents_used": body.agent_ids,
            "status": "active",
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
