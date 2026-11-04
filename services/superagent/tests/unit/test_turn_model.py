"""Story 3.3: see and record which model ran the turn (FR-13, AD-21).

The orchestrator reads the model the provider says it served and where the
request went; every step of the turn carries it; the receipt signs it once
as ``{check: "model", result: "pass"}``; the stream's ``done`` event, the
assistant's transcript row and the session export show it. A ``pass``
verdict refuses nothing, so recording the model can never change an outcome.
"""

from __future__ import annotations

import sys
from enum import StrEnum
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_openai import ChatOpenAI
from superagent.api.audit import (
    SettlementEvidence,
    build_run_audit,
    load_settlement,
    run_models,
)
from superagent.config import settings
from superagent.graph import runner as runner_mod
from superagent.graph.runner import _done_event, _yield_multistream_events
from superagent.graph.state import default_state
from superagent.middleware.observers import NoOpObserver, StepResult, set_observer
from superagent.middleware.system_steps import attest_system_tool_step
from superagent.nodes import orchestrator
from superagent.nodes.orchestrator import _make_chat_llm, orchestrator_llm_node
from superagent.persistence import transcript_store
from superagent.persistence.transcript_store import (
    _assistant_model,
    messages_to_entry_dicts,
)
from superagent.turn_model import TURN_MODEL_KEY, clean, route, turn_model

from .test_criterion_step_coverage import (  # noqa: F401 — the `sealed` fixture
    SESSION,
    _agent_step,
    _state,
    _system_call,
    sealed,
)

# ---------------------------------------------------------------------------
# turn_model: pure and total
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _generated_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """CI has no generated Prisma client; the transcript store imports its enums
    at call time. Supply the one enum these tests reach, shaped as Prisma's
    (a str enum whose values are the member names)."""

    class TranscriptRole(StrEnum):
        USER = "USER"
        ASSISTANT = "ASSISTANT"
        TOOL = "TOOL"

    enums = ModuleType("src.generated_client.enums")
    enums.TranscriptRole = TranscriptRole
    package = ModuleType("src.generated_client")
    package.enums = enums
    monkeypatch.setitem(sys.modules, "src.generated_client", package)
    monkeypatch.setitem(sys.modules, "src.generated_client.enums", enums)


def _openai(base_url: str, model: str = "m") -> ChatOpenAI:
    return ChatOpenAI(model=model, api_key="k", base_url=base_url)


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        ("https://openrouter.ai/api/v1", "openrouter.ai"),
        ("https://gateway.openrouter.ai/v1", "gateway.openrouter.ai"),
        ("http://localhost:11434/v1", "local"),
        ("http://127.0.0.1:8080/v1", "local"),
        ("http://10.0.0.7:9000/v1", "local"),
        ("http://192.168.1.20/v1", "local"),
        ("http://[fe80::1]:11434/v1", "local"),
        ("http://ollama:11434/v1", "local"),
        ("http://host.docker.internal:11434/v1", "local"),
        # a public host is never "local", whatever port it listens on
        ("http://inference.example.net:11434/v1", "inference.example.net"),
        ("https://gpu.example.com:11434/v1", "gpu.example.com"),
        ("https://api.groq.com/openai/v1", "api.groq.com"),
    ],
)
def test_route_names_where_the_request_goes(base_url: str, expected: str) -> None:
    assert route(_openai(base_url)) == expected


def test_route_knows_the_native_gemini_client_and_nothing_else() -> None:
    gemini = ChatGoogleGenerativeAI(model="gemini-2.5-flash", google_api_key="k")
    assert route(gemini) == "generativelanguage.googleapis.com"
    assert route(object()) == "unknown"
    assert route(SimpleNamespace(openai_api_base="not a url")) == "unknown"


def test_turn_model_prefers_the_model_the_provider_served() -> None:
    chat = _openai("https://openrouter.ai/api/v1", model="anthropic/claude-haiku-4.5")
    served = AIMessage(
        content="", response_metadata={"model_name": "anthropic/claude-haiku-4.5-1"}
    )
    assert turn_model(chat, served) == "openrouter.ai/anthropic/claude-haiku-4.5-1"
    # no metadata: the id the client was built with
    assert turn_model(chat, AIMessage(content="")) == (
        "openrouter.ai/anthropic/claude-haiku-4.5"
    )
    assert turn_model(chat, None) == "openrouter.ai/anthropic/claude-haiku-4.5"


