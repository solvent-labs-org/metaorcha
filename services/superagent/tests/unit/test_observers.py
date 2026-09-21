"""Tests for the ExecutionObserver open/closed seam."""

from __future__ import annotations

import os
from dataclasses import FrozenInstanceError

import pytest
from superagent.middleware.observers import (
    CompositeObserver,
    ExecutionObserver,
    NoOpObserver,
    StepResult,
    emit_step_complete,
    get_observer,
    peek_published_run_id,
    set_observer,
)


def _record(**overrides) -> StepResult:
    base = {
        "call_id": "call-1",
        "agent_id": "did:orcha:agent:web-scraper",
        "capability_id": "scrape",
        "protocol": "A2A",
        "tool_name": "web-scraper.scrape",
        "success": True,
        "content": "ok",
    }
    base.update(overrides)
    return StepResult(**base)


@pytest.fixture(autouse=True)
def _restore_default_observer():
    """Each test starts and ends with the default NoOpObserver installed."""
    set_observer(NoOpObserver())
    yield
    set_observer(NoOpObserver())


def test_default_observer_is_noop():
    assert isinstance(get_observer(), NoOpObserver)


@pytest.mark.asyncio
async def test_noop_observer_does_nothing():
    # Should complete without error and return None.
    assert await get_observer().on_step_complete(_record()) is None


@pytest.mark.asyncio
async def test_injected_observer_receives_record():
    seen: list[StepResult] = []

    class Recorder:
        async def on_step_complete(self, record: StepResult) -> None:
            seen.append(record)

    recorder = Recorder()
    assert isinstance(recorder, ExecutionObserver)  # structural Protocol check
    set_observer(recorder)

    await emit_step_complete(_record(call_id="call-42"))

    assert len(seen) == 1
    assert seen[0].call_id == "call-42"
    assert seen[0].agent_id == "did:orcha:agent:web-scraper"


@pytest.mark.asyncio
async def test_broken_observer_never_raises_to_caller():
    class Broken:
        async def on_step_complete(self, record: StepResult) -> None:
            raise RuntimeError("observer blew up")

    set_observer(Broken())

    # emit_step_complete must swallow the error — execution must not break.
    await emit_step_complete(_record())


def test_step_result_is_immutable():
    rec = _record()
    with pytest.raises(FrozenInstanceError):
        rec.success = False  # type: ignore[misc]


class _Recorder:
    """Records step/run events; pass run_hook=False to omit on_run_complete."""

    def __init__(self, seen: list, run_hook: bool = True):
        self._seen = seen
        if run_hook:
            self.on_run_complete = self._on_run_complete  # type: ignore[method-assign]

    async def on_step_complete(self, record: StepResult) -> None:
        self._seen.append(("step", record.call_id))

    async def _on_run_complete(self, session_id: str) -> None:
        self._seen.append(("run", session_id))


@pytest.mark.asyncio
async def test_composite_observer_fans_out_to_all_children():
    seen_a: list = []
    seen_b: list = []
    set_observer(
        CompositeObserver([_Recorder(seen_a), _Recorder(seen_b)])  # type: ignore[list-item]
    )

    await emit_step_complete(_record(call_id="call-99"))

    # Both children receive the same event (no last-wins overwrite).
    assert seen_a == [("step", "call-99")]
    assert seen_b == [("step", "call-99")]


@pytest.mark.asyncio
async def test_composite_observer_preserves_child_order():
    order: list[str] = []

    class First:
        async def on_step_complete(self, record: StepResult) -> None:
            record.metadata["cdv"] = {"score": 0.9}
            order.append("first")

    class Second:
        async def on_step_complete(self, record: StepResult) -> None:
            # Runs after First: sees what it wrote (CDV → attestation ordering).
            order.append(f"second:{record.metadata.get('cdv', {}).get('score')}")

    set_observer(CompositeObserver([First(), Second()]))  # type: ignore[list-item]

    await emit_step_complete(_record())

    assert order == ["first", "second:0.9"]


@pytest.mark.asyncio
async def test_composite_observer_broken_child_never_starves_others():
    seen: list = []

    class Broken:
        async def on_step_complete(self, record: StepResult) -> None:
            raise RuntimeError("child blew up")

    set_observer(CompositeObserver([Broken(), _Recorder(seen)]))  # type: ignore[list-item]

    await emit_step_complete(_record(call_id="call-7"))

    assert seen == [("step", "call-7")]


@pytest.mark.asyncio
async def test_composite_observer_run_complete_skips_children_without_hook():
    seen_a: list = []
    seen_b: list = []
    composite = CompositeObserver(  # type: ignore[list-item]
        [_Recorder(seen_a, run_hook=False), _Recorder(seen_b)]
    )
    set_observer(composite)

    from superagent.middleware.observers import emit_run_complete

    await emit_run_complete("session-1")

    assert seen_a == []
    assert seen_b == [("run", "session-1")]


def test_composite_observer_requires_at_least_one_child():
    with pytest.raises(ValueError):
        CompositeObserver([])


def test_peek_published_run_id_none_on_stock_observer():
    assert peek_published_run_id("sess-1") is None
    assert peek_published_run_id("") is None


