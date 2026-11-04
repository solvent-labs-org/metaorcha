"""Workflow CRUD routes."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Annotated, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from common.utils.src import firing_view
from common.utils.src.cron import crontab_trigger, next_slot

from ..config import settings
from ..offices.context import OfficeContext, require_member_office, require_office
from ..sessions.routes import assert_session_access
from .models import (
    CreateRoutineRequest,
    CreateWorkflowRequest,
    FiringResponse,
    UpdateWorkflowRequest,
    WorkflowResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/workflows", tags=["workflows"])


def _json_dict(value: Any) -> dict[str, Any]:
    value = getattr(value, "data", value)  # prisma.Json wraps its payload
    return dict(value) if isinstance(value, dict) else {}


def _state(row: Any) -> str:
    return str(getattr(row.state, "value", row.state))


# States whose run was sealed: the only ones with an envelope or a ledger row
# to read (story 2.5).
_SEALED = ("settled", "refused", "attested_unsettled")
# States a gate decision is shown for: the row's state is the truth (AD-22).
_GATED = ("settled", "refused")


async def _envelopes(db: Any, rows: list[Any]) -> dict[tuple[str, str], Any] | None:
    """Stored envelopes keyed by ``(run_id, session_id)``; None if unread.

    An envelope counts for a firing only in the firing's own session.
    """
    if not rows:
        return {}
    try:
        found = await db.attestation.find_many(
            where={
                "run_id": {"in": sorted({r.run_id for r in rows})},
                "session_id": {"in": sorted({r.session_id for r in rows})},
            }
        )
    except Exception:
        logger.warning("routine firings: envelopes not read", exc_info=True)
        return None
    return {(a.run_id, a.session_id): a.payload for a in found}


async def _ledger(db: Any, rows: list[Any]) -> dict[str, list[Any]] | None:
    """Each run's ``attested_settlements`` rows, oldest first; None if unread."""
    if not rows:
        return {}
    try:
        found = await db.attestedsettlement.find_many(
            where={"run_id": {"in": sorted({r.run_id for r in rows})}},
            order={"created_at": "asc"},
        )
    except Exception:
        logger.warning("routine firings: ledger not read", exc_info=True)
        return None
    by_run: dict[str, list[Any]] = {}
    for row in found:
        by_run.setdefault(row.run_id, []).append(row)
    return by_run


async def _firing_views(
    db: Any,
    rows: Iterable[Any],
    criteria_of: dict[str, dict[str, Any]],
    viewer_id: str,
) -> dict[str, FiringResponse]:
    """The pane's view of each firing, keyed by firing id (story 2.5).

    The state and its label come from the row. Two batched reads add to it,
    each in its own ``try`` so a failed read leaves its fields empty and
    never turns into a claim: the stored envelopes (receipt, declared checks)
    and the settlement ledger (gate, the "no settlement decision" note).
    """
    rows = list(rows)
    sealed = [r for r in rows if r.run_id and _state(r) in _SEALED]
    envelopes = await _envelopes(db, [r for r in sealed if r.session_id])
    ledger = await _ledger(db, sealed)

    views: dict[str, FiringResponse] = {}
    for row in rows:
        state = _state(row)
        is_sealed = bool(row.run_id) and state in _SEALED
        runs = ledger.get(row.run_id, []) if ledger is not None else None
        ledger_seen = bool(runs) if is_sealed and runs is not None else None

        gate = gate_label = None
        checks_on_row: list[str] = []
        if state in _GATED and runs:
            deciding = firing_view.deciding_row(runs)
            gate = firing_view.gate_kind(deciding)
            gate_label = firing_view.VERDICT_ONLY if gate == "verdict_only" else None
            checks_on_row = firing_view.gate_checks(deciding)

        receipt, checks, checks_label = False, None, None
        key = (row.run_id, row.session_id)
        if is_sealed and envelopes is not None and key in envelopes:
            receipt = True
            # An envelope the gate found failed verification is no evidence
            # of any check; the label already names the failed check.
            untrusted = bool(runs) and firing_view.envelope_untrusted(
                firing_view.deciding_row(runs)
            )
            checks, checks_label = firing_view.checks_view(
                criteria_of.get(row.routine_id), envelopes[key], untrusted
            )

        views[row.id] = FiringResponse(
            id=row.id,
            slot=row.slot,
            state=state,
            detail=row.detail,
            session_id=row.session_id,
            run_id=row.run_id,
            created_at=row.created_at,
            updated_at=row.updated_at,
            label=firing_view.state_label(state, row.detail),
            note=firing_view.note(state, row.detail, ledger_seen),
            gate=gate,
            gate_label=gate_label,
            gate_checks=checks_on_row,
            receipt_available=receipt,
            receipt_downloadable=receipt and row.user_id == viewer_id,
            checks=checks,
            checks_label=checks_label,
        )
    return views


