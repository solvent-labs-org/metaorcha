"""CDV verification integration (Slice 1, flag-gated).

- CDVObserver: per-step Channel-A (deterministic) scoring via cdv.step_scorer,
  recorded into StepResult.metadata["cdv"] and persisted through
  SQLiteBackedPriors into a per-run SQLite file ({cdv_store_dir}/{session_id}.db).
- AdaptiveStopper backstop: per-session Bayesian stop guard checked in
  route_after_orchestrator; when its guards trip (plateau / low-ROI / budget),
  the loop routes to respond instead of burning more steps.

Channel B (semantic LLM critic) is deliberately deferred — it needs an MCP
ctx.sample() shim over OpenRouter. v1 runs Channel-A only
(source="channel_a_only", per cdv's own conservative degradation).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..config import settings
from ..middleware.observers import StepResult

logger = logging.getLogger(__name__)

_QUALITY_CRITERIA = ["non-empty", "relevant to the goal", "coherent"]

_stoppers: dict[str, Any] = {}
_priors_cache: dict[str, Any] = {}


def _store_path(session_id: str) -> Path:
    safe = (
        "".join(c if c.isalnum() or c in "-_" else "_" for c in session_id) or "default"
    )
    return Path(settings.cdv_store_dir) / f"{safe}.db"


def _get_priors(session_id: str) -> Any:
    # Keyed by resolved store path (not bare session_id) so a per-run store is
    # never reused after cdv_store_dir changes (and tests stay isolated).
    key = str(_store_path(session_id))
    if key not in _priors_cache:
        from cdv.store import LoopStore, SQLiteBackedPriors

        path = Path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        _priors_cache[key] = SQLiteBackedPriors(LoopStore(path))
    return _priors_cache[key]


class CDVObserver:
    """ExecutionObserver that Channel-A-scores every completed external step."""

    def __init__(self) -> None:
        from cdv.step_scorer import build_step_evaluator

        self._evaluator = build_step_evaluator("composite", _QUALITY_CRITERIA)

    async def on_step_complete(self, record: StepResult) -> None:
        if not record.success:
            return
        try:
            from cdv.step_scorer import score_channel_a

            goal = record.metadata.get("goal") or "general agent task"
            result = score_channel_a(record.content, goal, self._evaluator)
            score = float(getattr(result, "score", result))
            record.metadata["cdv"] = {
                "score": score,
                "passed": score >= 0.7,
                "source": "channel_a_only",
            }
            session_id = record.session_id or "default"
            priors = _get_priors(session_id)
            observe = getattr(priors, "observe", None)
            if observe is not None:
                from cdv.priors import CallObservation

                # Real cdv 1.0.1 schema: required list fields `scores` /
                # `latencies_ms` / `metadata` (no scalar `score` field).
                observe(
                    CallObservation(
                        task_type="agent_step",
                        model_id=record.agent_id,
                        scores=[score],
                        latencies_ms=[record.latency_ms],
                        converged=score >= 0.7,
                        metadata={},
                    )
                )
        except Exception:
            # Observer contract: never break the pipeline on scoring errors.
            logger.exception("CDVObserver: scoring failed for call %s", record.call_id)


def get_stopper(session_id: str, goal: str) -> Any:
    """Per-session AdaptiveStopper (LangGraph-friendly, synchronous)."""
    # Path-keyed like _get_priors: a stopper embeds its priors, so it must not
    # outlive a cdv_store_dir change.
    key = str(_store_path(session_id))
    if key not in _stoppers:
        from cdv import AdaptiveStopper

        _stoppers[key] = AdaptiveStopper(
            priors=_get_priors(session_id),
            goal=goal,
            task_type="react_loop",
            model_id="orchestrator",
        )
    return _stoppers[key]


def stopper_should_stop(state: dict[str, Any]) -> bool:
    """Check the session's AdaptiveStopper against the latest step output.

    Returns True when the stopper's guards trip (plateau / low-ROI / budget /
    repeated output). Any error → False (never blocks the loop on CDV failure).
    """
    try:
        session_id = str(state.get("session_id", "")) or "default"
        messages = state.get("messages", [])
        last_output = ""
        for msg in reversed(messages):
            if getattr(msg, "type", "") == "tool":
                last_output = str(getattr(msg, "content", ""))
                break
        stopper = get_stopper(session_id, "react loop")
        return not stopper.should_continue(
            {
                "output": last_output,
                "tokens": int(state.get("_last_turn_tokens", 0) or 0),
            }
        )
    except Exception:
        logger.exception("stopper_should_stop: error — continuing loop")
        return False


def build_cdv_observer() -> CDVObserver:
    """Build the CDVObserver for boot-time composition.

    Does NOT call ``set_observer`` — main.py collects every enabled observer
    and installs them (single or via CompositeObserver) so several
    observer-backed features never silently overwrite each other.
    """
    logger.info("CDV verification enabled — CDVObserver created")
    return CDVObserver()
