"""Attestation-gated settle (Story 2.1, AD-1/AD-5/AD-6/AD-9).

Tests use the REAL vendored verifier (`emerge.run_attestation`) against real
built+signed envelopes — verify semantics are load-bearing (AD-5). The
verifier is mocked only when a test must reach gate policy after integrity
is assumed. Only the DB lookup/write and the Prisma client are faked.
"""

from __future__ import annotations

import base64
from types import SimpleNamespace
from typing import Any

import pytest

CHARTER = "b" * 64
WRONG_CHARTER = "c" * 64


@pytest.fixture(autouse=True)
def _reset_signing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from validator import signer

    monkeypatch.delenv(signer.PRIVATE_KEY_ENV, raising=False)
    signer._reset_signing_key_for_tests()


@pytest.fixture()
def make_envelope():
    from validator.run_envelope import build_run_envelope, sign_run_envelope

    def _make(
        run_id: str,
        *,
        charter_hash: str | None = CHARTER,
        tamper: bool = False,
        signer_did: str = "did:orcha:system:validator",
        agent_dids: list[str] | None = None,
        break_signature: bool = False,
        verdicts: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        unsigned = build_run_envelope(
            run_id=run_id,
            agent_dids=["did:orcha:agent:kya-demo"],
            charter_hash=charter_hash,
            policy_version="p/1",
            steps=[
                {
                    "call_id": "c1",
                    "tool": "search_docs",
                    "args": {"q": "x"},
                    "output": "y",
                    "success": True,
                    "latency_ms": 10,
                }
            ],
            verdicts=list(verdicts) if verdicts is not None else [],
            started_at="2026-08-06T01:00:00Z",
            finished_at="2026-08-06T01:00:01Z",
            signer_did=signer_did,
        )
        if agent_dids is not None:
            # build_run_envelope enforces the DID form, so a non-conformant DID
            # is substituted into the built envelope and the envelope is signed
            # over it. The result is well-formed and validly signed in every
            # respect but the one under test — which is what the acceptance
            # criterion asks for.
            unsigned["agent_dids"] = list(agent_dids)
        signed = sign_run_envelope(unsigned)
        if tamper:
            signed["steps"][0]["output_hash"] = "0" * 64
        if break_signature:
            # Valid base64 over the wrong bytes: the schema and the step chain
            # are untouched, so the signature is the first check to fail.
            signed["signature"] = base64.b64encode(b"\x00" * 64).decode("ascii")
        return signed

    return _make


class FakeUniqueViolation(Exception):
    """Stands in for ``prisma.errors.UniqueViolationError`` (code P2002)."""

    code = "P2002"


class FakeSettlementsTable:
    """In-memory ``attested_settlements`` that MODELS the unique index.

    Story 2.3 puts a unique index on ``settled_run_id``. A fake that ignored
    it would let the idempotency tests pass while the real constraint went
    unexercised — the exact green-mock failure the story exists to prevent.
    NULLs are distinct in a Postgres unique index, so refusals (claim NULL)
    are unconstrained and only a second SETTLED row for one run collides.
    """

    def __init__(self) -> None:
        self.rows: list[SimpleNamespace] = []

    async def create(self, data: dict[str, Any]) -> SimpleNamespace:
        claim = data.get("settled_run_id")
        if claim is not None and any(
            getattr(r, "settled_run_id", None) == claim for r in self.rows
        ):
            raise FakeUniqueViolation(
                "duplicate key value violates unique constraint "
                '"attested_settlements_settled_run_id_key"'
            )
        row = SimpleNamespace(id=f"as-{len(self.rows) + 1}", **data)
        self.rows.append(row)
        return row

    async def find_first(self, where: dict[str, Any]) -> SimpleNamespace | None:
        claim = where.get("settled_run_id")
        for row in self.rows:
            if getattr(row, "settled_run_id", None) == claim:
                return row
        return None


class FakeGateDB:
    def __init__(self) -> None:
        self.attestedsettlement = FakeSettlementsTable()


@pytest.fixture()
def gate_db() -> FakeGateDB:
    return FakeGateDB()


@pytest.fixture()
def mock_lookup(monkeypatch: pytest.MonkeyPatch):
    """Patch the lazy-imported attestation lookup. Set `.return_value` per test."""
    import validator.run_envelope

    state: dict[str, Any] = {"envelope": None}

    async def _fake(run_id: str, db: Any = None) -> Any:
        state["called_with"] = run_id
        return state["envelope"]

    monkeypatch.setattr(validator.run_envelope, "get_run_attestation_by_run_id", _fake)
    return state


def _failed_checks(row: SimpleNamespace) -> list[str]:
    payload = getattr(row.failed_checks, "data", row.failed_checks)
    return list(payload)


def _assert_refusal_audited(
    gate_db: FakeGateDB, run_id: str, checks: list[str]
) -> None:
    """The rest of the Story 2.2 contract for one refusal.

    The refusal is audited, the row names exactly the checks that failed, and
    no settled credit exists for the run. At this layer "no settled credit" is
    "no settled row and no claim": ``gate_attested_settle`` writes no credit
    itself — ``settle_invocation`` does, and only for a settled outcome — so a
    refusal that left either behind would become a credit one layer up.
    ``test_settle_gate_flow.py`` pins the same thing against the credit write.
    """
    rows = [r for r in gate_db.attestedsettlement.rows if r.run_id == run_id]
    assert rows, "every refusal is audited (AD-6)"
    assert rows[-1].outcome == "refused"
    assert _failed_checks(rows[-1]) == checks
    assert all(r.outcome != "settled" for r in rows)
    assert all(getattr(r, "settled_run_id", None) is None for r in rows)


async def test_valid_envelope_settles(make_envelope, gate_db, mock_lookup) -> None:
    from superagent.pricing.settle_gate import gate_attested_settle

    envelope = make_envelope("run-a")
    mock_lookup["envelope"] = envelope

    result = await gate_attested_settle(
        run_id="run-a", session_id="sess-1", expected_charter_hash=CHARTER, db=gate_db
    )

    assert result["outcome"] == "settled"
    assert result["failed_checks"] == []
    assert mock_lookup["called_with"] == "run-a"
    assert len(gate_db.attestedsettlement.rows) == 1
    row = gate_db.attestedsettlement.rows[0]
    assert row.outcome == "settled"
    assert row.run_id == "run-a"
    assert row.charter_hash == CHARTER
    assert _failed_checks(row) == []
    assert len(row.envelope_digest) == 64


async def test_fail_verdict_refuses_and_sdk_still_valid(
    make_envelope, gate_db, mock_lookup
) -> None:
    from emerge.run_attestation import verify_run_attestation
    from superagent.pricing.settle_gate import CHECK_VERDICT_FAIL, gate_attested_settle

    envelope = make_envelope(
        "run-fail",
        verdicts=[{"check": "exit_zero", "result": "fail", "detail": "tests"}],
    )
    mock_lookup["envelope"] = envelope

    sdk = verify_run_attestation(envelope)
    assert sdk.valid is True
    assert CHECK_VERDICT_FAIL not in sdk.checks

    result = await gate_attested_settle(
        run_id="run-fail",
        session_id="sess-1",
        expected_charter_hash=CHARTER,
        db=gate_db,
    )

    assert result["outcome"] == "refused"
    assert result["failed_checks"] == [CHECK_VERDICT_FAIL]
    _assert_refusal_audited(gate_db, "run-fail", [CHECK_VERDICT_FAIL])

    again = await gate_attested_settle(
        run_id="run-fail",
        session_id="sess-1",
        expected_charter_hash=CHARTER,
        db=gate_db,
    )
    assert again["outcome"] == "refused"
    assert again["failed_checks"] == [CHECK_VERDICT_FAIL]
    assert all(r.outcome != "settled" for r in gate_db.attestedsettlement.rows)


async def test_two_fail_verdicts_name_one_check(
    make_envelope, gate_db, mock_lookup
) -> None:
    from superagent.pricing.settle_gate import CHECK_VERDICT_FAIL, gate_attested_settle

    mock_lookup["envelope"] = make_envelope(
        "run-two-fail",
        verdicts=[
            {"check": "exit_zero", "result": "fail"},
            {"check": "lint", "result": "fail"},
        ],
    )

    result = await gate_attested_settle(
        run_id="run-two-fail", expected_charter_hash=CHARTER, db=gate_db
    )

    assert result["outcome"] == "refused"
    assert result["failed_checks"] == [CHECK_VERDICT_FAIL]


async def test_warn_verdict_does_not_refuse(
    make_envelope, gate_db, mock_lookup
) -> None:
    from superagent.pricing.settle_gate import gate_attested_settle

    mock_lookup["envelope"] = make_envelope(
        "run-warn",
        verdicts=[{"check": "lint", "result": "warn"}],
    )

    result = await gate_attested_settle(
        run_id="run-warn", expected_charter_hash=CHARTER, db=gate_db
    )

    assert result["outcome"] == "settled"
    assert result["failed_checks"] == []
    assert gate_db.attestedsettlement.rows[0].outcome == "settled"


async def test_pass_verdict_still_settles(make_envelope, gate_db, mock_lookup) -> None:
    from superagent.pricing.settle_gate import gate_attested_settle

    mock_lookup["envelope"] = make_envelope(
        "run-pass",
        verdicts=[{"check": "exit_zero", "result": "pass"}],
    )

    result = await gate_attested_settle(
        run_id="run-pass", expected_charter_hash=CHARTER, db=gate_db
    )

    assert result["outcome"] == "settled"
    assert result["failed_checks"] == []


async def test_malformed_verdicts_refuse_verify_error(
    make_envelope, gate_db, mock_lookup
) -> None:
    from superagent.pricing.settle_gate import gate_attested_settle

    envelope = make_envelope("run-bad-v")
    envelope["verdicts"] = "not-a-list"
    mock_lookup["envelope"] = envelope

    result = await gate_attested_settle(
        run_id="run-bad-v", expected_charter_hash=CHARTER, db=gate_db
    )

    # Tampering verdicts after sign fails integrity first — still refuse,
    # no credit. The explicit unreadable path is covered when verify is
    # bypassed below via a structurally valid envelope whose field is wrong.
    assert result["outcome"] == "refused"
    assert result["failed_checks"]
    _assert_refusal_audited(gate_db, "run-bad-v", result["failed_checks"])


async def test_unreadable_verdicts_after_verify_is_verify_error(
    make_envelope, gate_db, mock_lookup, monkeypatch: pytest.MonkeyPatch
) -> None:
    import emerge.run_attestation
    from superagent.pricing.settle_gate import gate_attested_settle

    envelope = make_envelope("run-unread")
    envelope["verdicts"] = {"oops": True}
    mock_lookup["envelope"] = envelope

    monkeypatch.setattr(
        emerge.run_attestation,
        "verify_run_attestation",
        lambda _env: type("V", (), {"valid": True, "checks": {}})(),
    )

    result = await gate_attested_settle(
        run_id="run-unread", expected_charter_hash=CHARTER, db=gate_db
    )

    assert result["outcome"] == "refused"
    assert result["failed_checks"] == ["verify_error"]
    _assert_refusal_audited(gate_db, "run-unread", ["verify_error"])


async def test_missing_attestation_refuses(gate_db, mock_lookup) -> None:
    from superagent.pricing.settle_gate import gate_attested_settle

    result = await gate_attested_settle(
        run_id="run-ghost", expected_charter_hash=CHARTER, db=gate_db
    )

    assert result["outcome"] == "refused"
    assert result["failed_checks"] == ["missing_attestation"]
    row = gate_db.attestedsettlement.rows[0]
    assert row.outcome == "refused"
    assert _failed_checks(row) == ["missing_attestation"]
    _assert_refusal_audited(gate_db, "run-ghost", ["missing_attestation"])


async def test_run_id_mismatch_refuses(make_envelope, gate_db, mock_lookup) -> None:
    from superagent.pricing.settle_gate import gate_attested_settle

    mock_lookup["envelope"] = make_envelope("run-a")  # envelope A…

    result = await gate_attested_settle(
        run_id="run-b",
        expected_charter_hash=CHARTER,
        db=gate_db,  # …settling run B
    )

    assert result["outcome"] == "refused"
    assert result["failed_checks"] == ["run_id_mismatch"]
    _assert_refusal_audited(gate_db, "run-b", ["run_id_mismatch"])


async def test_tampered_steps_refuses(make_envelope, gate_db, mock_lookup) -> None:
    from superagent.pricing.settle_gate import gate_attested_settle

    mock_lookup["envelope"] = make_envelope("run-a", tamper=True)

    result = await gate_attested_settle(
        run_id="run-a", expected_charter_hash=CHARTER, db=gate_db
    )

    assert result["outcome"] == "refused"
    assert result["failed_checks"] == ["steps_root"]
    _assert_refusal_audited(gate_db, "run-a", ["steps_root"])


async def test_verify_exception_refuses(
    make_envelope, gate_db, mock_lookup, monkeypatch: pytest.MonkeyPatch
) -> None:
    import emerge.run_attestation
    from superagent.pricing.settle_gate import gate_attested_settle

    mock_lookup["envelope"] = make_envelope("run-a")

    def _boom(envelope: Any) -> Any:
        raise RuntimeError("verifier exploded")

    monkeypatch.setattr(emerge.run_attestation, "verify_run_attestation", _boom)

    result = await gate_attested_settle(
        run_id="run-a", expected_charter_hash=CHARTER, db=gate_db
    )

    assert result["outcome"] == "refused"
    assert result["failed_checks"] == ["verify_error"]
    _assert_refusal_audited(gate_db, "run-a", ["verify_error"])


async def test_null_charter_refuses_on_settling_path(
    make_envelope, gate_db, mock_lookup
) -> None:
    from superagent.pricing.settle_gate import gate_attested_settle

    mock_lookup["envelope"] = make_envelope("run-a", charter_hash=None)

    result = await gate_attested_settle(
        run_id="run-a", expected_charter_hash=CHARTER, db=gate_db
    )

    assert result["outcome"] == "refused"
    assert result["failed_checks"] == ["charter_hash"]
    _assert_refusal_audited(gate_db, "run-a", ["charter_hash"])


async def test_wrong_charter_refuses(make_envelope, gate_db, mock_lookup) -> None:
    from superagent.pricing.settle_gate import gate_attested_settle

    mock_lookup["envelope"] = make_envelope("run-a", charter_hash=CHARTER)

    result = await gate_attested_settle(
        run_id="run-a", expected_charter_hash=WRONG_CHARTER, db=gate_db
    )

    assert result["outcome"] == "refused"
    assert result["failed_checks"] == ["charter_hash"]
    _assert_refusal_audited(gate_db, "run-a", ["charter_hash"])


async def test_unset_expected_charter_refuses_on_settling_path(
    make_envelope, gate_db, mock_lookup
) -> None:
    from superagent.pricing.settle_gate import gate_attested_settle

    mock_lookup["envelope"] = make_envelope("run-a")

    result = await gate_attested_settle(
        run_id="run-a", expected_charter_hash=None, db=gate_db
    )

    assert result["outcome"] == "refused"
    assert result["failed_checks"] == ["charter_hash"]
    _assert_refusal_audited(gate_db, "run-a", ["charter_hash"])


# ── Story 2.2 — the two refusal classes the enumeration was missing ──────────


async def test_a_non_conformant_agent_did_refuses_naming_only_agent_did(
    make_envelope, gate_db, mock_lookup
) -> None:
    """An envelope carrying an agent DID outside the `did:orcha:` namespace is
    schema-valid (RFC 0003 binds DID syntax, not the method) and validly
    signed, and the gate refuses it on the platform profile alone.

    `failed_checks` must say `agent_did` and only `agent_did`: an auditor
    reading `schema` would conclude the envelope is malformed, and one reading
    `signature` would conclude it was forged — both different and worse
    claims than the true one.
    """
    from superagent.pricing.settle_gate import gate_attested_settle

    mock_lookup["envelope"] = make_envelope(
        "run-bad-did", agent_dids=["did:emerge:foo"]
    )

    result = await gate_attested_settle(
        run_id="run-bad-did", expected_charter_hash=CHARTER, db=gate_db
    )

    assert result["outcome"] == "refused"
    assert result["failed_checks"] == ["agent_did"]
    _assert_refusal_audited(gate_db, "run-bad-did", ["agent_did"])


async def test_a_bad_signature_refuses_naming_only_signature(
    make_envelope, gate_db, mock_lookup
) -> None:
    """Schema and step chain intact, signature wrong: the last check is the
    only one that failed, and it is the only one named."""
    from superagent.pricing.settle_gate import gate_attested_settle

    mock_lookup["envelope"] = make_envelope("run-bad-sig", break_signature=True)

    result = await gate_attested_settle(
        run_id="run-bad-sig", expected_charter_hash=CHARTER, db=gate_db
    )

    assert result["outcome"] == "refused"
    assert result["failed_checks"] == ["signature"]
    _assert_refusal_audited(gate_db, "run-bad-sig", ["signature"])


# ── SettlementGateObserver (Story 2.1, AC4) ──────────────────────────────────


class _FakeAttestationObserver:
    def __init__(
        self, envelopes: dict, last_sealed: dict[str, str] | None = None
    ) -> None:
        self.envelopes = envelopes
        self.last_sealed = last_sealed if last_sealed is not None else {}


@pytest.fixture(autouse=True)
def _clean_deferred_map():
    from superagent.pricing import settlement

    settlement._deferred_settles.clear()
    yield
    settlement._deferred_settles.clear()


@pytest.fixture(autouse=True)
def _fast_defer_recheck(monkeypatch: pytest.MonkeyPatch):
    """Shrink the observer's bounded re-check so no-defer tests stay fast."""
    from superagent.pricing import settle_gate

    monkeypatch.setattr(settle_gate, "DEFER_RECHECKS", 2)
    monkeypatch.setattr(settle_gate, "DEFER_RECHECK_DELAY_S", 0.01)


async def test_observer_replays_deferred_settle_post_seal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from superagent.config import settings
    from superagent.pricing import settlement
    from superagent.pricing.settle_gate import SettlementGateObserver

    monkeypatch.setattr(settings, "settlement_require_attestation", True)
    settlement._store_deferred(
        "sess-1", {"user_id": "u1", "session_id": "sess-1", "call_id": "c1"}
    )
    attestation_obs = _FakeAttestationObserver(
        {"sess-1-abc123": {"run_id": "sess-1-abc123"}},
        last_sealed={"sess-1": "sess-1-abc123"},
    )
    calls: list[dict] = []

    async def _spy_settle(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(settlement, "settle_invocation", _spy_settle)

    observer = SettlementGateObserver(attestation_obs)
    await observer.on_run_complete("sess-1")

    assert len(calls) == 1
    assert calls[0]["run_id"] == "sess-1-abc123"
    assert calls[0]["user_id"] == "u1"
    assert settlement._deferred_settles == {}  # popped exactly once
    assert attestation_obs.last_sealed == {}  # binding consumed, not reusable


async def test_observer_noop_without_deferred_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from superagent.config import settings
    from superagent.pricing import settlement
    from superagent.pricing.settle_gate import SettlementGateObserver

    monkeypatch.setattr(settings, "settlement_require_attestation", True)
    calls: list[dict] = []
    monkeypatch.setattr(settlement, "settle_invocation", lambda **kw: calls.append(kw))

    observer = SettlementGateObserver(_FakeAttestationObserver({}))
    await observer.on_run_complete("sess-1")
    assert calls == []


async def test_observer_drops_deferred_when_no_sealed_envelope(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    from superagent.config import settings
    from superagent.pricing import settlement
    from superagent.pricing.settle_gate import SettlementGateObserver

    monkeypatch.setattr(settings, "settlement_require_attestation", True)
    settlement._store_deferred("sess-1", {"user_id": "u1"})
    calls: list[dict] = []
    monkeypatch.setattr(settlement, "settle_invocation", lambda **kw: calls.append(kw))

    observer = SettlementGateObserver(_FakeAttestationObserver({}))
    with caplog.at_level(logging.WARNING):
        await observer.on_run_complete("sess-1")

    assert calls == []
    assert "no sealed envelope" in caplog.text


async def test_observer_noop_when_flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    from superagent.config import settings
    from superagent.pricing import settlement
    from superagent.pricing.settle_gate import SettlementGateObserver

    monkeypatch.setattr(settings, "settlement_require_attestation", False)
    settlement._store_deferred("sess-1", {"user_id": "u1"})
    calls: list[dict] = []
    monkeypatch.setattr(settlement, "settle_invocation", lambda **kw: calls.append(kw))

    observer = SettlementGateObserver(
        _FakeAttestationObserver(
            {"sess-1-abc": {"run_id": "sess-1-abc"}},
            last_sealed={"sess-1": "sess-1-abc"},
        )
    )
    await observer.on_run_complete("sess-1")
    assert calls == []
    # Pop-then-check: a mid-process flip-off flushes the entry, never leaks it.
    assert settlement._deferred_settles == {}


async def test_observer_discard_run_drops_deferred(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An abandoned run's deferred fee must never settle against a later run."""
    from superagent.config import settings
    from superagent.pricing import settlement
    from superagent.pricing.settle_gate import SettlementGateObserver

    monkeypatch.setattr(settings, "settlement_require_attestation", True)
    settlement._store_deferred("sess-1", {"user_id": "u1", "session_id": "sess-1"})

    observer = SettlementGateObserver(_FakeAttestationObserver({}))
    await observer.discard_run("sess-1")
    assert settlement._deferred_settles == {}

    # The later run on the same session finds nothing to replay.
    calls: list[dict] = []
    monkeypatch.setattr(settlement, "settle_invocation", lambda **kw: calls.append(kw))
    await observer.on_run_complete("sess-1")
    assert calls == []


async def test_observer_recheck_catches_late_defer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A defer that lands after run-complete dispatch is still settled."""
    import asyncio

    from superagent.config import settings
    from superagent.pricing import settlement
    from superagent.pricing.settle_gate import SettlementGateObserver

    monkeypatch.setattr(settings, "settlement_require_attestation", True)
    attestation_obs = _FakeAttestationObserver(
        {}, last_sealed={"sess-1": "sess-1-late01"}
    )
    calls: list[dict] = []

    async def _spy_settle(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(settlement, "settle_invocation", _spy_settle)

    async def _late_defer():
        await asyncio.sleep(0.005)
        settlement._store_deferred("sess-1", {"user_id": "u1", "session_id": "sess-1"})

    observer = SettlementGateObserver(attestation_obs)
    await asyncio.gather(observer.on_run_complete("sess-1"), _late_defer())

    assert len(calls) == 1
    assert calls[0]["run_id"] == "sess-1-late01"


# ── Gate policy + hardening (2.1 review findings) ─────────────────────────────


async def test_wrong_signer_did_refuses(make_envelope, gate_db, mock_lookup) -> None:
    """Self-signed envelope from a non-platform signer refuses (trust anchor)."""
    from superagent.pricing.settle_gate import gate_attested_settle

    envelope = make_envelope("run-rogue", signer_did="did:orcha:system:rogue")
    mock_lookup["envelope"] = envelope

    result = await gate_attested_settle(
        run_id="run-rogue", expected_charter_hash=CHARTER, db=gate_db
    )

    assert result["outcome"] == "refused"
    assert result["failed_checks"] == ["signer_did"]
    row = gate_db.attestedsettlement.rows[0]
    assert row.outcome == "refused"
    assert _failed_checks(row) == ["signer_did"]
    _assert_refusal_audited(gate_db, "run-rogue", ["signer_did"])


async def test_settled_audit_write_failure_refuses(make_envelope, mock_lookup) -> None:
    """No settled outcome without an audit row: write failure → refused."""
    from superagent.pricing.settle_gate import gate_attested_settle

    class _BrokenSettlementsTable:
        async def create(self, data: dict[str, Any]) -> None:
            raise RuntimeError("db down")

    class _BrokenDB:
        attestedsettlement = _BrokenSettlementsTable()

    mock_lookup["envelope"] = make_envelope("run-a")
    result = await gate_attested_settle(
        run_id="run-a", expected_charter_hash=CHARTER, db=_BrokenDB()
    )

    assert result["outcome"] == "refused"
    assert result["failed_checks"] == ["audit_write_error"]
    # There is no audit table to query — this database is down by construction.
    # "No settled credit" is carried by the outcome alone: settle_invocation
    # writes credit only for a settled outcome, and this one refused.


async def test_refuse_audit_write_failure_still_refuses(mock_lookup) -> None:
    """gate_attested_settle never raises, even when the refuse-audit write dies."""
    from superagent.pricing.settle_gate import gate_attested_settle

    class _BrokenSettlementsTable:
        async def create(self, data: dict[str, Any]) -> None:
            raise RuntimeError("db down")

    class _BrokenDB:
        attestedsettlement = _BrokenSettlementsTable()

    mock_lookup["envelope"] = None  # missing attestation → refuse path
    result = await gate_attested_settle(
        run_id="run-ghost", expected_charter_hash=CHARTER, db=_BrokenDB()
    )

    assert result["outcome"] == "refused"
    assert result["failed_checks"] == ["missing_attestation"]
    # Same as above: a refusal stands without its audit row, and a refusal is
    # the thing that keeps credit from being written.


async def test_lookup_exception_refuses(monkeypatch: pytest.MonkeyPatch, gate_db):
    """A raising lookup is fail-closed, not a crash."""
    import validator.run_envelope
    from superagent.pricing.settle_gate import gate_attested_settle

    async def _boom(run_id: str, db: Any = None) -> Any:
        raise RuntimeError("lookup exploded")

    monkeypatch.setattr(validator.run_envelope, "get_run_attestation_by_run_id", _boom)

    result = await gate_attested_settle(
        run_id="run-a", expected_charter_hash=CHARTER, db=gate_db
    )
    assert result["outcome"] == "refused"
    assert result["failed_checks"] == ["verify_error"]
    _assert_refusal_audited(gate_db, "run-a", ["verify_error"])


def test_validate_gate_config_raises_on_flag_combo() -> None:
    """Gate flag without envelope production is a refused boot, not a warning."""
    from superagent.pricing.settle_gate import validate_gate_config

    bad = SimpleNamespace(
        settlement_require_attestation=True,
        run_attestation_enabled=False,
        run_attestation_charter_hash=CHARTER,
    )
    with pytest.raises(RuntimeError, match="silent free tier"):
        validate_gate_config(bad)


def test_validate_gate_config_warns_on_missing_charter(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    from superagent.pricing.settle_gate import validate_gate_config

    settle_nothing = SimpleNamespace(
        settlement_require_attestation=True,
        run_attestation_enabled=True,
        run_attestation_charter_hash=None,
    )
    with caplog.at_level(logging.WARNING):
        validate_gate_config(settle_nothing)
    assert "settles nothing" in caplog.text


def test_validate_gate_config_accepts_stock_and_full_kya() -> None:
    from superagent.pricing.settle_gate import validate_gate_config

    stock = SimpleNamespace(
        settlement_require_attestation=False,
        run_attestation_enabled=False,
        run_attestation_charter_hash=None,
    )
    kya = SimpleNamespace(
        settlement_require_attestation=True,
        run_attestation_enabled=True,
        run_attestation_charter_hash=CHARTER,
    )
    validate_gate_config(stock)
    validate_gate_config(kya)


# ── Story 2.3 — settle idempotency per run_id (AR-11) ────────────────────────
#
# The concurrency AC ("two concurrent settle attempts ... exactly one settled
# row") is NOT proven here. These tests run against FakeGateDB; a fake cannot
# prove a database constraint arbitrates a real race. The proof lives in
# test_settle_gate_idempotency_pg.py, which runs two genuinely concurrent
# transactions against a real Postgres. What these tests DO prove is the gate's
# behaviour on each side of that constraint.


async def test_settled_row_carries_the_claim_and_refused_row_does_not(
    make_envelope, gate_db, mock_lookup
) -> None:
    """The claim column is what the unique index constrains — assert it."""
    from superagent.pricing.settle_gate import gate_attested_settle

    mock_lookup["envelope"] = make_envelope("run-claim")
    settled = await gate_attested_settle(
        run_id="run-claim", expected_charter_hash=CHARTER, db=gate_db
    )
    assert settled["outcome"] == "settled"
    assert gate_db.attestedsettlement.rows[0].settled_run_id == "run-claim"

    # A refusal must leave the claim NULL, or refusals would collide with
    # each other and Story 2.2's enumeration would break.
    mock_lookup["envelope"] = make_envelope("run-refuse", charter_hash=WRONG_CHARTER)
    refused = await gate_attested_settle(
        run_id="run-refuse", expected_charter_hash=CHARTER, db=gate_db
    )
    assert refused["outcome"] == "refused"
    assert gate_db.attestedsettlement.rows[1].settled_run_id is None


async def test_replaying_a_settled_run_refuses_already_settled(
    make_envelope, gate_db, mock_lookup
) -> None:
    """AC1 — re-invoking settle for a settled run_id writes no second credit."""
    from superagent.pricing.settle_gate import gate_attested_settle

    mock_lookup["envelope"] = make_envelope("run-dup")

    first = await gate_attested_settle(
        run_id="run-dup", expected_charter_hash=CHARTER, db=gate_db
    )
    second = await gate_attested_settle(
        run_id="run-dup", expected_charter_hash=CHARTER, db=gate_db
    )

    assert first["outcome"] == "settled"
    assert second["outcome"] == "refused"
    assert second["failed_checks"] == ["already_settled"]

    settled_rows = [
        r for r in gate_db.attestedsettlement.rows if r.outcome == "settled"
    ]
    assert len(settled_rows) == 1

    # The refusal is audited, and carries the verified digest — the pre-check
    # runs after verification precisely so this row is not blank.
    refused_row = gate_db.attestedsettlement.rows[1]
    assert _failed_checks(refused_row) == ["already_settled"]
    assert len(refused_row.envelope_digest) == 64
    assert refused_row.charter_hash == CHARTER


async def test_losing_the_write_race_refuses_already_settled(
    monkeypatch: pytest.MonkeyPatch, make_envelope, gate_db, mock_lookup
) -> None:
    """The index, not the pre-check, is the authority.

    Blind the TOCTOU pre-check so it reports "not claimed" — exactly what
    happens to the loser of a real race, which reads before the winner
    commits. The unique violation on the write must still refuse, and must be
    named `already_settled` rather than collapsing into `audit_write_error`.
    """
    from superagent.pricing import settle_gate

    mock_lookup["envelope"] = make_envelope("run-race")

    first = await settle_gate.gate_attested_settle(
        run_id="run-race", expected_charter_hash=CHARTER, db=gate_db
    )
    assert first["outcome"] == "settled"

    async def _blind_precheck(db, run_id):  # noqa: ARG001
        return False

    monkeypatch.setattr(settle_gate, "_settled_claim_exists", _blind_precheck)

    second = await settle_gate.gate_attested_settle(
        run_id="run-race", expected_charter_hash=CHARTER, db=gate_db
    )

    assert second["outcome"] == "refused"
    assert second["failed_checks"] == ["already_settled"]
    settled_rows = [
        r for r in gate_db.attestedsettlement.rows if r.outcome == "settled"
    ]
    assert len(settled_rows) == 1


async def test_a_failed_precheck_read_does_not_fail_open(
    monkeypatch: pytest.MonkeyPatch, make_envelope, gate_db, mock_lookup
) -> None:
    """A raising pre-check must defer to the index, never settle twice."""
    from superagent.pricing import settle_gate

    mock_lookup["envelope"] = make_envelope("run-precheck-boom")
    first = await settle_gate.gate_attested_settle(
        run_id="run-precheck-boom", expected_charter_hash=CHARTER, db=gate_db
    )
    assert first["outcome"] == "settled"

    async def _boom(db, run_id):  # noqa: ARG001
        raise RuntimeError("pre-check read failed")

    monkeypatch.setattr(settle_gate, "_settled_claim_exists", _boom)

    second = await settle_gate.gate_attested_settle(
        run_id="run-precheck-boom", expected_charter_hash=CHARTER, db=gate_db
    )
    assert second["outcome"] == "refused"
    assert second["failed_checks"] == ["already_settled"]


async def test_many_refusals_for_one_run_are_all_audited(
    make_envelope, gate_db, mock_lookup
) -> None:
    """Guards Story 2.2 against this story.

    A plain UNIQUE(run_id) would have capped a run at one audit row and
    silently destroyed refuse enumeration. Three refusals for one run must
    still produce three rows.
    """
    from superagent.pricing.settle_gate import gate_attested_settle

    # Three refusals of one run, each for a different broken link — the point
    # of the enumeration is that the rows can be told apart.
    cases = [
        (make_envelope("run-many", charter_hash=WRONG_CHARTER), ["charter_hash"]),
        (make_envelope("run-many", tamper=True), ["steps_root"]),
        (make_envelope("run-many", agent_dids=["did:emerge:foo"]), ["agent_did"]),
    ]

    for envelope, expected in cases:
        mock_lookup["envelope"] = envelope
        result = await gate_attested_settle(
            run_id="run-many", expected_charter_hash=CHARTER, db=gate_db
        )
        assert result["outcome"] == "refused"
        assert result["failed_checks"] == expected

    rows = [r for r in gate_db.attestedsettlement.rows if r.run_id == "run-many"]
    assert len(rows) == 3
    assert all(r.settled_run_id is None for r in rows)
    # Contents, not just the count: a row that named the wrong check would
    # leave the auditor with three rows and no idea which link broke.
    assert [_failed_checks(r) for r in rows] == [
        ["charter_hash"],
        ["steps_root"],
        ["agent_did"],
    ]


def test_unique_violation_detection_recognises_the_prisma_contract() -> None:
    """`P2002` -> UniqueViolationError is the mapping in prisma/engine/utils.py.

    Detection is layered, so assert each layer independently; a miss degrades
    to `audit_write_error`, which still refuses and still writes no credit.
    """
    from superagent.pricing.settle_gate import _is_unique_violation

    assert _is_unique_violation(FakeUniqueViolation("boom")) is True

    class _ByMessage(Exception):
        pass

    assert (
        _is_unique_violation(
            _ByMessage('duplicate key value violates unique constraint "x_key"')
        )
        is True
    )
    assert _is_unique_violation(RuntimeError("connection reset by peer")) is False
    assert _is_unique_violation(TimeoutError()) is False


# ── Story 2.4: claim on ``db``, refusals on ``audit_db``, call_id on both ────


async def test_refusals_are_written_to_audit_db_not_db(gate_db, mock_lookup) -> None:
    """A refusal must land on the client that is NOT the settle transaction."""
    from superagent.pricing.settle_gate import gate_attested_settle

    audit_db = FakeGateDB()
    mock_lookup["envelope"] = None
    result = await gate_attested_settle(
        run_id="run-audit-split",
        expected_charter_hash=CHARTER,
        db=gate_db,
        audit_db=audit_db,
        call_id="call-9",
    )
    assert result["outcome"] == "refused"
    assert gate_db.attestedsettlement.rows == []
    _assert_refusal_audited(audit_db, "run-audit-split", ["missing_attestation"])
    assert audit_db.attestedsettlement.rows[0].call_id == "call-9"


async def test_settled_claim_is_written_to_db_and_carries_call_id(
    make_envelope, gate_db, mock_lookup
) -> None:
    from superagent.pricing.settle_gate import gate_attested_settle

    audit_db = FakeGateDB()
    mock_lookup["envelope"] = make_envelope("run-claim-call")
    result = await gate_attested_settle(
        run_id="run-claim-call",
        expected_charter_hash=CHARTER,
        db=gate_db,
        audit_db=audit_db,
        call_id="call-10",
    )
    assert result["outcome"] == "settled"
    assert audit_db.attestedsettlement.rows == []
    (row,) = gate_db.attestedsettlement.rows
    assert row.outcome == "settled"
    assert row.settled_run_id == "run-claim-call"
    assert row.call_id == "call-10"


async def test_audit_db_defaults_to_db_and_call_id_to_none(
    gate_db, mock_lookup
) -> None:
    """Direct callers that pass one client keep today's behaviour."""
    from superagent.pricing.settle_gate import gate_attested_settle

    mock_lookup["envelope"] = None
    await gate_attested_settle(
        run_id="run-default", expected_charter_hash=CHARTER, db=gate_db
    )
    (row,) = gate_db.attestedsettlement.rows
    assert row.outcome == "refused"
    assert row.call_id is None


def test_credit_write_error_is_in_the_vocabulary_and_outside_verifier_order() -> None:
    from superagent.pricing import settle_gate

    assert settle_gate.CHECK_CREDIT_WRITE == "credit_write_error"
    assert settle_gate.CHECK_CREDIT_WRITE not in settle_gate._VERIFIER_CHECK_ORDER
    assert settle_gate.CHECK_AGENT_DID == "agent_did"
    assert settle_gate.CHECK_AGENT_DID not in settle_gate._VERIFIER_CHECK_ORDER
    assert settle_gate.CHECK_VERDICT_FAIL == "verdict_fail"
    assert settle_gate.CHECK_VERDICT_FAIL not in settle_gate._VERIFIER_CHECK_ORDER


# ── Story 2.4: the revenue split is validated at boot ───────────────────────


def _kya_settings() -> SimpleNamespace:
    return SimpleNamespace(
        settlement_require_attestation=True,
        run_attestation_enabled=True,
        run_attestation_charter_hash=CHARTER,
    )


@pytest.mark.parametrize(
    ("coordinator", "validator", "fragment"),
    [
        ("5%", "0", "COORDINATOR_SHARE_BPS"),
        ("", "0", "COORDINATOR_SHARE_BPS"),
        ("0", "2.5", "VALIDATOR_SHARE_BPS"),
        ("-1", "0", "negative"),
        ("6000", "5000", "exceeds"),
    ],
)
def test_validate_gate_config_refuses_an_uncomputable_revenue_split(
    monkeypatch: pytest.MonkeyPatch, coordinator: str, validator: str, fragment: str
) -> None:
    from superagent.pricing.settle_gate import validate_gate_config

    monkeypatch.setenv("COORDINATOR_SHARE_BPS", coordinator)
    monkeypatch.setenv("VALIDATOR_SHARE_BPS", validator)
    with pytest.raises(RuntimeError, match=fragment):
        validate_gate_config(_kya_settings())


def test_validate_gate_config_accepts_a_computable_split(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from superagent.pricing.settle_gate import validate_gate_config

    for coordinator, validator in (("0", "0"), ("500", "250"), (" 1000 ", "9000")):
        monkeypatch.setenv("COORDINATOR_SHARE_BPS", coordinator)
        monkeypatch.setenv("VALIDATOR_SHARE_BPS", validator)
        validate_gate_config(_kya_settings())
    monkeypatch.delenv("COORDINATOR_SHARE_BPS")
    monkeypatch.delenv("VALIDATOR_SHARE_BPS")
    validate_gate_config(_kya_settings())
