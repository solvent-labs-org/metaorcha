"""The self-test journeys, one module each: AXIS (speed | setup | clarity | real work |
recovery), NEEDS (caps a journey cannot run without; it is skipped with the reason),
BUDGET (step -> ms, recorded always, enforced on the tablet), SCENARIOS (name -> fake
engine lines) and run(session). ORDER is the plan's order."""
from __future__ import annotations

import importlib

ORDER = ("cold_start", "setup_first_run", "ask_receipt", "tools_offline", "workflow_chain",
         "concurrency", "recovery_engine")


def names() -> list[str]:
    return list(ORDER)


def pick(wanted: list[str]) -> list[str]:
    unknown = [n for n in wanted if n not in ORDER]
    if unknown:
        raise KeyError(f"unknown journey: {', '.join(unknown)}")
    return [n for n in ORDER if n in wanted]


def load(name: str):
    if name not in ORDER:
        raise KeyError(name)
    return importlib.import_module(f"{__name__}.{name}")
