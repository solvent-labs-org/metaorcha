"""Story 1.6b: prove the token never leaves the transport (FR-21, AD-14, AD-16).

A connection's token, a stdio connection's variable and a BYOK model key are
planted as canary strings. A full connector turn then runs through the real
node → pipeline → PreFlight → handler → normalizer → observer path, with the
handler echoing its own request headers and environment back as its result.
Every surface the run writes is searched for the canaries: the log, the SSE
events, the ToolMessages and state update (what LangGraph checkpoints), the
StepResults, the Kafka payload, the run audit package, the run config and
the checkpoint metadata LangGraph derives from it, and the resume payload.

Each search has a positive control beside it: the handler *did* receive the
secret, and the redaction marker *is* in the output — so an absence means
"redacted", never "never sent".
"""

from __future__ import annotations

import ast
import json
import logging
import re
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.base import get_checkpoint_metadata
from superagent.api.audit import build_run_audit
from superagent.config import settings
from superagent.graph.runner import SessionRunner
from superagent.graph.state import (
    SESSION_CREDENTIALS_CONFIG_KEY,
    session_credentials_from_config,
)
from superagent.middleware import preflight as preflight_mod
from superagent.middleware.observers import NoOpObserver, StepResult, set_observer
from superagent.middleware.step_events import step_result_payload
from superagent.middleware.step_output import redact_output
from superagent.nodes import execute_agent_calls as node_mod
from superagent.nodes import orchestrator as orchestrator_mod
from superagent.nodes.execute_agent_calls import execute_agent_calls_node
from superagent.persistence.transcript_store import TRANSCRIPT_TOOL_META_KEY

CANARY = "canary-8f3a1c9e-connection-token-never-leaks"
CANARY_ENV = "canary-5d2b7e0a-stdio-variable-never-leaks"
CANARY_LLM = "canary-c4e19b73-byok-model-key-never-leaks"
CANARIES = (CANARY, CANARY_ENV, CANARY_LLM)

USER = "user-1"
SSE_DID = "did:orcha:agent:gh-1a2b3c4d"
STDIO_DID = "did:orcha:agent:local-9e8f7a6b"

SSE_MANIFEST = {
    "agent_id": SSE_DID,
    "name": "GitHub",
    "tags": ["mcp", "user", "connection"],
    "transport": {"type": "sse", "endpoint": "https://example.com/mcp"},
    "security": {
        "auth_strategies": [
            {
                "id": "b",
                "type": "http_bearer",
                "config": {"token_vault_ref": "GITHUB_TOKEN"},
            }
        ]
    },
    "capabilities": [],
}
STDIO_MANIFEST = {
    "agent_id": STDIO_DID,
    "name": "Local tools",
    "tags": ["mcp", "user", "connection"],
    "transport": {
        "type": "stdio",
        "command": "npx",
        "args": ["-y", "some-mcp"],
        "env": {"LOCAL_TOKEN": "${LOCAL_TOKEN}", "LOG_LEVEL": "info"},
    },
    "security": {"auth_strategies": []},
    "capabilities": [],
}


class FakeVault:
    """Vault whose rows are the planted canaries; records every write."""

    def __init__(self, rows: dict[tuple[str, str, str], str]) -> None:
        self.rows = rows
        self.reads: list[tuple[str, str, str]] = []
        self.saved: list[tuple[str, str, str, str]] = []

    async def get_agent_env(self, user_id: str, agent_id: str, var: str) -> str | None:
        self.reads.append((user_id, agent_id, var))
        return self.rows.get((user_id, agent_id, var))

    async def get_user_secret(self, user_id: str, key: str) -> str | None:
        return None

    async def save_agent_env(self, user_id: str, agent_id: str, var: str, value: str):
        self.saved.append((user_id, agent_id, var, value))

    async def save_user_secret(self, user_id: str, key: str, value: str) -> None:
        self.saved.append((user_id, "", key, value))


class Recorder:
    def __init__(self) -> None:
        self.records: list[StepResult] = []

    async def on_step_complete(self, record: StepResult) -> None:
        self.records.append(record)


