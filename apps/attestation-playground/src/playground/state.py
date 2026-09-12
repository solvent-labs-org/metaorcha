"""In-memory run state + event fan-out. Localhost single-user demo — no persistence by design.

Every emitted event is appended to the run's log and fanned out to current
subscribers. `subscribe` returns the buffered log plus a fresh queue, so a
late subscriber (mid-run browser refresh) replays history then continues
live — each event exactly once, in order. Both methods are synchronous with
no awaits, so log-copy and queue-registration cannot interleave with `emit`.
"""

import asyncio
from typing import Any


class RunStore:
    def __init__(self) -> None:
        self._runs: dict[str, dict[str, Any]] = {}
        self._logs: dict[str, list[dict[str, Any]]] = {}
        self._subscribers: dict[str, list[asyncio.Queue]] = {}

    def create(self, run_id: str, mode: str) -> None:
        self._runs[run_id] = {
            "run_id": run_id,
            "mode": mode,
            "status": "running",
            "envelope": None,
            "error": None,
        }
        self._logs[run_id] = []
        self._subscribers[run_id] = []

    def get(self, run_id: str) -> dict[str, Any] | None:
        return self._runs.get(run_id)

    def emit(self, run_id: str, event: dict[str, Any]) -> None:
        self._logs[run_id].append(event)
        for queue in self._subscribers[run_id]:
            queue.put_nowait(event)

    def subscribe(self, run_id: str) -> tuple[list[dict[str, Any]], asyncio.Queue]:
        queue: asyncio.Queue = asyncio.Queue()
        self._subscribers[run_id].append(queue)
        return list(self._logs[run_id]), queue

    def unsubscribe(self, run_id: str, queue: asyncio.Queue) -> None:
        subscribers = self._subscribers.get(run_id)
        if subscribers is not None and queue in subscribers:
            subscribers.remove(queue)

    def sink(self, run_id: str) -> Any:
        """Async-`put`-compatible adapter so StepEventRouter emits via the store."""
        store = self

        class _Sink:
            async def put(self, event: dict[str, Any]) -> None:
                store.emit(run_id, event)

        return _Sink()

    def set_envelope(self, run_id: str, envelope: dict[str, Any]) -> None:
        self._runs[run_id]["envelope"] = envelope
        self._runs[run_id]["status"] = "done"

    def set_failed(self, run_id: str, error: str) -> None:
        self._runs[run_id]["status"] = "failed"
        self._runs[run_id]["error"] = error


run_store = RunStore()
