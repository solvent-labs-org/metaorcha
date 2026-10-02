"""Workflow request/response models."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class CreateWorkflowRequest(BaseModel):
    session_id: str
    name: str
    description: str | None = None


class UpdateWorkflowRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    status: str | None = Field(None, pattern="^(active|inactive|scheduled)$")


class CreateRoutineRequest(BaseModel):
    """A routine: a goal with its connections, allowed writes, model, criteria
    and schedule (story 2.1, FR-23). Validated before anything is persisted."""

    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    goal: str = Field(min_length=1, max_length=8000)
    connections: list[str] = Field(min_length=1, max_length=20)
    scope_allow: list[str] = Field(default_factory=list, max_length=100)
    model: str = Field(min_length=1, max_length=200)
    criteria: dict[str, bool] = Field(default_factory=dict)
    criteria_operands: dict[str, dict[str, Any]] = Field(default_factory=dict)
    cron: str = Field(min_length=1, max_length=120)
    timezone: str = Field(default="UTC", max_length=64)


class FiringResponse(BaseModel):
    """One claimed schedule slot (story 2.2, AD-22). ``state`` is the shared
    vocabulary: scheduled, running, attested_unsettled, settled, refused,
    paused, skipped, error. A paused firing's approval is in ``session_id``."""

    id: str
    slot: datetime
    state: str
    detail: str | None = None
    session_id: str | None = None
    run_id: str | None = None
    created_at: datetime
    updated_at: datetime


class WorkflowResponse(BaseModel):
    id: str
    name: str
    description: str | None
    goal_template: str
    status: str
    agents_used: list[str]
    steps: Any
    run_count: int
    created_at: datetime
    updated_at: datetime
    # Routine fields (story 2.1); absent on a template saved from a chat.
    schedule_cron: str | None = None
    schedule_tz: str | None = None
    schedule_enabled: bool = False
    model: str | None = None
    scope_allow: list[str] = Field(default_factory=list)
    criteria: dict[str, Any] = Field(default_factory=dict)
    criteria_operands: dict[str, Any] = Field(default_factory=dict)
    # Story 2.2: when the schedule next fires, and how the last firing ended.
    next_run_at: datetime | None = None
    last_firing: FiringResponse | None = None