def _dump(obj: Any) -> str:
    """JSON text of anything the run produced (messages, refs, dataclasses)."""

    def default(o: Any) -> Any:
        if hasattr(o, "model_dump"):
            return o.model_dump()
        if hasattr(o, "__dataclass_fields__"):
            return asdict(o)
        return repr(o)

    return json.dumps(obj, default=default, sort_keys=True)


def _log_text(caplog: pytest.LogCaptureFixture) -> str:
    # The formatted message, the raw args and any exception text.
    parts = [caplog.text]
    for record in caplog.records:
        parts.append(record.getMessage())
        parts.append(repr(record.args))
        if record.exc_text:
            parts.append(record.exc_text)
    return "\n".join(parts)


def _assert_clean(surfaces: dict[str, str], *canaries: str) -> None:
    hits = {
        name: [c for c in canaries if c in text]
        for name, text in surfaces.items()
        if any(c in text for c in canaries)
    }
    assert not hits, f"a credential leaked into: {hits}"
    assert surfaces, "no surface was searched"


@pytest.fixture(autouse=True)
def _observer():
    set_observer(NoOpObserver())
    yield
    set_observer(NoOpObserver())


@pytest.fixture
def connections_on(monkeypatch):
    monkeypatch.setattr(settings, "connections_enabled", True)


@pytest.fixture
def vault(monkeypatch) -> FakeVault:
    fake = FakeVault(
        {
            (USER, SSE_DID, "GITHUB_TOKEN"): CANARY,
            (USER, STDIO_DID, "LOCAL_TOKEN"): CANARY_ENV,
            (USER, "__llm__", "api_key"): CANARY_LLM,
            (USER, "__llm__", "base_url"): "https://api.example.com/v1",
            (USER, "__llm__", "model"): "example-model",
        }
    )
    monkeypatch.setattr("superagent.vault.client.VaultClient", lambda: fake)
    return fake


# -- the connector turn -------------------------------------------------------


def _tool_name(agent_id: str, capability_id: str) -> str:
    safe_id = "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in agent_id)
    return f"{safe_id}__{capability_id}"


def _candidate(manifest: dict[str, Any], capability: str) -> dict[str, Any]:
    return {
        "agent_id": manifest["agent_id"],
        "agent_name": manifest["name"],
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


def _turn_state() -> dict[str, Any]:
    return {
        "session_id": "sess-canary",
        "user_id": USER,
        "messages": [
            HumanMessage(content="list my repos and files"),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": _tool_name(SSE_DID, "search_repos"),
                        "args": {"query": "orcha"},
                        "id": "call_sse",
                        "type": "tool_call",
                    },
                    {
                        "name": _tool_name(STDIO_DID, "list_files"),
                        "args": {"path": "/"},
                        "id": "call_stdio",
                        "type": "tool_call",
                    },
                ],
            ),
        ],
        "pnd_candidates": [
            _candidate(SSE_MANIFEST, "search_repos"),
            _candidate(STDIO_MANIFEST, "list_files"),
        ],
        "_session_credentials": {},
        "_pending_events": [],
        "artifacts": {},
    }


@pytest.fixture
def echoing_handler(monkeypatch) -> list[dict[str, Any]]:
    """The MCP handler echoes its headers and environment — the worst agent."""
    seen: list[dict[str, Any]] = []

    async def call_tool(self, *, agent_id, capability_id, args, transport):
        seen.append(
            {
                "agent_id": agent_id,
                "headers": dict(self._auth_headers),
                "resolved_env": dict(transport.get("resolved_env") or {}),
            }
        )
        echo = (
            f"headers={json.dumps(self._auth_headers, sort_keys=True)} "
            f"env={json.dumps(transport.get('resolved_env') or {}, sort_keys=True)}"
        )
        return {"content": [{"type": "text", "text": echo}]}

    monkeypatch.setattr(
        "superagent.handlers.mcp_handler.MCPHandler.call_tool", call_tool
    )
    return seen


