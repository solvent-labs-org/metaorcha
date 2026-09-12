"""HTTP fetch of a sealed RFC 0003 envelope — serves bytes, never verifies."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import HTTPException
from superagent.api.routes import get_run_attestation, get_session_attestation
from superagent.graph.runner import _done_event
from superagent.middleware.observers import NoOpObserver, set_observer


@pytest.fixture(autouse=True)
def _restore_observer():
    set_observer(NoOpObserver())
    yield
    set_observer(NoOpObserver())


ENVELOPE = {
    "format": "orcha.run-attestation/v1",
    "run_id": "sess-1-abc123def456",
    "signature": "not-checked-here",
}


@pytest.mark.asyncio
async def test_get_run_attestation_returns_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    verify_calls: list[Any] = []

    async def _record(run_id: str, db: Any = None) -> dict[str, Any]:
        return {
            "session_id": "sess-1",
            "run_id": run_id,
            "envelope": ENVELOPE,
        }

    async def _owner(session_id: str, user_id: str) -> bool:
        return session_id == "sess-1" and user_id == "alice"

    def _boom(*_args: object, **_kwargs: object) -> None:
        verify_calls.append(True)
        raise AssertionError("fetch route must not verify")

    monkeypatch.setattr(
        "validator.run_envelope.get_run_attestation_record", _record
    )
    monkeypatch.setattr(
        "superagent.persistence.transcript_store.verify_session_owner", _owner
    )
    monkeypatch.setattr("emerge.run_attestation.verify_run_attestation", _boom)

    response = await get_run_attestation("sess-1-abc123def456", user_id="alice")

    assert response.status_code == 200
    assert response.body  # raw envelope, not a wrapper
    assert b"orcha.run-attestation/v1" in response.body
    assert (
        response.headers["content-disposition"]
        == 'attachment; filename="sess-1-abc123def456.json"'
    )
    assert verify_calls == []


@pytest.mark.asyncio
async def test_get_run_attestation_unknown_is_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _missing(run_id: str, db: Any = None) -> None:
        return None

    monkeypatch.setattr(
        "validator.run_envelope.get_run_attestation_record", _missing
    )
    with pytest.raises(HTTPException) as exc:
        await get_run_attestation("never", user_id="alice")
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_get_run_attestation_wrong_owner_is_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _record(run_id: str, db: Any = None) -> dict[str, Any]:
        return {
            "session_id": "sess-1",
            "run_id": run_id,
            "envelope": ENVELOPE,
        }

    async def _owner(session_id: str, user_id: str) -> bool:
        return False

    monkeypatch.setattr(
        "validator.run_envelope.get_run_attestation_record", _record
    )
    monkeypatch.setattr(
        "superagent.persistence.transcript_store.verify_session_owner", _owner
    )
    with pytest.raises(HTTPException) as exc:
        await get_run_attestation("sess-1-abc123def456", user_id="eve")
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_get_session_attestation_returns_latest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _latest(session_id: str, db: Any = None) -> dict[str, Any]:
        return {
            "session_id": session_id,
            "run_id": ENVELOPE["run_id"],
            "envelope": ENVELOPE,
        }

    async def _owner(session_id: str, user_id: str) -> bool:
        return True

    monkeypatch.setattr(
        "validator.run_envelope.get_latest_run_attestation_for_session",
        _latest,
    )
    monkeypatch.setattr(
        "superagent.persistence.transcript_store.verify_session_owner", _owner
    )

    response = await get_session_attestation("sess-1", user_id="alice")
    assert response.status_code == 200
    assert b"sess-1-abc123def456" in response.body


def test_done_event_stock_is_unchanged() -> None:
    assert _done_event("sess-1") == {"type": "done", "session_id": "sess-1"}


def test_done_event_adds_fetch_path_when_published() -> None:
    class _Publisher:
        published = {"sess-1": "sess-1-abc123def456"}

        async def on_step_complete(self, record: object) -> None:
            return None

    set_observer(_Publisher())  # type: ignore[arg-type]
    assert _done_event("sess-1") == {
        "type": "done",
        "session_id": "sess-1",
        "run_id": "sess-1-abc123def456",
        "attestation_path": "/runs/sess-1-abc123def456/attestation",
    }
