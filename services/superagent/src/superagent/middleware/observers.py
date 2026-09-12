"""ExecutionObserver — the open/closed seam in the execution pipeline.

This is the boundary between the open-source runtime and any future hosted /
closed data layer. The public package ships a ``NoOpObserver`` that does
nothing. A hosted deployment injects its own observer (e.g. a
``FulfillmentRecorder`` feeding a semantic judge / GNN) *server-side only* —
it is never part of the public package.

The contract is deliberately tiny: one coroutine, called once per agent
execution, immediately after the OutputNormalizer step. Observers MUST NOT
raise — a failing observer must never break a user-facing execution. The
pipeline guards the call, but observers should also fail closed internally.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StepResult:
    """Immutable record of a single agent execution.

    This is the unit the observer seam emits. It is intentionally
    transport-agnostic and contains no credentials or raw auth headers.
    """

    call_id: str
    agent_id: str
    capability_id: str
    protocol: str
    tool_name: str
    success: bool
    content: str
    user_id: str = ""
    session_id: str = ""
    latency_ms: int = 0
    base_fee: str = "0"
    total_cost_usd: str = "0"
    completed_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    metadata: dict[str, Any] = field(default_factory=dict)
    verdict: dict[str, Any] | None = None
    # Raw call arguments (post-InputGuard). Needed by the run-attestation
    # observer to compute the RFC 0003 args_hash; never carries credentials
    # (auth headers live on the handlers, not here). In-process only —
    # stripped from the Kafka fan-out payload (step_events.step_result_payload).
    args: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class ExecutionObserver(Protocol):
    """Hook invoked after each agent execution completes.

    The public repo ships :class:`NoOpObserver`. Hosted deployments inject a
    recorder server-side. Implementations must be non-blocking-friendly and
    must never raise out to the caller.

    Optional second hook: observers may also implement
    ``async def on_run_complete(self, session_id: str)``, dispatched once per
    completed run (a graph turn that ends without a pending HITL interrupt).
    Optional third hook: ``async def discard_run(self, session_id: str)``,
    dispatched when a run is abandoned (stream error / cancel) before its
    boundary, so buffered per-run state can be dropped. Both are deliberately
    NOT part of this Protocol — step-only observers must keep satisfying the
    structural isinstance check without them.
    """

    async def on_step_complete(self, record: StepResult) -> None: ...


class NoOpObserver:
    """Default observer — does nothing. Shipped in the open-source package."""

    async def on_step_complete(self, record: StepResult) -> None:  # noqa: D102
        return None

    async def on_run_complete(self, session_id: str) -> None:  # noqa: D102
        return None


class CompositeObserver:
    """Fan out to several observers, in order, as one installed observer.

    Boot-time composition seam: ``set_observer`` holds a single observer, so
    deployments enabling several observer-backed features (audit ledger, CDV
    scoring, run attestation) install one composite instead of silently
    last-wins overwriting. Order matters — e.g. the CDV observer must run
    before the run-attestation observer so ``metadata["cdv"]`` is populated
    when the attestation step is accumulated. Each child fails closed: one
    broken observer never starves the ones after it.
    """

    def __init__(self, observers: list[ExecutionObserver]) -> None:
        if not observers:
            raise ValueError("CompositeObserver requires at least one observer")
        self._observers = list(observers)

    @property
    def observers(self) -> tuple[ExecutionObserver, ...]:
        return tuple(self._observers)

    async def on_step_complete(self, record: StepResult) -> None:  # noqa: D102
        for observer in self._observers:
            try:
                await observer.on_step_complete(record)
            except Exception:
                logger.exception(
                    "CompositeObserver child %s raised for call_id=%s; continuing",
                    type(observer).__name__,
                    record.call_id,
                )

    async def on_run_complete(self, session_id: str) -> None:  # noqa: D102
        for observer in self._observers:
            hook = getattr(observer, "on_run_complete", None)
            if hook is None:
                continue
            try:
                await hook(session_id)
            except Exception:
                logger.exception(
                    "CompositeObserver child %s raised for session=%s; continuing",
                    type(observer).__name__,
                    session_id,
                )

    async def discard_run(self, session_id: str) -> None:  # noqa: D102
        for observer in self._observers:
            hook = getattr(observer, "discard_run", None)
            if hook is None:
                continue
            try:
                await hook(session_id)
            except Exception:
                logger.exception(
                    "CompositeObserver child %s raised for session=%s; continuing",
                    type(observer).__name__,
                    session_id,
                )


# Module-level singleton. Hosted deployments call ``set_observer`` at startup
# to swap in their recorder; the OSS package leaves the no-op in place.
_observer: ExecutionObserver = NoOpObserver()


def set_observer(observer: ExecutionObserver) -> None:
    """Install the process-wide execution observer (server-side injection point)."""
    global _observer
    _observer = observer
    logger.info("ExecutionObserver installed: %s", type(observer).__name__)


def get_observer() -> ExecutionObserver:
    """Return the currently installed observer (defaults to NoOpObserver)."""
    return _observer


def peek_published_run_id(session_id: str) -> str | None:
    """RFC 0003 ``run_id`` sealed for this session, if the producer published one.

    Walks a ``CompositeObserver`` without popping ``last_sealed`` — that
    binding is the settlement gate's one-shot handoff. Stock OSS (no
    attestation observer) returns ``None``.
    """
    if not session_id:
        return None
    observer = _observer
    children = getattr(observer, "observers", None)
    candidates = children if children else (observer,)
    for child in candidates:
        published = getattr(child, "published", None)
        if isinstance(published, dict):
            run_id = published.get(session_id)
            if isinstance(run_id, str) and run_id:
                return run_id
    return None


async def emit_step_complete(record: StepResult) -> None:
    """Dispatch a completed step to the installed observer, swallowing errors.

    A broken observer must never surface to the user-facing execution path.
    """
    try:
        await _observer.on_step_complete(record)
    except Exception:  # pragma: no cover - defensive; observers must fail closed
        logger.exception(
            "ExecutionObserver.on_step_complete raised for call_id=%s; ignoring",
            record.call_id,
        )
    # D1: optional Kafka fan-out for validator nodes (never raises).
    try:
        from .step_events import schedule_fan_out

        schedule_fan_out(record)
    except Exception:  # pragma: no cover
        logger.exception(
            "step_complete fan-out failed for call_id=%s; ignoring", record.call_id
        )


async def emit_run_complete(session_id: str) -> None:
    """Dispatch run completion to the installed observer, swallowing errors.

    Called by the SessionRunner when a graph turn ends without a pending
    interrupt — that is the run boundary for the run-attestation observer
    (RFC 0003). Observers without an ``on_run_complete`` hook are skipped.
    """
    hook = getattr(_observer, "on_run_complete", None)
    if hook is None:
        return
    try:
        await hook(session_id)
    except Exception:  # pragma: no cover - defensive; observers must fail closed
        logger.exception(
            "ExecutionObserver.on_run_complete raised for session=%s; ignoring",
            session_id,
        )


async def emit_run_discarded(session_id: str) -> None:
    """Dispatch run abandonment to the installed observer, swallowing errors.

    Called by the SessionRunner when a turn ends WITHOUT reaching its run
    boundary (graph-stream error or kill-switch cancel). Observers buffering
    per-session state (the RFC 0003 run-attestation observer accumulates
    ``StepResult``s keyed by session) must drop it here — otherwise it grows
    unboundedly and leaks into the session's next sealed envelope. Observers
    without a ``discard_run`` hook are skipped.
    """
    hook = getattr(_observer, "discard_run", None)
    if hook is None:
        return
    try:
        await hook(session_id)
    except Exception:  # pragma: no cover - defensive; observers must fail closed
        logger.exception(
            "ExecutionObserver.discard_run raised for session=%s; ignoring",
            session_id,
        )