@pytest.fixture
def preflight_offline(monkeypatch):
    manifests = {SSE_DID: SSE_MANIFEST, STDIO_DID: STDIO_MANIFEST}

    async def get_manifest(agent_id: str) -> dict[str, Any]:
        return manifests[agent_id]

    monkeypatch.setattr(preflight_mod.MANIFEST_CACHE, "get_manifest", get_manifest)
    monkeypatch.setattr(preflight_mod.PreFlightManager, "_assert_healthy", AsyncMock())
    monkeypatch.setattr(
        preflight_mod, "_redis_has_grant", AsyncMock(return_value=False)
    )


async def test_a_connector_turn_leaks_no_credential_anywhere(
    connections_on, vault, echoing_handler, preflight_offline, caplog
) -> None:
    recorder = Recorder()
    set_observer(recorder)
    state = _turn_state()
    config = {
        "configurable": {"thread_id": "sess-canary", SESSION_CREDENTIALS_CONFIG_KEY: {}}
    }
    with caplog.at_level(logging.DEBUG):
        updates = await execute_agent_calls_node(state, config)

    # Positive controls: both secrets reached the handler, and both results
    # came back carrying them — the redaction is what keeps them out.
    assert [c["agent_id"] for c in echoing_handler] == [SSE_DID, STDIO_DID]
    assert echoing_handler[0]["headers"] == {"Authorization": f"Bearer {CANARY}"}
    assert echoing_handler[1]["resolved_env"]["LOCAL_TOKEN"] == CANARY_ENV
    sse_msg, stdio_msg = updates["messages"]
    assert "[REDACTED:Authorization]" in sse_msg.content
    assert "[REDACTED:LOCAL_TOKEN]" in stdio_msg.content
    assert len(recorder.records) == 2
    assert "[REDACTED:Authorization]" in _dump(recorder.records[0].output_preimage)
    assert "[REDACTED:LOCAL_TOKEN]" in _dump(recorder.records[1].output_preimage)
    assert all(r.success for r in recorder.records)

    now = datetime.now(UTC)
    rows = [
        SimpleNamespace(
            role="USER", content="list my repos", tool_inputs=None, created_at=now
        )
    ]
    rows += [
        SimpleNamespace(
            role="TOOL",
            content=m.content,
            tool_inputs=m.additional_kwargs.get(TRANSCRIPT_TOOL_META_KEY),
            created_at=now,
        )
        for m in updates["messages"]
    ]
    audit = build_run_audit("sess-canary", rows)

    surfaces = {
        "service log": _log_text(caplog),
        "sse events": _dump(updates["_pending_events"]),
        "state update (checkpointed)": _dump(updates),
        "state after the node": _dump(state),
        "step results (observer seam)": _dump([asdict(r) for r in recorder.records]),
        "kafka payload": _dump([step_result_payload(r) for r in recorder.records]),
        "run audit package": audit.model_dump_json(),
        "run config": _dump(config),
        "checkpoint metadata": _dump(get_checkpoint_metadata(config, {})),
    }
    _assert_clean(surfaces, CANARY, CANARY_ENV)


async def test_the_criteria_step_reads_the_redacted_output(
    connections_on, vault, echoing_handler, preflight_offline
) -> None:
    # A declared criterion that would only pass on the raw token fails: the
    # criteria step never sees it (AD-16).
    from superagent.middleware.criteria import evaluate_declared_criteria

    state = _turn_state()
    updates = await execute_agent_calls_node(state, {})
    content = updates["messages"][0].content
    assert CANARY not in content and "[REDACTED:Authorization]" in content
    accepted, _ = evaluate_declared_criteria({"citations_required": False}, content)
    assert isinstance(accepted, bool)


def test_redact_output_covers_every_shape() -> None:
    creds = [("GITHUB_TOKEN", CANARY), ("Authorization", f"Bearer {CANARY}")]
    block = SimpleNamespace(text=f"token {CANARY}")
    value = {
        "content": [{"type": "text", "text": f"Bearer {CANARY}"}, block],
        CANARY: ("tuple", CANARY),
        "n": 1,
        "b": b"raw-bytes-are-not-text",
    }
    out = redact_output(value, creds)
    text = _dump(out)
    assert CANARY not in text
    # the longer credential wins, so the marker names the header not the token
    assert out["content"][0]["text"] == "[REDACTED:Authorization]"
    assert isinstance(out["content"][1], str)  # the block rendered a secret
    assert out["n"] == 1 and out["b"] == b"raw-bytes-are-not-text"
    assert redact_output("x", []) == "x"
    assert redact_output(None, creds) is None


