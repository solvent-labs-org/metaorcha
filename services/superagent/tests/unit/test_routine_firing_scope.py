"""Story 2.2: on a routine firing nobody is present, so the gate refuses.

A firing's turn carries ``routine_context`` (its connections and allows) in
graph state. The scope gate (AD-18) then:

- passes a read, and a write the routine allows as ``<DID>#<capability>``;
- refuses a write outside the allows with ``scope_not_allowed`` — never a
  pause, because nobody is there to answer one;
- still pauses a destructive call, which is never auto-approved;
- refuses a call to anything that is not one of the routine's connections,
  before PaymentGuard or PreFlight could pause the run on it.

A refusal dispatches nothing and is recorded as a failed step with the
``scope_approval`` verdict ``warn / scope_not_allowed``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from superagent.config import settings
from superagent.middleware import pipeline as pipeline_mod
from superagent.middleware.observers import NoOpObserver, StepResult, set_observer
from superagent.middleware.pipeline import ExecutionMiddleware
from superagent.middleware.scope_classes import ScopeClass
from superagent.middleware.scope_gate import (
    SCOPE_NOT_ALLOWED,
    ScopeApprovalRequired,
    ScopeNotAllowed,
    scope_gate,
)

DID = "did:orcha:agent:gh-1a2b3c4d"
OTHER = "did:orcha:agent:slack-9f8e7d6c"
CALL = "call_7"
ROUTINE = {
    "routine_id": "wf-1",
    "firing_id": "f-1",
    "connections": [DID],
    "scope_allow": [f"{DID}#create_comment"],
}


def _manifest(**extra: Any) -> dict[str, Any]:
    return {
        "agent_id": DID,
        "name": "GitHub",
        "tags": ["mcp", "user", "connection"],
        "is_active": True,
        "transport": {"type": "sse", "endpoint": "https://example.com/mcp"},
        "security": {"auth_strategies": []},
        "capabilities": [],
        **extra,
    }


def _gate(capability: str, agent_id: str = DID, **kw: Any) -> ScopeClass:
    return scope_gate(
        agent_id=agent_id,
        capability_id=capability,
        args={"owner": "o", "repo": "r"},
        call_id=CALL,
        session_id="s1",
        manifest=kw.pop("manifest", _manifest()),
        routine=kw.pop("routine", ROUTINE),
        **kw,
    )


# -- the gate ---------------------------------------------------------------


def test_a_read_passes_on_a_firing() -> None:
    assert _gate("list_issues") is ScopeClass.READ


def test_a_write_the_routine_allows_passes() -> None:
    assert _gate("create_comment") is ScopeClass.WRITE


def test_a_write_outside_the_allows_is_refused_not_paused() -> None:
    with pytest.raises(ScopeNotAllowed) as err:
        _gate("create_issue")
    assert str(err.value).startswith(f"{SCOPE_NOT_ALLOWED}: 'create_issue'")
    assert not isinstance(err.value, ScopeApprovalRequired)


def test_the_connection_manifest_allow_does_not_widen_a_routine() -> None:
    # Outside a firing a manifest allow passes a write; on a firing only the
    # routine's own allows count.
    with pytest.raises(ScopeNotAllowed):
        _gate("create_issue", manifest=_manifest(scope_allow=["create_issue"]))


def test_an_allow_for_another_connection_does_not_cover_this_one() -> None:
    routine = {
        **ROUTINE,
        "connections": [DID, OTHER],
        "scope_allow": [f"{OTHER}#create_issue"],
    }
    with pytest.raises(ScopeNotAllowed):
        _gate("create_issue", routine=routine)


def test_a_destructive_call_still_pauses_and_is_never_auto_approved() -> None:
    routine = {**ROUTINE, "scope_allow": [f"{DID}#delete_repo"]}  # save refuses this
    with pytest.raises(ScopeApprovalRequired) as err:
        _gate("delete_repo", routine=routine)
    assert err.value.scope_class is ScopeClass.DESTRUCTIVE


def test_the_owner_approval_for_this_call_passes_the_destructive_call() -> None:
    approval = {"call_id": CALL, "approver": "did:orcha:user:u1"}
    assert _gate("delete_repo", scope_approval=approval) is ScopeClass.DESTRUCTIVE


def test_a_connection_outside_the_routine_is_refused_even_for_a_read() -> None:
    with pytest.raises(ScopeNotAllowed) as err:
        _gate("list_issues", agent_id=OTHER)
    assert OTHER in str(err.value)


def test_platform_tools_keep_their_standing_allow_on_a_firing() -> None:
    assert _gate("save_artifact", agent_id="_system") is ScopeClass.WRITE


def test_without_a_routine_a_write_still_pauses_as_before() -> None:
    with pytest.raises(ScopeApprovalRequired):
        _gate("create_issue", routine=None)


# -- the pipeline -------------------------------------------------------------


class Recorder:
    def __init__(self) -> None:
        self.records: list[StepResult] = []

    async def on_step_complete(self, record: StepResult) -> None:
        self.records.append(record)


@pytest.fixture
def recorder():
    rec = Recorder()
    set_observer(rec)
    yield rec
    set_observer(NoOpObserver())


@pytest.fixture
def connections_on(monkeypatch):
    monkeypatch.setattr(settings, "connections_enabled", True)


async def _run(
    agent_id: str, capability: str, manifest: dict[str, Any], protocol: str = "MCP"
) -> tuple[dict[str, Any] | None, AsyncMock, AsyncMock, BaseException | None]:
    mw = ExecutionMiddleware(
        state={"session_id": "s1", "user_id": "u1", "routine_context": ROUTINE}
    )
    dispatch = AsyncMock(return_value="done")
    preflight = AsyncMock(
        return_value={"headers": {}, "manifest": manifest, "resolved_env": None}
    )
    raised: BaseException | None = None
    result = None
    with (
        patch.object(mw, "_get_capability_schema", AsyncMock(return_value=None)),
        patch.object(pipeline_mod.PreFlightManager, "run", preflight),
        patch.object(mw, "_resolve_base_fee", AsyncMock(return_value=0)),
        patch.object(mw, "_dispatch", dispatch),
        patch(
            "superagent.middleware.pipeline.OutputNormalizer.normalize",
            AsyncMock(return_value={"content": "done"}),
        ),
    ):
        try:
            result = await mw.execute(
                agent_id=agent_id,
                capability_id=capability,
                protocol=protocol,
                tool_name=f"t__{capability}",
                args={"owner": "o"},
                call_id=CALL,
            )
        except ScopeApprovalRequired as exc:
            raised = exc
    return result, dispatch, preflight, raised


async def test_the_pipeline_refuses_a_write_outside_the_allows(
    connections_on, recorder
) -> None:
    result, dispatch, _, raised = await _run(DID, "create_issue", _manifest())
    assert raised is None
    assert result["content"].startswith(f"Error: {SCOPE_NOT_ALLOWED}")
    dispatch.assert_not_awaited()
    (step,) = recorder.records
    assert step.success is False
    assert step.call_id == CALL
    assert step.metadata["scope_approval"] == {
        "result": "warn",
        "detail": SCOPE_NOT_ALLOWED,
    }


async def test_the_pipeline_dispatches_an_allowed_write(
    connections_on, recorder
) -> None:
    result, dispatch, _, raised = await _run(DID, "create_comment", _manifest())
    assert raised is None
    dispatch.assert_awaited_once()
    assert "scope_approval" not in recorder.records[0].metadata


async def test_the_pipeline_pauses_a_destructive_call_on_a_firing(
    connections_on, recorder
) -> None:
    _, dispatch, _, raised = await _run(DID, "delete_repo", _manifest())
    assert isinstance(raised, ScopeApprovalRequired)
    dispatch.assert_not_awaited()


@pytest.mark.parametrize("protocol", ["MCP", "A2A"])
async def test_an_agent_outside_the_routine_is_refused_before_preflight(
    connections_on, recorder, protocol
) -> None:
    # Not one of the routine's connections — not even a connection at all:
    # refused before PaymentGuard or PreFlight could pause the firing on it.
    result, dispatch, preflight, raised = await _run(
        OTHER, "send_message", _manifest(agent_id=OTHER, tags=["a2a"]), protocol
    )
    assert raised is None
    assert OTHER in result["content"]
    preflight.assert_not_awaited()
    dispatch.assert_not_awaited()
    assert recorder.records[0].metadata["scope_approval"]["detail"] == SCOPE_NOT_ALLOWED
