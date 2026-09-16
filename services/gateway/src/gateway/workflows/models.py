"""Workflow request/response models."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator


class CreateWorkflowRequest(BaseModel):
    session_id: str
    name: str
    description: str | None = None


class ComposeWorkflowRequest(BaseModel):
    """User-owned A2A: wire at least two of their registered agents."""

    name: str = Field(min_length=1)
    agent_ids: list[str]
    description: str | None = None

    @field_validator("agent_ids")
    @classmethod
    def at_least_two(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value if item and item.strip()]
        if len(cleaned) < 2:
            raise ValueError("wire at least two of your agents")
        return cleaned


class UpdateWorkflowRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    status: str | None = Field(None, pattern="^(active|inactive|scheduled)$")


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
