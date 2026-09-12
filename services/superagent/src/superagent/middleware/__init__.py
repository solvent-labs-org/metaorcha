"""Execution middleware pipeline."""

from .audit_ledger import LedgerObserver
from .observers import (
    CompositeObserver,
    ExecutionObserver,
    NoOpObserver,
    StepResult,
    emit_step_complete,
    get_observer,
    set_observer,
)

__all__ = [
    "CompositeObserver",
    "ExecutionMiddleware",
    "ExecutionObserver",
    "LedgerObserver",
    "NoOpObserver",
    "StepResult",
    "emit_step_complete",
    "get_observer",
    "set_observer",
]


def __getattr__(name: str):
    if name == "ExecutionMiddleware":
        from .pipeline import ExecutionMiddleware

        return ExecutionMiddleware
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
