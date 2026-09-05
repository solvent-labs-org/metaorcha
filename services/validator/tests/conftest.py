"""Shared test doubles and fixture paths for the validator test suite.

Import from here (`from conftest import FakeDB, ...`) rather than from other
test modules — cross-test-module imports break under pytest's importlib mode
and couple files that should move independently.

`FIXTURE_PATH` assumes the monorepo checkout layout
(`services/validator/tests/` → repo root is three parents up); if the test
tree is relocated, update this one constant.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from validator import signer

FIXTURE_PATH = (
    Path(__file__).resolve().parents[3] / "docs/spec/fixtures/demo-charter.json"
)


def load_demo_charter() -> dict[str, Any]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


class FakeAttestationTable:
    """In-memory stand-in for the Prisma `attestation` model client."""

    def __init__(self) -> None:
        self.rows: dict[str, SimpleNamespace] = {}
        self._seq = 0

    async def create(self, data: dict[str, Any]) -> SimpleNamespace:
        self._seq += 1
        row = SimpleNamespace(id=f"att-{self._seq}", **data)
        self.rows[row.id] = row
        return row

    async def find_first(self, where: dict[str, Any]) -> SimpleNamespace | None:
        for row in self.rows.values():
            if all(getattr(row, key, None) == value for key, value in where.items()):
                return row
        return None

    async def find_unique(self, where: dict[str, Any]) -> SimpleNamespace | None:
        return await self.find_first(where)


class FakeDB:
    def __init__(self) -> None:
        self.attestation = FakeAttestationTable()


class ExplodingDB:
    """DB stand-in whose attestation table always raises (outage simulation)."""

    @property
    def attestation(self) -> Any:
        raise RuntimeError("database is down")


def _step_result(
    call_id: str,
    *,
    session_id: str = "sess-1",
    agent_id: str = "did:orcha:agent:rulebook-rag",
    tool_name: str = "search_docs",
    args: dict[str, Any] | None = None,
    content: str = "result text",
    success: bool = True,
    latency_ms: int = 100,
    completed_at: str = "2026-08-06T01:00:01+00:00",
    verdict: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        call_id=call_id,
        agent_id=agent_id,
        capability_id="cap-1",
        protocol="MCP",
        tool_name=tool_name,
        success=success,
        content=content,
        user_id="user-1",
        session_id=session_id,
        latency_ms=latency_ms,
        completed_at=completed_at,
        verdict=verdict or {"verified": success, "reason": "ok"},
        metadata=metadata or {},
        args=args if args is not None else {"query": "refund policy"},
    )


@pytest.fixture(autouse=True)
def _reset_signing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate the process-wide key cache; ephemeral key per test by default."""
    monkeypatch.delenv(signer.PRIVATE_KEY_ENV, raising=False)
    signer._reset_signing_key_for_tests()