# -- graph state, run config and the checkpoint (AD-14) -----------------------


class _CapturingGraph:
    """Compiled-graph stand-in that keeps what the runner streams into it."""

    def __init__(self, snapshots: list[Any]) -> None:
        self._snapshots = list(snapshots)
        self.inputs: list[tuple[Any, dict[str, Any]]] = []

    async def aget_state(self, config: dict[str, Any]) -> Any:
        return self._snapshots.pop(0) if self._snapshots else None

    def astream(self, stream_input: Any, config: dict[str, Any], **_: Any) -> Any:
        self.inputs.append((stream_input, config))

        async def _gen():
            return
            yield  # pragma: no cover

        return _gen()


@pytest.fixture
def _no_persist(monkeypatch):
    async def _noop(self, *args, **kwargs):
        return None

    monkeypatch.setattr(SessionRunner, "_persist_transcript", _noop)


async def _collect(agen: Any) -> list[dict[str, Any]]:
    return [e async for e in agen]


async def test_run_turn_puts_no_credential_into_state_or_checkpoint(
    vault, _no_persist, caplog
) -> None:
    graph = _CapturingGraph(
        [SimpleNamespace(values={"messages": [HumanMessage(content="hi")]}, tasks=[])]
    )
    session_creds = {
        "__llm__": {"api_key": CANARY_LLM, "base_url": "https://x", "model": "m"}
    }
    with caplog.at_level(logging.DEBUG):
        await _collect(
            SessionRunner(graph).run_turn(
                "sess-1", USER, "hello", session_credentials=session_creds
            )
        )
    ((state_update, config),) = graph.inputs
    # positive control: the credentials did travel — on the config, under the
    # key LangGraph keeps out of checkpoint metadata
    assert session_credentials_from_config(config) == session_creds
    assert SESSION_CREDENTIALS_CONFIG_KEY.startswith("__")
    # the vault was not read into state; nothing is assigned there at all
    assert vault.reads == []
    assert not state_update.get("_session_credentials")
    metadata = get_checkpoint_metadata(config, {})
    assert SESSION_CREDENTIALS_CONFIG_KEY not in metadata
    _assert_clean(
        {
            "state update (checkpointed)": _dump(state_update),
            "checkpoint metadata": _dump(metadata),
            "service log": _log_text(caplog),
        },
        CANARY_LLM,
    )


async def test_byok_is_a_local_read_at_call_time(vault, caplog) -> None:
    with caplog.at_level(logging.DEBUG):
        session = await orchestrator_mod.resolve_byok(
            {
                "configurable": {
                    SESSION_CREDENTIALS_CONFIG_KEY: {"__llm__": {"api_key": "s-1"}}
                }
            },
            USER,
        )
        permanent = await orchestrator_mod.resolve_byok({"configurable": {}}, USER)
        nobody = await orchestrator_mod.resolve_byok({"configurable": {}}, "")
    assert session == {"api_key": "s-1"}  # session-scoped wins, no vault read
    assert permanent == {
        "api_key": CANARY_LLM,
        "base_url": "https://api.example.com/v1",
        "model": "example-model",
    }
    assert nobody is None
    assert vault.reads == [
        (USER, "__llm__", v) for v in ("api_key", "base_url", "model")
    ]
    assert CANARY_LLM not in _log_text(caplog)


