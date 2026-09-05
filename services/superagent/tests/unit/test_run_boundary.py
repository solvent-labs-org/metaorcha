"""Run-boundary dispatch tests — SessionRunner ↔ ExecutionObserver seam.

Covers the final-review fixes:
- a failed checkpoint read (aget_state raising) must NOT be treated as "no
  pending interrupt" — emit_run_complete is skipped (fail closed);
- graph-stream error paths dispatch discard_run through the seam so buffered
  per-session observer state is dropped instead of leaking into the next turn.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import HumanMessage
from superagent.graph.runner import SessionRunner
from superagent.middleware.observers import (
    NoOpObserver,
    set_observer,
)


class _FakeGraph:
    """Minimal compiled-graph stand-in for run-boundary tests.

    ``aget_state`` returns ``snapshot`` on the first call (run_turn's state
    probe) and then follows ``then_raise`` / ``then_snapshot`` for the
    post-stream pending-interrupt read.
    """

    def __init__(
        self,
        *,
        snapshot: Any,
        then_raise: Exception | None = None,
        then_snapshot: Any = None,
        stream_error: Exception | None = None,
    ) -> None:
        self._snapshot = snapshot
        self._then_raise = then_raise
        self._then_snapshot = then_snapshot
        self._stream_error = stream_error
        self._state_calls = 0

    async def aget_state(self, config: dict[str, Any]) -> Any:
        self._state_calls += 1
        # run_turn probes state before streaming; resume_from_interrupt does
        # not, so with snapshot=None the FIRST call is the pending read.
        if self._state_calls == 1 and self._snapshot is not None:
            return self._snapshot
        if self._then_raise is not None:
            raise self._then_raise
        return self._then_snapshot

    def astream(self, *args: Any, **kwargs: Any) -> Any:
        stream_error = self._stream_error

        async def _gen():
            if stream_error is not None:
                raise stream_error
            return
            yield  # pragma: no cover - makes this an async generator

        return _gen()


class _Recorder:
    """Observer recording run-boundary and discard dispatches."""

    def __init__(self) -> None:
        self.completed: list[str] = []
        self.discarded: list[str] = []

    async def on_step_complete(self, record: Any) -> None:
        return None

    async def on_run_complete(self, session_id: str) -> None:
        self.completed.append(session_id)

    async def discard_run(self, session_id: str) -> None:
        self.discarded.append(session_id)


@pytest.fixture(autouse=True)
def _restore_default_observer():
    set_observer(NoOpObserver())
    yield
    set_observer(NoOpObserver())


@pytest.fixture(autouse=True)
def _no_transcript_persist(monkeypatch: pytest.MonkeyPatch):
    async def _noop(self: Any, *args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(SessionRunner, "_persist_transcript", _noop)


def _snapshot_with_messages() -> Any:
    return SimpleNamespace(values={"messages": [HumanMessage(content="hi")]}, tasks=[])


def _snapshot_with_interrupt() -> Any:
    payload = {
        "type": "interrupt",
        "interrupt_type": "payment",
        "interrupt_id": "int-1",
        "agent_id": "did:orcha:agent:x",
        "session_id": "sess-1",
        "message": "approve?",
        "metadata": {},
        "resumable": True,
    }
    task = SimpleNamespace(interrupts=[SimpleNamespace(value=payload)])
    return SimpleNamespace(values={"messages": []}, tasks=[task])


async def _collect(agen: Any) -> list[dict[str, Any]]:
    return [event async for event in agen]


@pytest.mark.asyncio
async def test_run_turn_checkpoint_read_failure_skips_run_complete() -> None:
    """aget_state raising on the post-stream read must not seal the run."""
    recorder = _Recorder()
    set_observer(recorder)
    graph = _FakeGraph(
        snapshot=_snapshot_with_messages(),
        then_raise=RuntimeError("checkpointer connection lost"),
    )
    runner = SessionRunner(graph)

    events = await _collect(
        runner.run_turn("sess-1", "user-1", "hello", lead_gen_options=None)
    )

    assert recorder.completed == []  # no fail-open run-complete dispatch
    assert recorder.discarded == []  # buffered steps kept — run may resume
    assert events[-1] == {"type": "done", "session_id": "sess-1"}


@pytest.mark.asyncio
async def test_run_turn_pending_interrupt_skips_run_complete() -> None:
    """A real pending interrupt still suppresses run-complete (regression)."""
    recorder = _Recorder()
    set_observer(recorder)
    graph = _FakeGraph(
        snapshot=_snapshot_with_messages(),
        then_snapshot=_snapshot_with_interrupt(),
    )
    runner = SessionRunner(graph)

    events = await _collect(runner.run_turn("sess-1", "user-1", "hello"))

    assert recorder.completed == []
    assert any(e.get("type") == "interrupt" for e in events)
    assert events[-1] == {"type": "done", "session_id": "sess-1"}


@pytest.mark.asyncio
async def test_run_turn_clean_completion_dispatches_run_complete() -> None:
    """No pending interrupt + healthy read → run-complete fires (regression)."""
    recorder = _Recorder()
    set_observer(recorder)
    graph = _FakeGraph(
        snapshot=_snapshot_with_messages(),
        then_snapshot=SimpleNamespace(values={"messages": []}, tasks=[]),
    )
    runner = SessionRunner(graph)

    await _collect(runner.run_turn("sess-1", "user-1", "hello"))

    assert recorder.completed == ["sess-1"]
    assert recorder.discarded == []


@pytest.mark.asyncio
async def test_run_turn_stream_error_discards_run_via_seam() -> None:
    """Graph-stream error path drops buffered observer state, never seals."""
    recorder = _Recorder()
    set_observer(recorder)
    graph = _FakeGraph(
        snapshot=_snapshot_with_messages(),
        stream_error=RuntimeError("graph blew up"),
    )
    runner = SessionRunner(graph)

    events = await _collect(runner.run_turn("sess-1", "user-1", "hello"))

    assert recorder.discarded == ["sess-1"]
    assert recorder.completed == []
    assert events[-1]["type"] == "error"


@pytest.mark.asyncio
async def test_resume_checkpoint_read_failure_skips_run_complete() -> None:
    """Same fail-closed rule on the HITL resume path."""
    recorder = _Recorder()
    set_observer(recorder)
    graph = _FakeGraph(
        snapshot=None,  # resume never probes state before streaming
        then_raise=RuntimeError("checkpointer connection lost"),
    )
    runner = SessionRunner(graph)

    events = await _collect(runner.resume_from_interrupt("sess-1", {"ok": True}))

    assert recorder.completed == []
    assert recorder.discarded == []
    assert events[-1] == {"type": "done", "session_id": "sess-1"}


@pytest.mark.asyncio
async def test_resume_stream_error_discards_run_via_seam() -> None:
    recorder = _Recorder()
    set_observer(recorder)
    graph = _FakeGraph(snapshot=None, stream_error=RuntimeError("graph blew up"))
    runner = SessionRunner(graph)

    events = await _collect(runner.resume_from_interrupt("sess-1", {"ok": True}))

    assert recorder.discarded == ["sess-1"]
    assert recorder.completed == []
    assert events[-1]["type"] == "error"
