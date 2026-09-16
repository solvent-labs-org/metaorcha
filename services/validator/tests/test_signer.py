"""WS9 tests: Ed25519-signed attestations, persistence, and the thin anchor."""

from __future__ import annotations

import asyncio
import base64
import logging
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from emerge_node.envelope import verify_bytes
from validator import anchor, signer

CASE_PAYLOAD = {
    "session_id": "sess-1",
    "goal": "demo goal",
    "trail": [
        {"content_hash": "sha256:aaa", "prev_hash": ""},
        {"content_hash": "sha256:bbb", "prev_hash": "sha256:aaa"},
    ],
}


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

    async def update(
        self, where: dict[str, Any], data: dict[str, Any]
    ) -> SimpleNamespace:
        row = self.rows[where["id"]]
        for key, value in data.items():
            setattr(row, key, value)
        return row

    async def find_unique(self, where: dict[str, Any]) -> SimpleNamespace | None:
        return self.rows.get(where["id"])


class FakeDB:
    def __init__(self) -> None:
        self.attestation = FakeAttestationTable()


@pytest.fixture(autouse=True)
def _reset_signing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate the process-wide key cache; ephemeral key per test by default.

    Same name as the suite conftest fixture, so this one wins. Keep the
    ephemeral opt-in here or case-signing tests refuse at get_signing_key().
    """
    monkeypatch.delenv(signer.PRIVATE_KEY_ENV, raising=False)
    monkeypatch.setenv(signer.ALLOW_EPHEMERAL_KEY_ENV, "1")
    signer._reset_signing_key_for_tests()


@pytest.mark.asyncio
async def test_sign_persist_and_verify_offline() -> None:
    db = FakeDB()
    result = await signer.sign_case_attestation("sess-1", CASE_PAYLOAD, db=db)

    assert result["status"] == "pending"
    assert result["case_hash"] == signer.compute_case_hash(CASE_PAYLOAD)

    row = db.attestation.rows[result["attestation_id"]]
    assert row.session_id == "sess-1"
    assert row.case_hash == result["case_hash"]
    assert row.signature == result["signature"]
    assert row.public_key == result["public_key"]
    assert row.status == "pending"

    assert (
        signer.verify_attestation(
            CASE_PAYLOAD, result["signature"], result["public_key"]
        )
        is True
    )


@pytest.mark.asyncio
async def test_tampered_payload_fails_verification() -> None:
    db = FakeDB()
    result = await signer.sign_case_attestation("sess-1", CASE_PAYLOAD, db=db)

    tampered = {**CASE_PAYLOAD, "goal": "tampered goal"}
    assert (
        signer.verify_attestation(tampered, result["signature"], result["public_key"])
        is False
    )


@pytest.mark.asyncio
async def test_explicit_env_key_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    seed = b"\x07" * 32
    monkeypatch.setenv(signer.PRIVATE_KEY_ENV, base64.b64encode(seed).decode("ascii"))
    signer._reset_signing_key_for_tests()

    db = FakeDB()
    result = await signer.sign_case_attestation("sess-1", CASE_PAYLOAD, db=db)
    assert (
        signer.verify_attestation(
            CASE_PAYLOAD, result["signature"], result["public_key"]
        )
        is True
    )


@pytest.mark.asyncio
async def test_ephemeral_key_fallback_warns(caplog: pytest.LogCaptureFixture) -> None:
    db = FakeDB()
    with caplog.at_level(logging.WARNING, logger="validator.signer"):
        result = await signer.sign_case_attestation("sess-1", CASE_PAYLOAD, db=db)

    assert any("EPHEMERAL" in record.message for record in caplog.records)
    assert (
        signer.verify_attestation(
            CASE_PAYLOAD, result["signature"], result["public_key"]
        )
        is True
    )


@pytest.mark.asyncio
async def test_signature_compatible_with_emerge_node_envelope() -> None:
    """The attestation signature verifies with emerge_node's crypto (FR-9.1)."""
    db = FakeDB()
    result = await signer.sign_case_attestation("sess-1", CASE_PAYLOAD, db=db)

    assert (
        verify_bytes(
            result["case_hash"].encode("utf-8"),
            result["signature"],
            result["public_key"],
        )
        is True
    )


@pytest.mark.asyncio
async def test_anchor_disabled_marks_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(anchor.ENABLED_ENV, "false")
    db = FakeDB()
    result = await signer.sign_case_attestation("sess-1", CASE_PAYLOAD, db=db)

    status = await anchor.anchor_attestation(result["attestation_id"], db=db)

    assert status == "skipped"
    assert db.attestation.rows[result["attestation_id"]].status == "skipped"


class _FakeResponse:
    def __init__(self, status_code: int, body: dict[str, Any]) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> dict[str, Any]:
        return self._body


class _FakeAsyncClient:
    """Stand-in for httpx.AsyncClient returning a canned response or raising."""

    response: _FakeResponse | None = None
    error: Exception | None = None
    posted: list[dict[str, Any]] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        pass

    async def post(self, url: str, json: dict[str, Any]) -> _FakeResponse:
        self.posted.append({"url": url, "json": json})
        if self.error is not None:
            raise self.error
        assert self.response is not None
        return self.response


@pytest.mark.asyncio
async def test_anchor_enabled_persists_chain_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(anchor.ENABLED_ENV, "true")
    monkeypatch.setenv(anchor.URL_ENV, "http://anchor.test/anchor")
    _FakeAsyncClient.response = _FakeResponse(
        200, {"chain_id": "eip155:84532", "tx_hash": "0xabc"}
    )
    _FakeAsyncClient.error = None
    _FakeAsyncClient.posted = []
    monkeypatch.setattr(anchor.httpx, "AsyncClient", _FakeAsyncClient)

    db = FakeDB()
    result = await signer.sign_case_attestation("sess-1", CASE_PAYLOAD, db=db)
    status = await anchor.anchor_attestation(result["attestation_id"], db=db)

    assert status == "anchored"
    row = db.attestation.rows[result["attestation_id"]]
    assert row.status == "anchored"
    assert row.chain_id == "eip155:84532"
    assert row.tx_hash == "0xabc"
    assert row.anchored_at is not None
    # Only the case hash leaves the process — never the payload or keys.
    assert _FakeAsyncClient.posted[0]["json"] == {
        "case_hash": result["case_hash"],
        "attestation_id": result["attestation_id"],
    }


@pytest.mark.asyncio
async def test_anchor_endpoint_down_stays_pending_no_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(anchor.ENABLED_ENV, "true")
    monkeypatch.setenv(anchor.URL_ENV, "http://anchor.test/anchor")
    _FakeAsyncClient.response = None
    _FakeAsyncClient.error = httpx.ConnectError("connection refused")
    _FakeAsyncClient.posted = []
    monkeypatch.setattr(anchor.httpx, "AsyncClient", _FakeAsyncClient)

    db = FakeDB()
    result = await signer.sign_case_attestation("sess-1", CASE_PAYLOAD, db=db)
    status = await anchor.anchor_attestation(result["attestation_id"], db=db)

    assert status == "pending"
    assert db.attestation.rows[result["attestation_id"]].status == "pending"


@pytest.mark.asyncio
async def test_finalize_case_signs_and_fires_background_anchor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(anchor.ENABLED_ENV, "false")
    db = FakeDB()

    result = await signer.finalize_case("sess-1", CASE_PAYLOAD, db=db)
    assert result["status"] == "pending"

    # Background anchor task completes on its own; row ends up skipped.
    await asyncio.gather(*list(signer._background_tasks))
    assert db.attestation.rows[result["attestation_id"]].status == "skipped"
