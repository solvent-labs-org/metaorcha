"""Story 1.5: a write pauses for a human, a destructive call always does.

The gate (AD-18) sits at every dispatch site; a write on a connection with no
declared allow and every destructive call raise ``ScopeApprovalRequired``
before any request reaches the platform. The node turns that into a
``HITL_APPROVAL`` interrupt; approval re-runs the call with the approver
named in the step, a decline records a ``warn`` verdict and dispatches
nothing (AD-21). With ``CONNECTIONS_ENABLED`` off the gate is never invoked.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage
from superagent.config import settings
from superagent.middleware import pipeline as pipeline_mod
from superagent.middleware.observers import NoOpObserver, StepResult, set_observer
from superagent.middleware.pipeline import ExecutionMiddleware, _is_control_flow
from superagent.middleware.scope_classes import SYSTEM_TOOL_CLASSES, ScopeClass
from superagent.middleware.scope_gate import (
    SCOPE_DECLINED,
    ScopeApprovalRequired,
    approval_for,
    approver_did,
    scope_gate,
    scope_verdict,
    target_summary,
)
from superagent.nodes import execute_agent_calls as node_mod
from superagent.nodes.execute_agent_calls import execute_agent_calls_node
from superagent.system_tools.registry import (
    SYSTEM_TOOL_REGISTRY,
    register_all_system_tools,
)

DID = "did:orcha:agent:gh-1a2b3c4d"
CALL = "call_7"


def _manifest(**extra: Any) -> dict[str, Any]:
    return {
        "agent_id": DID,
        "name": "GitHub",
        "tags": ["mcp", "user", "connection"],
        "transport": {"type": "sse", "endpoint": "https://example.com/mcp"},
        "security": {"auth_strategies": []},
        "capabilities": [],
        **extra,
    }


def _gate(capability: str, **kw: Any) -> ScopeClass:
    return scope_gate(
        agent_id=DID,
        capability_id=capability,
        args=kw.pop("args", {"owner": "o", "repo": "r"}),
        call_id=CALL,
        session_id="s1",
        manifest=kw.pop("manifest", _manifest()),
        **kw,
    )


@pytest.fixture
def connections_on(monkeypatch):
    monkeypatch.setattr(settings, "connections_enabled", True)


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


# -- the gate itself ---------------------------------------------------------


def test_a_read_passes_without_anyone() -> None:
    assert _gate("list_issues") is ScopeClass.READ


def test_a_write_with_no_declared_allow_pauses() -> None:
    with pytest.raises(ScopeApprovalRequired) as info:
        _gate("create_issue")
    event = info.value.event
    assert event.interrupt_type.value == "HITL_APPROVAL"
    meta = event.metadata
    assert meta["scope_class"] == "write"
    assert meta["capability_id"] == "create_issue"
    assert meta["connection_id"] == DID
    assert meta["connection_name"] == "GitHub"
    assert meta["call_id"] == CALL
    assert '"owner": "o"' in meta["target"] and '"repo": "r"' in meta["target"]
    assert meta["risk_level"] == "medium"
    assert event.resumable is True


def test_a_write_the_manifest_allows_passes() -> None:
    manifest = _manifest(scope_allow=["create_issue"])
    assert _gate("create_issue", manifest=manifest) is ScopeClass.WRITE


def test_an_approval_for_this_call_passes_it() -> None:
    approval = {"call_id": CALL, "approver": approver_did("u1")}
    assert _gate("create_issue", scope_approval=approval) is ScopeClass.WRITE


def test_an_approval_for_another_call_does_not_pass_it() -> None:
    approval = {"call_id": "call_other", "approver": approver_did("u1")}
    with pytest.raises(ScopeApprovalRequired):
        _gate("create_issue", scope_approval=approval)


@pytest.mark.parametrize(
    "path",
    [
        {},
        {"manifest": _manifest(scope_allow=["delete_repo"])},
        {"manifest": _manifest(scope_classes={"delete_repo": "read"})},
        {"override": "read"},
        {"scope_approval": {"call_id": "call_other", "approver": "did:orcha:user:u1"}},
        {"scope_approval": {"call_id": CALL, "approver": ""}},
        {"scope_approval": {"approver": "did:orcha:user:u1"}},
        {"scope_approval": "yes"},
    ],
)
def test_a_destructive_call_pauses_on_every_configuration_path(path) -> None:
    # Allow list, a loosening manifest class, a loosening override, a
    # mismatched or malformed approval: none of them skips the pause.
    with pytest.raises(ScopeApprovalRequired) as info:
        _gate("delete_repo", **path)
    assert info.value.scope_class is ScopeClass.DESTRUCTIVE
    assert info.value.event.metadata["risk_level"] == "high"


def test_an_unlisted_capability_is_destructive_and_pauses() -> None:
    with pytest.raises(ScopeApprovalRequired) as info:
        _gate("frobnicate")
    assert info.value.scope_class is ScopeClass.DESTRUCTIVE


def test_a_manifest_may_only_tighten() -> None:
    with pytest.raises(ScopeApprovalRequired) as info:
        _gate("list_issues", manifest=_manifest(scope_classes={"list_issues": "write"}))
    assert info.value.scope_class is ScopeClass.WRITE


def test_system_tools_pass_under_the_platform_standing_allow() -> None:
    for name, cls in SYSTEM_TOOL_CLASSES.items():
        assert (
            scope_gate(
                agent_id="_system",
                capability_id=name,
                args={},
                call_id=CALL,
                session_id="s1",
            )
            is cls
        )


def test_target_summary_prefers_named_keys_and_is_bounded() -> None:
    assert target_summary({"owner": "o", "repo": "r", "body": "x" * 500}) == (
        '{"owner": "o", "repo": "r"}'
    )
    assert len(target_summary({"blob": "x" * 500})) <= 200
    assert target_summary({}) == ""
    assert target_summary("not a dict") == ""


def test_approver_did_and_verdict_shapes() -> None:
    assert approver_did(" u1 ") == "did:orcha:user:u1"
    assert approval_for(CALL, {"authoriser_user_id": "u9"}, "owner") == {
        "call_id": CALL,
        "approver": "did:orcha:user:u9",
    }
    # The Gateway stamps authoriser_user_id; without it the session owner.
    assert approval_for(CALL, {"status": "approved"}, "owner")["approver"] == (
        "did:orcha:user:owner"
    )
    assert scope_verdict(approved=True, approver="did:orcha:user:u1") == {
        "result": "pass",
        "detail": "did:orcha:user:u1",
    }
    assert scope_verdict(approved=False) == {"result": "warn", "detail": "declined"}


# -- the pipeline slot -----------------------------------------------------


def _preflight_result(manifest: dict[str, Any]) -> dict[str, Any]:
    return {"headers": {}, "manifest": manifest, "resolved_env": None}


async def _run_pipeline(
    manifest: dict[str, Any],
    capability: str,
    *,
    scope_approval: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, AsyncMock, BaseException | None]:
    mw = ExecutionMiddleware(state={"session_id": "s1", "user_id": "u1"})
    dispatch = AsyncMock(return_value="done")
    raised: BaseException | None = None
    result = None
    with (
        patch.object(mw, "_get_capability_schema", AsyncMock(return_value=None)),
        patch.object(
            pipeline_mod.PreFlightManager,
            "run",
            AsyncMock(return_value=_preflight_result(manifest)),
        ),
        patch.object(mw, "_dispatch", dispatch),
        patch(
            "superagent.middleware.pipeline.OutputNormalizer.normalize",
            AsyncMock(return_value={"content": "done"}),
        ),
    ):
        try:
            result = await mw.execute(
                agent_id=DID,
                capability_id=capability,
                protocol="MCP",
                tool_name=f"gh__{capability}",
                args={"owner": "o"},
                call_id=CALL,
                scope_approval=scope_approval,
            )
        except ScopeApprovalRequired as exc:
            raised = exc
    return result, dispatch, raised


async def test_the_pipeline_pauses_a_write_before_any_request(
    connections_on, recorder
) -> None:
    result, dispatch, raised = await _run_pipeline(_manifest(), "create_issue")
    assert isinstance(raised, ScopeApprovalRequired)
    assert result is None
    dispatch.assert_not_awaited()
    assert recorder.records == []  # a pause is not a step


async def test_the_pipeline_records_the_approver_on_the_step(
    connections_on, recorder
) -> None:
    approval = {"call_id": CALL, "approver": "did:orcha:user:u1"}
    result, dispatch, raised = await _run_pipeline(
        _manifest(), "create_issue", scope_approval=approval
    )
    assert raised is None and result is not None
    dispatch.assert_awaited_once()
    (record,) = recorder.records
    assert record.metadata["scope_approval"] == {
        "result": "pass",
        "detail": "did:orcha:user:u1",
    }


async def test_a_read_records_no_approval(connections_on, recorder) -> None:
    _, dispatch, raised = await _run_pipeline(_manifest(), "list_issues")
    assert raised is None
    dispatch.assert_awaited_once()
    assert "scope_approval" not in recorder.records[0].metadata


async def test_flag_off_means_the_gate_is_not_invoked(recorder) -> None:
    # CONNECTIONS_ENABLED unset: the pipeline never calls scope_gate, so no
    # interrupt of this type can be raised. (PreFlight refuses connections
    # earlier in the real path; the gate must still not be the thing that
    # runs.)
    with patch("superagent.middleware.scope_gate.scope_gate") as gate:
        _, dispatch, raised = await _run_pipeline(_manifest(), "delete_repo")
    gate.assert_not_called()
    assert raised is None
    dispatch.assert_awaited_once()


async def test_an_agent_that_is_not_a_connection_is_not_gated(
    connections_on, recorder
) -> None:
    with patch("superagent.middleware.scope_gate.scope_gate") as gate:
        _, dispatch, raised = await _run_pipeline(
            _manifest(tags=["mcp"]), "delete_repo"
        )
    gate.assert_not_called()
    assert raised is None
    dispatch.assert_awaited_once()


def test_the_pause_is_control_flow_not_a_failed_step() -> None:
    with pytest.raises(ScopeApprovalRequired) as info:
        _gate("create_issue")
    assert _is_control_flow(info.value) is True


# -- every dispatch site calls the gate (AD-18) ------------------------------

_SRC = Path(node_mod.__file__).resolve().parents[1]


def _calls_in(path: Path, name: str) -> list[str]:
    """Names of the functions in ``path`` whose body calls ``name``."""
    tree = ast.parse(path.read_text())
    found: list[str] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for call in ast.walk(fn):
            if isinstance(call, ast.Call):
                target = call.func
                ident = (
                    target.id
                    if isinstance(target, ast.Name)
                    else target.attr
                    if isinstance(target, ast.Attribute)
                    else ""
                )
                if ident == name:
                    found.append(fn.name)
                    break
    return found


def test_every_dispatch_site_goes_through_the_one_gate() -> None:
    pipeline_src = _SRC / "middleware" / "pipeline.py"
    node_src = _SRC / "nodes" / "execute_agent_calls.py"
    # The two dispatch sites the spine names.
    assert "_scope_gate" in _calls_in(pipeline_src, "_dispatch_with_timeout") or (
        "execute" in _calls_in(pipeline_src, "_scope_gate")
    )
    assert "execute" in _calls_in(pipeline_src, "_scope_gate")
    assert "_scope_gate" in _calls_in(pipeline_src, "scope_gate")
    assert "execute_agent_calls_node" in _calls_in(node_src, "_system_tool_scope_gate")
    assert "_system_tool_scope_gate" in _calls_in(node_src, "scope_gate")
    # No handler is dispatched from anywhere but the pipeline's one path.
    assert _calls_in(pipeline_src, "_dispatch_with_timeout") == ["execute"]
    handler_calls = re.findall(r"\b\w+Handler\(", node_src.read_text())
    assert handler_calls == [], handler_calls
    # And the system-tool registry is called from the node only, right after
    # the gate.
    assert _calls_in(node_src, "call") == ["execute_agent_calls_node"]


# -- the node: approve, decline, system tools -------------------------------


def _tool_name(agent_id: str, capability_id: str) -> str:
    safe_id = "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in agent_id)
    return f"{safe_id}__{capability_id}"


def _state(capability: str) -> dict[str, Any]:
    return {
        "session_id": "s1",
        "user_id": "owner-1",
        "messages": [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": _tool_name(DID, capability),
                        "args": {"owner": "o", "repo": "r"},
                        "id": CALL,
                        "type": "tool_call",
                    }
                ],
            )
        ],
        "pnd_candidates": [
            {
                "agent_id": DID,
                "agent_name": "GitHub",
                "protocol_type": "MCP",
                "capabilities": [
                    {
                        "capability_id": capability,
                        "capability_type": "TOOL",
                        "description": "cap",
                        "input_schema": {"type": "object", "properties": {}},
                    }
                ],
            }
        ],
    }


def _pausing_execute(seen: list[dict[str, Any]]):
    """A middleware that pauses until it is handed an approval for the call."""

    async def fake_execute(self, **kwargs: Any) -> dict[str, Any]:
        seen.append(kwargs)
        approval = kwargs.get("scope_approval")
        if approval and approval.get("call_id") == kwargs["call_id"]:
            return {"content": "issue #12 created", "base_fee": "0"}
        raise ScopeApprovalRequired(
            _pause_event(kwargs["capability_id"]), ScopeClass.WRITE
        )

    return fake_execute


def _pause_event(capability: str):
    try:
        _gate(capability)
    except ScopeApprovalRequired as exc:
        return exc.event
    raise AssertionError("expected a pause")


async def test_approve_re_runs_the_call_with_the_approver(
    connections_on, monkeypatch, recorder
) -> None:
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(ExecutionMiddleware, "execute", _pausing_execute(seen))
    prompts: list[dict[str, Any]] = []
    monkeypatch.setattr(
        node_mod,
        "interrupt",
        lambda event: (
            prompts.append(event)
            or {"status": "approved", "authoriser_user_id": "approver-9"}
        ),
    )
    updates = await execute_agent_calls_node(_state("create_issue"), {})
    assert len(prompts) == 1 and prompts[0]["interrupt_type"] == "HITL_APPROVAL"
    assert prompts[0]["metadata"]["capability_id"] == "create_issue"
    # first pass paused, second pass carried the approval
    assert [k.get("scope_approval") for k in seen] == [
        None,
        {"call_id": CALL, "approver": "did:orcha:user:approver-9"},
    ]
    (msg,) = updates["messages"]
    assert msg.content == "issue #12 created"
    assert msg.additional_kwargs["transcript_meta"]["verified"] is True


async def test_decline_dispatches_nothing_and_records_a_warn(
    connections_on, monkeypatch, recorder
) -> None:
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(ExecutionMiddleware, "execute", _pausing_execute(seen))
    monkeypatch.setattr(node_mod, "interrupt", lambda event: {"status": "denied"})
    updates = await execute_agent_calls_node(_state("create_issue"), {})
    assert len(seen) == 1  # the paused pass only; no re-run
    (msg,) = updates["messages"]
    assert msg.content.startswith(f"Error: {SCOPE_DECLINED}")
    assert msg.additional_kwargs["transcript_meta"]["verified"] is False
    (record,) = recorder.records
    assert record.call_id == CALL
    assert record.success is False
    assert record.metadata["scope_approval"] == {
        "result": "warn",
        "detail": "declined",
    }
    events = [e for e in updates["_pending_events"] if e["type"] == "invocation_result"]
    assert events[-1]["status"] == "error"


async def test_a_retry_after_approval_is_not_re_gated(
    connections_on, monkeypatch, recorder
) -> None:
    seen: list[dict[str, Any]] = []
    attempts = {"n": 0}

    async def flaky_execute(self, **kwargs: Any) -> dict[str, Any]:
        seen.append(kwargs)
        approval = kwargs.get("scope_approval")
        if not (approval and approval.get("call_id") == kwargs["call_id"]):
            raise ScopeApprovalRequired(
                _pause_event(kwargs["capability_id"]), ScopeClass.WRITE
            )
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise TimeoutError("transient")
        return {"content": "created", "base_fee": "0"}

    monkeypatch.setattr(ExecutionMiddleware, "execute", flaky_execute)
    monkeypatch.setattr(node_mod, "interrupt", lambda event: {"status": "approved"})
    updates = await execute_agent_calls_node(_state("create_issue"), {})
    assert updates["messages"][0].content == "created"
    # pause, approved attempt (transient), retried attempt — with the same approval
    assert len(seen) == 3
    assert seen[1]["scope_approval"] == seen[2]["scope_approval"]
    assert seen[2]["scope_approval"]["approver"] == "did:orcha:user:owner-1"


@pytest.fixture
def all_tools_registered():
    register_all_system_tools()
    return SYSTEM_TOOL_REGISTRY


async def test_a_system_tool_goes_through_the_gate_and_passes(
    connections_on, all_tools_registered, monkeypatch
) -> None:
    gated: list[str] = []
    real = node_mod._system_tool_scope_gate

    def spy(state, tool_name, args, call_id):
        gated.append(tool_name)
        real(state, tool_name, args, call_id)

    monkeypatch.setattr(node_mod, "_system_tool_scope_gate", spy)
    state = _state("x")
    state["messages"][0].tool_calls[0]["name"] = "get_datetime"
    state["messages"][0].tool_calls[0]["args"] = {}
    updates = await execute_agent_calls_node(state, {})
    assert gated == ["get_datetime"]
    assert not updates["messages"][0].content.startswith("Error:")


async def test_flag_off_means_no_system_tool_gate_either(
    all_tools_registered, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "connections_enabled", False)
    with patch("superagent.middleware.scope_gate.scope_gate") as gate:
        state = _state("x")
        state["messages"][0].tool_calls[0]["name"] = "get_datetime"
        state["messages"][0].tool_calls[0]["args"] = {}
        await execute_agent_calls_node(state, {})
    gate.assert_not_called()


def test_a_connection_capability_named_like_a_system_tool_is_still_gated() -> None:
    # The standing allow is keyed on the dispatch site, never on the name.
    with pytest.raises(ScopeApprovalRequired):
        _gate("save_artifact")
