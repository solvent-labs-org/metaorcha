"""RunAttestationObserver — ExecutionObserver emitting RFC 0003 run envelopes.

Installed process-wide by SuperAgent at boot when ``RUN_ATTESTATION_ENABLED``
is true. Accumulates each run's ``StepResult``s via ``on_step_complete`` and,
at run completion (``on_run_complete``, dispatched through the observer seam
when the run's graph turn ends without a pending interrupt), builds, signs,
and persists an ``orcha.run-attestation/v1`` envelope.

Fail-closed throughout: any error is logged, never raised into the pipeline.
If the DB is unavailable the signed envelope stays retrievable in memory
(``observer.envelopes``) and the run continues unaffected.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .run_envelope import (
    build_run_envelope,
    cdv_score_to_bp,
    persist_run_attestation,
    sign_run_envelope,
)

logger = logging.getLogger(__name__)

DEFAULT_SIGNER_DID = "did:orcha:system:validator"
POLICY_VERSION_ENV = "RUN_ATTESTATION_POLICY_VERSION"
DEFAULT_POLICY_VERSION = "run-attestation/1.0"
# Persist ceiling on the SSE path (same idiom as anchor.py _TIMEOUT_SECONDS):
# a hung DB must not stall the run's `done` event.
PERSIST_TIMEOUT_ENV = "RUN_ATTESTATION_PERSIST_TIMEOUT_SECONDS"
DEFAULT_PERSIST_TIMEOUT_SECONDS = 10.0
# Bound on the in-memory envelope fallback — DB-down envelopes are retrievable
# here, but the map must not grow without limit.
MAX_ENVELOPES_ENV = "RUN_ATTESTATION_MAX_ENVELOPES"
DEFAULT_MAX_ENVELOPES = 1000


def _rfc3339_z(value: datetime) -> str:
    """RFC 3339 UTC with Z designator (envelope timestamp format)."""
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ts(raw: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(raw))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed
    except (TypeError, ValueError):
        return datetime.now(UTC)


@dataclass
class _AccumulatedStep:
    call_id: str
    tool: str
    args: dict[str, Any]
    output: Any
    success: bool
    latency_ms: int
    cdv_bp: int | None
    agent_id: str
    verdict: dict[str, Any] | None
    completed_at: datetime


class RunAttestationObserver:
    """ExecutionObserver that attests each completed run (RFC 0003).

    A "run" is one completed graph turn of a session: steps accumulate keyed
    by ``session_id`` and are sealed when the seam dispatches
    ``on_run_complete(session_id)`` (end of ``SessionRunner.run_turn`` /
    ``resume_from_interrupt`` with no pending HITL interrupt). The buffer is
    cleared at seal time, so the next turn of the same session starts a fresh
    run with a fresh ``run_id``. Runs abandoned before their boundary (stream
    error / kill-switch cancel) are dropped via ``discard_run(session_id)`` so
    their steps neither accumulate nor leak into the next turn's envelope.
    The in-memory envelope fallback is bounded (``max_envelopes``, oldest
    evicted with a warning).
    """

    def __init__(
        self,
        *,
        signer_did: str = DEFAULT_SIGNER_DID,
        policy_version: str | None = None,
        charter_hash: str | None = None,
        db: Any = None,
        persist_timeout: float | None = None,
        max_envelopes: int | None = None,
    ) -> None:
        self.signer_did = signer_did
        self.policy_version = policy_version or os.environ.get(
            POLICY_VERSION_ENV, DEFAULT_POLICY_VERSION
        )
        self.charter_hash = charter_hash
        self._db = db
        self._persist_timeout = (
            persist_timeout
            if persist_timeout is not None
            else float(
                os.environ.get(PERSIST_TIMEOUT_ENV, DEFAULT_PERSIST_TIMEOUT_SECONDS)
            )
        )
        self._max_envelopes = (
            max_envelopes
            if max_envelopes is not None
            else int(os.environ.get(MAX_ENVELOPES_ENV, DEFAULT_MAX_ENVELOPES))
        )
        self._steps: dict[str, list[_AccumulatedStep]] = {}
        # run_id → signed envelope; the in-memory fallback when the DB is down.
        # Bounded by _max_envelopes (oldest evicted with a warning).
        self.envelopes: dict[str, dict[str, Any]] = {}
        # session_id → just-sealed run_id: the explicit handoff the settlement
        # gate observer pops to bind its deferred settle to THIS run — never a
        # prefix heuristic or newest-row query (2.1 review). Bounded like
        # envelopes; entries are popped by the reader, so the bound only
        # matters when no gate observer is registered.
        self.last_sealed: dict[str, str] = {}

    async def on_step_complete(self, record: Any) -> None:
        """Observer contract — accumulate one completed step for its run."""
        try:
            cdv = (
                record.metadata.get("cdv")
                if isinstance(record.metadata, dict)
                else None
            )
            cdv_bp = (
                cdv_score_to_bp(cdv["score"])
                if isinstance(cdv, dict) and cdv.get("score") is not None
                else None
            )
            session_id = record.session_id or "default"
            self._steps.setdefault(session_id, []).append(
                _AccumulatedStep(
                    call_id=record.call_id,
                    tool=record.tool_name,
                    args=dict(getattr(record, "args", None) or {}),
                    output=record.content,
                    success=bool(record.success),
                    latency_ms=int(record.latency_ms),
                    cdv_bp=cdv_bp,
                    agent_id=record.agent_id,
                    verdict=record.verdict
                    if isinstance(record.verdict, dict)
                    else None,
                    completed_at=_parse_ts(record.completed_at),
                )
            )
        except Exception:
            logger.exception(
                "RunAttestationObserver: failed to accumulate call_id=%s",
                getattr(record, "call_id", "?"),
            )

    def _verdicts(self, steps: list[_AccumulatedStep]) -> list[dict[str, Any]]:
        verdicts: list[dict[str, Any]] = []
        for step in steps:
            verdict = step.verdict
            if not verdict or "verified" not in verdict:
                continue
            entry: dict[str, Any] = {
                "check": "structural_verification",
                "result": "pass" if verdict["verified"] else "fail",
            }
            reason = verdict.get("reason")
            if reason:
                entry["detail"] = f"{step.call_id}: {reason}"
            verdicts.append(entry)
        return verdicts

    async def discard_run(self, session_id: str) -> None:
        """Drop buffered steps for an abandoned run (error/cancel before seal).

        Dispatched through the observer seam from the runner's error/cancel
        return paths. Without it, steps for a run that never reaches its
        boundary would accumulate forever and leak into the session's next
        sealed envelope (cross-turn contamination).
        """
        dropped = self._steps.pop(session_id, None)
        if dropped:
            logger.info(
                "RunAttestationObserver: discarded %d buffered step(s) for "
                "abandoned run %s",
                len(dropped),
                session_id,
            )

    def _store_envelope(self, signed: dict[str, Any]) -> None:
        """Retain a signed envelope in memory, evicting oldest past the bound."""
        self.envelopes[signed["run_id"]] = signed
        while len(self.envelopes) > self._max_envelopes:
            evicted_id = next(iter(self.envelopes))
            del self.envelopes[evicted_id]
            logger.warning(
                "RunAttestationObserver: in-memory envelope bound (%d) reached — "
                "evicted oldest envelope %s",
                self._max_envelopes,
                evicted_id,
            )

    def _record_sealed(self, session_id: str, run_id: str) -> None:
        """Record the session's just-sealed run_id, evicting past the bound."""
        self.last_sealed[session_id] = run_id
        while len(self.last_sealed) > self._max_envelopes:
            evicted_session = next(iter(self.last_sealed))
            del self.last_sealed[evicted_session]
            logger.warning(
                "RunAttestationObserver: last_sealed bound (%d) reached — "
                "evicted binding for session %s",
                self._max_envelopes,
                evicted_session,
            )

    async def on_run_complete(self, session_id: str) -> None:
        """Seal the run: build + sign + persist the attestation envelope.

        Never raises — a broken attestation path must not affect the run.
        """
        steps = self._steps.pop(session_id, None)
        if not steps:
            logger.debug(
                "RunAttestationObserver: run %s completed with no steps; skipping",
                session_id,
            )
            return
        try:
            first, last = steps[0], steps[-1]
            started = first.completed_at - timedelta(milliseconds=first.latency_ms)
            envelope = build_run_envelope(
                run_id=f"{session_id}-{uuid.uuid4().hex[:12]}",
                agent_dids=list(dict.fromkeys(s.agent_id for s in steps)),
                charter_hash=self.charter_hash,
                policy_version=self.policy_version,
                steps=[
                    {
                        "call_id": s.call_id,
                        "tool": s.tool,
                        "args": s.args,
                        "output": s.output,
                        "success": s.success,
                        "latency_ms": s.latency_ms,
                        **({"cdv_bp": s.cdv_bp} if s.cdv_bp is not None else {}),
                    }
                    for s in steps
                ],
                verdicts=self._verdicts(steps),
                started_at=_rfc3339_z(started),
                finished_at=_rfc3339_z(last.completed_at),
                signer_did=self.signer_did,
            )
            signed = sign_run_envelope(envelope)
            self._store_envelope(signed)
            self._record_sealed(session_id, signed["run_id"])
            # Bounded wait: a hung DB must not stall the SSE `done` event.
            # Timeout keeps the envelope in memory and never raises.
            try:
                persisted = await asyncio.wait_for(
                    persist_run_attestation(session_id, signed, db=self._db),
                    timeout=self._persist_timeout,
                )
            except TimeoutError:
                logger.warning(
                    "RunAttestationObserver: persist timed out after %.1fs for "
                    "run %s — envelope %s kept in memory",
                    self._persist_timeout,
                    session_id,
                    signed["run_id"],
                )
                persisted = None
            if persisted is None:
                logger.warning(
                    "RunAttestationObserver: run %s attestation %s held in memory "
                    "(DB unavailable)",
                    session_id,
                    signed["run_id"],
                )
            else:
                logger.info(
                    "RunAttestationObserver: run %s attested as %s (steps_root=%s)",
                    session_id,
                    signed["run_id"],
                    signed["steps_root"],
                )
        except Exception:
            logger.exception(
                "RunAttestationObserver: attestation failed for run %s; run unaffected",
                session_id,
            )