def test_turn_model_is_verdict_safe_and_never_raises() -> None:
    chat = _openai("http://localhost:11434/v1", model="qwen2.5:3b")
    odd = AIMessage(content="", response_metadata={"model_name": " qwén☃ 2.5 "})
    assert turn_model(chat, odd) == "local/qwn 2.5"
    long = AIMessage(content="", response_metadata={"model_name": "x" * 500})
    assert len(turn_model(chat, long)) == 200
    assert turn_model(chat, SimpleNamespace(response_metadata="junk")) == (
        "local/qwen2.5:3b"
    )
    assert turn_model(SimpleNamespace(model_name=""), None) is None
    assert turn_model(SimpleNamespace(model_name=7), None) is None
    assert clean("☃") is None
    assert clean(None) is None


# ---------------------------------------------------------------------------
# The orchestrator records the model of each call
# ---------------------------------------------------------------------------


@pytest.fixture()
def quiet_node(monkeypatch):
    """The node with no PnD, no registry and no network: only the LLM call."""
    monkeypatch.setattr(orchestrator, "pnd_gate", AsyncMock(return_value=False))
    monkeypatch.setattr(orchestrator, "get_baseline_openai_tools", lambda: [])
    monkeypatch.setattr(orchestrator, "resolve_byok", AsyncMock(return_value=None))

    def install(reply: Any):
        monkeypatch.setattr(
            orchestrator, "_accumulate_chat_stream", AsyncMock(return_value=reply)
        )

    return install


def _turn_state(override: str | None) -> dict[str, Any]:
    state = default_state("s-model", "u1")
    state.update(
        {
            "messages": [HumanMessage(content="hello")],
            "orchestrator_model_override": override,
        }
    )
    return state


async def test_two_turns_on_different_models_each_record_their_own(
    quiet_node,
) -> None:
    quiet_node(AIMessage(content="first"))
    first = await orchestrator_llm_node(_turn_state("model-a"), {"configurable": {}})
    quiet_node(AIMessage(content="second"))
    second = await orchestrator_llm_node(_turn_state("model-b"), {"configurable": {}})

    assert first["_turn_model"] == "openrouter.ai/model-a"
    assert second["_turn_model"] == "openrouter.ai/model-b"
    # the message itself carries it, for the transcript row
    assert first["messages"][0].response_metadata[TURN_MODEL_KEY] == (
        "openrouter.ai/model-a"
    )


async def test_the_served_model_wins_over_the_requested_alias(quiet_node) -> None:
    quiet_node(
        AIMessage(
            content="ok", response_metadata={"model_name": "meta/llama-3.1-8b-2026"}
        )
    )
    updates = await orchestrator_llm_node(
        _turn_state("meta/llama-3.1-8b"), {"configurable": {}}
    )
    assert updates["_turn_model"] == "openrouter.ai/meta/llama-3.1-8b-2026"


async def test_a_stopped_session_records_no_model(quiet_node, monkeypatch) -> None:
    monkeypatch.setattr(orchestrator, "is_cancelled", lambda _sid: True)
    monkeypatch.setattr(orchestrator, "session_id_from_config", lambda _c: "s-model")
    updates = await orchestrator_llm_node(_turn_state("model-a"), {"configurable": {}})
    assert "_turn_model" not in updates


def test_a_local_only_configuration_makes_no_external_call(monkeypatch) -> None:
    """SM-5: with the one configured endpoint local, every platform LLM client
    — the orchestrator's and the small model's — points there, and the
    receipt will say ``local/…``."""
    monkeypatch.setattr(settings, "openrouter_base_url", "http://127.0.0.1:11434/v1")
    monkeypatch.setattr(settings, "orchestrator_model", "qwen2.5:3b")
    monkeypatch.setattr(orchestrator, "_small_llm", None)

    chat = _make_chat_llm()
    assert route(chat) == "local"
    assert turn_model(chat, None) == "local/qwen2.5:3b"
    assert str(orchestrator._get_small_llm().base_url).startswith(
        "http://127.0.0.1:11434"
    )
    # a per-turn override stays on the local endpoint — except a ``gemini-*``
    # override, which the orchestrator builds as the native client and the
    # receipt then names by that host, not "local"
    assert route(_make_chat_llm(model_override="llama3.2")) == "local"
    assert route(_make_chat_llm(model_override="gemini-2.5-flash")) == (
        "generativelanguage.googleapis.com"
    )
    monkeypatch.setattr(orchestrator, "_small_llm", None)