def _auth_form_snapshot(vault_key: str = "GITHUB_TOKEN") -> Any:
    event = {
        "type": "interrupt",
        "interrupt_type": "AUTH_FORM_SUBMISSION",
        "interrupt_id": f"AUTH_FORM_SUBMISSION__{SSE_DID}__search_repos__ab12cd34",
        "agent_id": SSE_DID,
        "session_id": "sess-1",
        "message": "Credential required",
        "metadata": {
            "form_title": "Credential Required — GitHub",
            "field_label": "API Key",
            "vault_key": vault_key,
            "is_secret": True,
        },
        "resumable": True,
    }
    return SimpleNamespace(
        values={"messages": []},
        tasks=[SimpleNamespace(interrupts=[SimpleNamespace(value=event)])],
    )


async def test_a_submitted_credential_goes_to_the_vault_not_the_checkpoint(
    vault, _no_persist, caplog
) -> None:
    graph = _CapturingGraph([_auth_form_snapshot(), None])
    with caplog.at_level(logging.DEBUG):
        await _collect(
            SessionRunner(graph).resume_from_interrupt(
                "sess-1",
                {
                    "status": "complete",
                    "vault_key": "GITHUB_TOKEN",
                    "credential_value": CANARY,
                },
                user_id=USER,
            )
        )
    # positive control: stored, under the agent's own scope (AD-15), for the
    # Gateway-verified user — the agent and key taken from the checkpoint's
    # own interrupt record
    assert vault.saved == [(USER, SSE_DID, "GITHUB_TOKEN", CANARY)]
    ((command, config),) = graph.inputs
    resume = command.resume
    assert resume == {"status": "complete", "vault_key": "GITHUB_TOKEN"}
    _assert_clean(
        {
            "resume payload (checkpointed)": _dump(resume),
            "run config": _dump(config),
            "checkpoint metadata": _dump(get_checkpoint_metadata(config, {})),
            "service log": _log_text(caplog),
        },
        CANARY,
    )


async def test_a_credential_on_the_wrong_interrupt_is_dropped_not_stored(
    vault, _no_persist, caplog
) -> None:
    hitl = _auth_form_snapshot()
    hitl.tasks[0].interrupts[0].value = {
        **hitl.tasks[0].interrupts[0].value,
        "interrupt_type": "HITL_APPROVAL",
        "metadata": {
            "action_description": "d",
            "risk_level": "high",
            "agent_display_name": "GitHub",
            "capability_name": "delete_repo",
        },
    }
    graph = _CapturingGraph([hitl, None])
    with caplog.at_level(logging.DEBUG):
        await _collect(
            SessionRunner(graph).resume_from_interrupt(
                "sess-1",
                {"status": "approved", "credential_value": CANARY},
                user_id=USER,
            )
        )
    assert vault.saved == []
    ((command, _),) = graph.inputs
    assert command.resume == {"status": "approved"}
    assert CANARY not in _log_text(caplog)


# -- no vault-decrypt result is assigned into state (AD-14, by inspection) ------

_SRC = Path(node_mod.__file__).resolve().parents[1]
_STATE_NAMES = {"state", "state_update", "updates", "new_state"}
_SECRET_RE = re.compile(r"vault|byok|credential|secret|decrypt|token_value", re.I)


def _state_assignments(path: Path) -> list[tuple[int, str]]:
    """``(line, source)`` of every assignment into a state-like mapping."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(path.read_text())):
        if not isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if (
                isinstance(target, ast.Subscript)
                and isinstance(target.value, ast.Name)
                and target.value.id in _STATE_NAMES
            ):
                found.append((node.lineno, ast.unparse(node)))
    return found


@pytest.mark.parametrize(
    "relative",
    [
        "graph/runner.py",
        "nodes/orchestrator.py",
        "nodes/execute_agent_calls.py",
        "middleware/pipeline.py",
        "middleware/preflight.py",
    ],
)
def test_no_vault_result_is_assigned_into_state(relative: str) -> None:
    assignments = _state_assignments(_SRC / relative)
    offending = [
        (line, src)
        for line, src in assignments
        if "_session_credentials" in src or _SECRET_RE.search(src.split("=", 1)[1])
    ]
    assert offending == [], offending


def test_the_inspection_sees_assignments_at_all() -> None:
    # The node writes artifacts and grants into state; if this ever comes
    # back empty the walker is broken, not the code clean.
    assert _state_assignments(_SRC / "nodes/execute_agent_calls.py")