def _criteria_of(records: Iterable[Any]) -> dict[str, dict[str, Any]]:
    return {r.id: _json_dict(getattr(r, "criteria", None)) for r in records}


def _to_response(
    record: object, last_firing: FiringResponse | None = None
) -> WorkflowResponse:
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
        next_run_at=getattr(record, "next_run_at", None),
        last_firing=last_firing,
    )


async def _last_firings(db: Any, routine_ids: list[str]) -> dict[str, Any]:
    """The newest firing of each routine, keyed by routine id."""
    if not routine_ids:
        return {}
    rows = await db.routinefiring.find_many(
        where={"routine_id": {"in": routine_ids}},
        order={"slot": "desc"},
        distinct=["routine_id"],
    )
    latest: dict[str, Any] = {}
    for row in rows:
        latest.setdefault(row.routine_id, row)
    return latest


_NOT_FOUND = "Workflow not found"


def _visible_where(ctx: OfficeContext) -> dict[str, Any]:
    """Story 2.0 (FR-32, OQ-16): owners see the office's routines; members their own."""
    where: dict[str, Any] = {"office_id": ctx.office_id}
    if not ctx.is_owner:
        where["user_id"] = ctx.user_id
    return where


async def _find_visible(db: Any, workflow_id: str, ctx: OfficeContext) -> Any:
    record = await db.workflowtemplate.find_first(
        where={"id": workflow_id, **_visible_where(ctx)}
    )
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND)
    return record


def _reject(field: str, reason: str) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={"field": field, "reason": reason},
    )


def _check_schedule(cron: str, tz: str) -> None:
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        raise _reject("timezone", f"unknown time zone: {tz}") from None
    try:
        # A standard crontab (0 = Sunday), read through the one translation
        # the SuperAgent's scheduler fires with (story 2.2).
        crontab_trigger(cron, tz)
    except ValueError as exc:
        raise _reject("cron", f"invalid cron expression: {exc}") from None


def _next_slot(cron: str, tz: str) -> datetime:
    """The schedule's first slot after now, in UTC (the SuperAgent's rule)."""
    try:
        return next_slot(" ".join(cron.split()), tz, datetime.now(UTC))
    except ValueError as exc:
        raise _reject("cron", str(exc)) from None