# ---------------------------------------------------------------------------
# Every step carries it
# ---------------------------------------------------------------------------


@pytest.fixture()
def recorded():
    seen: list[StepResult] = []

    class Recorder:
        async def on_step_complete(self, record: StepResult) -> None:
            seen.append(record)

    set_observer(Recorder())
    yield seen
    set_observer(NoOpObserver())


async def test_agent_and_system_steps_carry_the_turn_model(recorded) -> None:
    state = {**_state({}), "_turn_model": "openrouter.ai/model-a"}
    await _agent_step(state, "fine", call_id="c-agent")
    await attest_system_tool_step(
        call_id="c-sys",
        tool_name="get_datetime",
        args={},
        raw_result={"now": "x"},
        content='{"now": "x"}',
        success=True,
        latency_ms=1,
        state=state,
    )
    assert [s.metadata["model"] for s in recorded] == ["openrouter.ai/model-a"] * 2


async def test_a_turn_with_no_model_stamps_nothing(recorded) -> None:
    await _agent_step(_state({}), "fine", call_id="c-agent")
    (step,) = recorded
    assert "model" not in step.metadata


async def test_a_failed_dispatch_step_carries_the_turn_model_too(recorded) -> None:
    state = {**_state({}), "_turn_model": "openrouter.ai/model-a"}
    # the pipeline records the failed step, then re-raises for the node
    with pytest.raises(RuntimeError, match="down"):
        await _agent_step(
            state,
            "unused",
            call_id="c-down",
            dispatch=AsyncMock(side_effect=RuntimeError("down")),
        )
    (step,) = recorded
    assert step.success is False
    assert step.metadata["model"] == "openrouter.ai/model-a"


# ---------------------------------------------------------------------------
# The stream, the transcript and the export show it
# ---------------------------------------------------------------------------


def test_the_done_event_carries_the_model_and_is_otherwise_unchanged() -> None:
    assert _done_event("s1", "openrouter.ai/model-a") == {
        "type": "done",
        "session_id": "s1",
        "model": "openrouter.ai/model-a",
    }
    assert _done_event("s1", None) == {"type": "done", "session_id": "s1"}
    assert _done_event("s1", "") == {"type": "done", "session_id": "s1"}


async def test_the_values_sink_captures_the_turn_model(monkeypatch) -> None:
    class FakeGraph:
        async def astream(self, *_args: Any, **_kwargs: Any):
            yield ("values", {"messages": [], "_turn_model": "openrouter.ai/model-a"})

    monkeypatch.setattr(runner_mod, "_merge_graph_config", lambda c: c)
    sink: dict[str, Any] = {}
    async for _event in _yield_multistream_events(
        FakeGraph(), {}, {"configurable": {}}, values_messages_sink=sink
    ):
        pass
    assert sink["_turn_model"] == "openrouter.ai/model-a"


def test_the_assistant_row_records_the_model() -> None:
    with_model = AIMessage(
        content="hi", response_metadata={TURN_MODEL_KEY: "openrouter.ai/model-a"}
    )
    assert _assistant_model(with_model) == {"model": "openrouter.ai/model-a"}
    assert _assistant_model(AIMessage(content="hi")) is None
    assert _assistant_model(AIMessage(content="hi", response_metadata={})) is None
    # the row the store writes, not just the helper
    rows = messages_to_entry_dicts([HumanMessage(content="q"), with_model], 0)
    assert [r["tool_inputs"] for r in rows] == [
        None,
        {"model": "openrouter.ai/model-a"},
    ]


class _TwoTurnGraph:
    """A graph whose first turn ran an LLM (values carry ``_turn_model``) and
    whose second made no LLM call. Records what the runner hands it."""

    def __init__(self) -> None:
        self.inputs: list[dict[str, Any]] = []
        self.turn = 0
        self.messages = [HumanMessage(content="hi")]

    async def aget_state(self, _config: Any) -> Any:
        return SimpleNamespace(values={"messages": list(self.messages)}, tasks=[])

    def astream(self, state_update: Any, _config: Any, stream_mode: Any = None):
        self.inputs.append(dict(state_update))
        self.turn += 1
        answer = AIMessage(
            content=f"a{self.turn}",
            response_metadata={TURN_MODEL_KEY: "openrouter.ai/model-a"}
            if self.turn == 1
            else {},
        )
        self.messages = [*self.messages, answer]
        values: dict[str, Any] = {"messages": list(self.messages)}
        if self.turn == 1:
            values["_turn_model"] = "openrouter.ai/model-a"

        async def gen():
            yield ("values", values)

        return gen()