def test_peek_published_run_id_walks_composite_without_popping():
    class _Publisher:
        def __init__(self) -> None:
            self.published = {"sess-1": "sess-1-abc"}
            self.last_sealed = {"sess-1": "sess-1-abc"}

        async def on_step_complete(self, record: StepResult) -> None:
            return None

    set_observer(CompositeObserver([_Publisher()]))  # type: ignore[list-item]

    assert peek_published_run_id("sess-1") == "sess-1-abc"
    publisher = get_observer().observers[0]
    assert publisher.last_sealed["sess-1"] == "sess-1-abc"


@pytest.mark.asyncio
async def test_system_tool_step_lands_on_the_observer_seam():
    """FR-7: a system-tool call is a recorded step, not a silent registry hit."""
    import json

    from superagent.middleware.system_steps import (
        SYSTEM_TOOL_AGENT_DID,
        SYSTEM_TOOL_PROTOCOL,
        attest_system_tool_step,
    )

    seen: list[StepResult] = []

    class Recorder:
        async def on_step_complete(self, record: StepResult) -> None:
            seen.append(record)

    set_observer(Recorder())
    output = json.dumps({"exit_code": 1, "stdout": "failed"})
    await attest_system_tool_step(
        call_id="call-suite",
        tool_name="run_tests",
        args={"cwd": "."},
        content=output,
        success=True,
        latency_ms=12,
        state={
            "user_id": "u1",
            "session_id": "s1",
            "_declared_criteria": {"exit_zero": True},
        },
    )

    assert len(seen) == 1
    rec = seen[0]
    assert rec.call_id == "call-suite"
    assert rec.agent_id == SYSTEM_TOOL_AGENT_DID
    assert rec.protocol == SYSTEM_TOOL_PROTOCOL
    assert rec.tool_name == "run_tests"
    assert rec.args == {"cwd": "."}
    assert rec.metadata["declared_acceptance"] == {
        "result": "fail",
        "detail": "nonzero exit: 1",
        "criteria": {"exit_zero": "fail"},
    }


@pytest.mark.asyncio
async def test_declared_criteria_read_the_raw_output_not_the_280_char_card(
    monkeypatch: pytest.MonkeyPatch,
):
    """Regression from the 2026-09-21 local bed: the normalizer caps plain
    text at 280 chars for the markdown_card; a test-runner JSON longer than
    that was cut mid-string, so ``exit_code: 1`` read as "no exit code"."""
    import json
    from unittest.mock import AsyncMock, patch

    # Importing the pipeline builds superagent.config.Settings; the hosted
    # observer-seam job runs without these, so supply placeholders here.
    for key, value in {
        "OPENROUTER_API_KEY": "test",
        "REDIS_URL": "redis://localhost:6379/9",
        "DATABASE_URL": "postgresql://u:p@localhost:5432/x",
        "VAULT_KEY": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        "PND_SERVICE_URL": "http://localhost:8001",
    }.items():
        monkeypatch.setenv(key, os.environ.get(key, value))

    from superagent.middleware.pipeline import ExecutionMiddleware

    class FakePreFlightManager:
        def __init__(self, _vault):
            pass

        async def run(self, **kwargs):
            return {"manifest": {"transport": {}}, "headers": {}, "resolved_env": None}

    seen: list[StepResult] = []

    class Recorder:
        async def on_step_complete(self, record: StepResult) -> None:
            seen.append(record)

    set_observer(Recorder())
    raw = json.dumps(
        {
            "command": "pytest -q",
            "duration_ms": 392,
            "exit_code": 1,
            "repo": "/app/fixtures/failing",
            "stdout_tail": "F"
            + " " * 60
            + "[100%]\n"
            + "=" * 35
            + " FAILURES "
            + "=" * 35
            + "\n"
            + "_" * 26
            + " test_regression "
            + "_" * 26
            + "\n\n    def test_regression():\n>       assert 1 == 2\nE       assert 1 == 2\n\n1 failed in 0.02s\n",
        }
    )
    assert len(raw) > 280
    state = {
        "user_id": "u1",
        "session_id": "s1",
        "_declared_criteria": {"exit_zero": True},
    }
    with (
        patch("superagent.middleware.pipeline.PreFlightManager", FakePreFlightManager),
        patch(
            "superagent.middleware.pipeline.InputGuard.validate",
            side_effect=lambda args, _schema: args,
        ),
        patch.object(
            ExecutionMiddleware, "_get_capability_schema", AsyncMock(return_value=None)
        ),
        patch.object(ExecutionMiddleware, "_dispatch", AsyncMock(return_value=raw)),
        patch("superagent.vault.client.VaultClient"),
    ):
        result = await ExecutionMiddleware(state=state).execute(
            agent_id="did:orcha:agent:test-runner",
            capability_id="run_tests",
            protocol="A2A",
            tool_name="delegate__did_orcha_agent_test-runner",
            args={"task": "run pytest -q"},
            call_id="call_long",
            config={"configurable": {}},
        )

    # the card content is capped (display concern) …
    assert len(result["content"]) <= 280
    # … but the criterion read the agent's bytes
    assert len(seen) == 1
    assert seen[0].metadata["declared_acceptance"] == {
        "result": "fail",
        "detail": "nonzero exit: 1",
        "criteria": {"exit_zero": "fail"},
    }