@router.post("", response_model=WorkflowResponse, status_code=201)
async def create_workflow(
    body: CreateWorkflowRequest,
    request: Request,
    ctx: Annotated[OfficeContext, Depends(require_office)],
) -> WorkflowResponse:
    from common.database.src.generated_client.fields import Json

    payload = ctx.payload
    sa = request.app.state.superagent
    # The session must be the caller's, in this office: its captured workflow
    # is read from SuperAgent state, which does no ownership check of its own.
    await assert_session_access(request, body.session_id, ctx)
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
            "office_id": ctx.office_id,
            "name": body.name,
            "description": body.description or captured.get("goal_template", "")[:200],
            "goal_template": captured["goal_template"],
            # Json columns must be wrapped, as in create_routine.
            "parameters": Json(captured.get("parameters", {})),
            "steps": Json(captured.get("steps", [])),
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
    ctx: Annotated[OfficeContext, Depends(require_member_office)],
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
    payload = ctx.payload
    cron = " ".join(body.cron.split())
    _check_schedule(cron, body.timezone)

    # Story 2.0: a routine may use only the caller's own, active connections in
    # this office. A member's token acts as that member (FR-17), so another
    # member's connection — or another office's — is refused here, before the
    # SuperAgent reads anything.
    db = request.app.state.db
    owned = await db.agent.find_many(
        where={
            "id": {"in": body.connections},
            "office_id": ctx.office_id,
            "user_id": ctx.user_id,
            "is_active": True,
        }
    )
    owned_ids = {a.id for a in owned}
    for did in body.connections:
        if did not in owned_ids:
            raise _reject(
                "connections", f"{did} is not one of your connections in this office"
            )

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

    record = await db.workflowtemplate.create(
        data={
            "user_id": payload.user_id,
            "office_id": ctx.office_id,
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
    ctx: Annotated[OfficeContext, Depends(require_office)],
) -> list[WorkflowResponse]:
    db = request.app.state.db
    records = await db.workflowtemplate.find_many(
        where=_visible_where(ctx),
        order={"created_at": "desc"},
    )
    latest = await _last_firings(db, [r.id for r in records])
    views = await _firing_views(db, latest.values(), _criteria_of(records), ctx.user_id)
    return [
        _to_response(r, views[latest[r.id].id] if r.id in latest else None)
        for r in records
    ]


@router.get("/{workflow_id}", response_model=WorkflowResponse)
async def get_workflow(
    workflow_id: str,
    request: Request,
    ctx: Annotated[OfficeContext, Depends(require_office)],
) -> WorkflowResponse:
    db = request.app.state.db
    record = await _find_visible(db, workflow_id, ctx)
    latest = await _last_firings(db, [record.id])
    views = await _firing_views(
        db, latest.values(), _criteria_of([record]), ctx.user_id
    )
    last = latest.get(record.id)
    return _to_response(record, views[last.id] if last is not None else None)


@router.get("/{workflow_id}/firings", response_model=list[FiringResponse])
async def list_firings(
    workflow_id: str,
    request: Request,
    ctx: Annotated[OfficeContext, Depends(require_office)],
    limit: int = Query(20, ge=1, le=100),
) -> list[FiringResponse]:
    """A routine's firings, newest slot first (story 2.2, AD-22).

    Visible exactly as the routine is: an owner sees any routine's in the
    office, a member their own. A paused firing's approval card is in its
    session, which only the routine's owner can open (OQ-15).
    """
    db = request.app.state.db
    record = await _find_visible(db, workflow_id, ctx)
    rows = await db.routinefiring.find_many(
        where={"routine_id": record.id}, order={"slot": "desc"}, take=limit
    )
    views = await _firing_views(db, rows, _criteria_of([record]), ctx.user_id)
    return [views[r.id] for r in rows]


@router.patch("/{workflow_id}", response_model=WorkflowResponse)
async def update_workflow(
    workflow_id: str,
    body: UpdateWorkflowRequest,
    request: Request,
    ctx: Annotated[OfficeContext, Depends(require_office)],
) -> WorkflowResponse:
    """Edit your own routine; an owner may also pause anyone's (FR-33)."""
    db = request.app.state.db
    existing = await _find_visible(db, workflow_id, ctx)
    update_data = body.model_dump(exclude_none=True)
    if existing.user_id != ctx.user_id and update_data != {"status": "inactive"}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="an owner may pause another member's routine, not edit it",
        )
    if update_data.get("status") == "inactive":
        # Pausing turns the schedule off; a slot is never claimed while off.
        update_data["schedule_enabled"] = False
        update_data["next_run_at"] = None
    elif update_data.get("status") == "scheduled":
        # Story 2.2: the routine's own member turns its schedule on. It fires
        # at the next slot after now — never a catch-up of missed slots.
        if not getattr(existing, "schedule_cron", None):
            raise _reject("status", "this routine has no schedule to turn on")
        if not settings.connections_enabled:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="connections_disabled: a routine runs on connections, which "
                "are turned off on this deployment (CONNECTIONS_ENABLED)",
            )
        _check_schedule(existing.schedule_cron, existing.schedule_tz or "UTC")
        update_data["schedule_enabled"] = True
        update_data["next_run_at"] = _next_slot(
            existing.schedule_cron, existing.schedule_tz or "UTC"
        )
    record = await db.workflowtemplate.update(
        where={"id": workflow_id}, data=update_data
    )
    return _to_response(record)


@router.delete("/{workflow_id}", status_code=204)
async def delete_workflow(
    workflow_id: str,
    request: Request,
    ctx: Annotated[OfficeContext, Depends(require_office)],
) -> None:
    db = request.app.state.db
    existing = await _find_visible(db, workflow_id, ctx)
    if existing.user_id != ctx.user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="only the member who saved a routine can delete it",
        )
    await db.workflowtemplate.delete(where={"id": workflow_id})