async def test_two_turns_of_one_session_each_show_and_record_their_own_model(
    monkeypatch,
) -> None:
    graph = _TwoTurnGraph()
    persisted: list[list[Any]] = []

    async def fake_persist(_session_id: str, msgs: list[Any], _prev: int) -> int:
        persisted.append(list(msgs))
        return len(msgs)

    monkeypatch.setattr(runner_mod, "_merge_graph_config", lambda c: c)
    monkeypatch.setattr(
        transcript_store, "get_session_meta", AsyncMock(return_value=(0, "u"))
    )
    monkeypatch.setattr(transcript_store, "persist_new_messages", fake_persist)
    set_observer(NoOpObserver())
    run = runner_mod.SessionRunner(graph)

    first = [e async for e in run.run_turn("s-two", "u", "hi")]
    second = [e async for e in run.run_turn("s-two", "u", "and again")]

    # the done event the runner emits, not the helper: turn 1 names its
    # model, turn 2 (no LLM call) names none — the model is never the last turn's
    assert first[-1] == {
        "type": "done",
        "session_id": "s-two",
        "model": "openrouter.ai/model-a",
    }
    assert second[-1] == {"type": "done", "session_id": "s-two"}
    assert [i["_turn_model"] for i in graph.inputs] == [None, None]  # reset per turn
    # the assistant row of turn 1 records the model; turn 2's records none
    rows = messages_to_entry_dicts(persisted[-1], 0)
    assistant = [r["tool_inputs"] for r in rows if r["role"] == "ASSISTANT"]
    assert assistant == [{"model": "openrouter.ai/model-a"}, None]


def test_the_export_reads_the_models_the_receipt_signed() -> None:
    envelope = {
        "verdicts": [
            {"check": "structural_verification", "result": "pass"},
            {"check": "model", "result": "pass", "detail": "openrouter.ai/model-a"},
            {"check": "model", "result": "pass", "detail": "openrouter.ai/model-a"},
            {"check": "model", "result": "pass", "detail": "local/qwen2.5:3b"},
            {"check": "model", "result": "pass"},
        ]
    }
    assert run_models(envelope) == ["openrouter.ai/model-a", "local/qwen2.5:3b"]
    assert run_models({"verdicts": []}) == []
    assert run_models({}) == []
    assert run_models(None) is None

    evidence = SettlementEvidence(run_id="r", envelope=envelope)
    # a hand-built envelope verifies as nothing: its verdicts are not read
    assert build_run_audit("s", [], evidence=evidence).models is None
    assert "models" not in build_run_audit("s", [], evidence=evidence).model_dump(
        exclude_none=True
    )
    # with the verifier absent (None), verdicts are read as stored
    assert run_models({"verdicts": [{"check": "model", "detail": "a/b"}]}, None) == [
        "a/b"
    ]
    with patch("superagent.api.audit.receipt_verifies", return_value=True):
        with_receipt = build_run_audit("s", [], evidence=evidence)
    assert with_receipt.models == ["openrouter.ai/model-a", "local/qwen2.5:3b"]
    assert build_run_audit("s", []).models is None
    assert "models" not in build_run_audit("s", []).model_dump(exclude_none=True)


# ---------------------------------------------------------------------------
# Sealed: one signed model verdict, and the gate is unmoved by it
# ---------------------------------------------------------------------------


async def test_the_receipt_signs_the_model_once_and_still_settles(
    sealed,  # noqa: F811 — the fixture imported above, by pytest's name lookup
) -> None:
    from superagent.nodes.execute_agent_calls import execute_agent_calls_node

    state = {**_state({}), "_turn_model": "openrouter.ai/model-a"}
    await execute_agent_calls_node(_system_call("get_datetime", state, "c-clock"), {})
    await _agent_step(state, "fine", call_id="c-agent")
    envelope, ledger = await sealed()

    models = [v for v in envelope["verdicts"] if v["check"] == "model"]
    assert models == [
        {"check": "model", "result": "pass", "detail": "openrouter.ai/model-a"}
    ]
    (row,) = ledger
    assert row["outcome"] == "settled"

    # the export reads it back from the receipt
    evidence = await load_settlement(SESSION, db=sealed.db)
    assert build_run_audit(SESSION, [], evidence=evidence).models == [
        "openrouter.ai/model-a"
    ]
