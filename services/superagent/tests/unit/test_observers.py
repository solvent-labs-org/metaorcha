"""Tests for the ExecutionObserver open/closed seam."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest
from superagent.middleware.observers import (
    CompositeObserver,
    ExecutionObserver,
    NoOpObserver,
    StepResult,
    emit_step_complete,
    get_observer,
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
