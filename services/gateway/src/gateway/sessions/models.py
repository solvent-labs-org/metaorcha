"""Session request/response models."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

# Keep in sync with superagent.middleware.criteria.SUPPORTED_CRITERIA.
_SUPPORTED_CRITERIA = frozenset({"citations_required", "exit_zero"})
_MAX_CRITERIA_KEYS = 8


class CreateSessionBody(BaseModel):
    """Optional title from client (truncated first prompt)."""

    title: str | None = Field(default=None, max_length=200)


class CreateSessionResponse(BaseModel):
    session_id: str


class MessageRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=32768)
    artifact_ids: list[str] = Field(default_factory=list)
    model: str | None = Field(
        default=None,
        description="Per-turn orchestrator model override (e.g. from the model picker).",
    )
    custom_instructions: str | None = Field(
        default=None,
        max_length=2000,
        description="Per-session operator instructions appended to the orchestrator system prompt.",
    )
    acceptance_criteria: dict[str, Any] | None = Field(
        default=None,
        description="Optional machine-checkable acceptance criteria for this turn. "
        "Unknown keys are 422. At most 8 keys; values must be booleans.",
    )

    @field_validator("acceptance_criteria")
    @classmethod
    def _bound_acceptance_criteria(
        cls, value: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        if value is None:
            return value
        if len(value) > _MAX_CRITERIA_KEYS:
            raise ValueError(
                f"acceptance_criteria has at most {_MAX_CRITERIA_KEYS} keys"
            )
        for key, item in value.items():
            if key not in _SUPPORTED_CRITERIA:
                raise ValueError(f"unsupported criterion: {key}")
            if type(item) is not bool:
                raise ValueError(f"acceptance_criteria[{key!r}] must be a boolean")
        return value


class ResumeRequest(BaseModel):
    interrupt_id: str
    interrupt_type: str
    value: dict[str, Any] = Field(
        default_factory=dict,
        description="Resume value forwarded verbatim to the suspended interrupt() call.",
    )


class SessionStatusResponse(BaseModel):
    session_id: str
    status: str  # "ready" | "interrupted" | "not_found"
    active_interrupt: Any | None = None
    """Full InterruptEvent dict when interrupted; None otherwise."""
    estimated_token_count: int = 0
    artifacts: dict[str, Any] = Field(default_factory=dict)
    pnd_candidates: list[Any] = Field(default_factory=list)
    task_checklist: Any | None = None
    captured_workflow: Any | None = None


class SessionStopResponse(BaseModel):
    ok: bool
    status: Literal["stopping", "not_running"]


class ToolCallPart(BaseModel):
    id: str
    name: str
    args: dict[str, Any] = Field(default_factory=dict)


class TranscriptEntryDTO(BaseModel):
    sequence_num: int
    role: Literal["USER", "ASSISTANT", "TOOL"]
    content: str
    tool_calls: list[ToolCallPart] | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    tool_inputs: dict[str, Any] | None = None
    tool_status: Literal["success", "error"] | None = None
    created_at: str


class TranscriptListResponse(BaseModel):
    entries: list[TranscriptEntryDTO]


class ConversationSessionSummaryDTO(BaseModel):
    session_id: str
    title: str
    updated_at: str


class PaginatedSessionsResponse(BaseModel):
    items: list[ConversationSessionSummaryDTO]
    page: int
    page_size: int
    total: int
    has_next: bool
