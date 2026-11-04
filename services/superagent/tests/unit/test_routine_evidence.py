"""Story 2.6: say "attested but unsettled" when no gate ran.

End to end as story 2.3's test (only the database and the connection's reply
are faked): a routine fires through the scheduler, the real pipeline, the
real run-attestation observer and — where installed — the real gate observer;
then the session's exported evidence is read back exactly as
``GET /sessions/{id}/audit`` builds it (``load_settlement`` +
``build_run_audit``).

- gate off, or the gate wrote nothing → the firing and its export say
  "attested but unsettled", and nothing in them implies a settle, a refusal
  or a payment;
- a ledger row the firing has not yet heard of → the export says nothing
  about settlement until the restart sweep reconciles the row, then says
  what the gate decided;
- gate on → "settled" / "refused — <check>", each marked verdict only,
  nothing charged (``call_id`` NULL, AD-12).
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest
from superagent.api.audit import build_run_audit, load_settlement
from superagent.config import settings
from superagent.middleware.observers import (
    CompositeObserver,
    NoOpObserver,
    set_observer,
)
from superagent.pricing import settle_gate
from superagent.pricing.settle_gate import CHECK_VERDICT_FAIL, SettlementGateObserver
from superagent.workflow import firing_rules as rules

from common.utils.src import firing_view

from .test_routine_firing_gate import CITED, CRITERIA, UNCITED, _fire
from .test_routine_scheduler import DB, _scheduler, _seed

# Words that would claim a decision for a run no gate decided (AC1, AC3).
# "unsettled" does not match \bsettled\b; "gate_evaluated" does not match
# \bevaluated\b (the underscore is a word character).
DECISION_WORDS = re.compile(
    r"\b(settled|refused|charged|paid|withheld|evaluated)\b", re.IGNORECASE
)
# Words no export copy may use about a run (commit 1's sweep, extended here
# to every export statement).
CLAIM_WORDS = re.compile(r"\b(done|success|successful|verified)\b", re.IGNORECASE)
MONEY_KEYS = ("verdict_only", "failed_checks", "failed_verdicts", "charged", "outcome")


@pytest.fixture()
def office(monkeypatch):
    """A routine and its database, with the gate observer installed or not.

    ``gate=False`` mirrors ``superagent/main.py`` with
    ``SETTLEMENT_REQUIRE_ATTESTATION`` off: the run-attestation observer is
    the only one installed, and nothing judges the run.
    """
    from validator.run_observer import RunAttestationObserver

    monkeypatch.setattr(settings, "connections_enabled", True)
    real = settle_gate.gate_verdict_only

    def make(*, gate: bool) -> DB:
        monkeypatch.setattr(settings, "settlement_require_attestation", gate)
        db = DB()
        _seed(db, criteria=CRITERIA)
        attestation = RunAttestationObserver(db=db)
        if not gate:
            set_observer(attestation)
            return db
        set_observer(
            CompositeObserver([attestation, SettlementGateObserver(attestation)])
        )

        async def on_this_db(**kwargs: Any) -> Any:
            return await real(db=db, **kwargs)

        monkeypatch.setattr(settle_gate, "gate_verdict_only", on_this_db)
        return db

    yield make
    set_observer(NoOpObserver())


produced: list[str] = []  # every export statement these tests produced


async def _export(db: DB, session_id: str) -> dict[str, Any]:
    """The session's export as the route serves it (``exclude_none``)."""
    evidence = await load_settlement(session_id, db=db)
    audit = build_run_audit(session_id, [], evidence=evidence)
    body = audit.model_dump(exclude_none=True)
    assert not re.search(r"\bOrcha\b", body["note"])
    texts = [body["note"]]
    for block in ("settlement", "firing"):
        if block in body:
            texts += [v for v in body[block].values() if isinstance(v, str)]
    if "settlement" in body:
        produced.append(body["settlement"]["statement"])
    for text in texts:
        assert not CLAIM_WORDS.search(text), text
    return body


def _ledger_for(db: DB, run_id: str) -> list[dict[str, Any]]:
    return [r for r in db.attestedsettlement.rows if r["run_id"] == run_id]


async def _says_attested_but_unsettled(db: DB, firing: dict[str, Any]) -> None:
    """AC1: the pane's words and the export both say no decision is recorded."""
    assert firing["state"] == rules.ATTESTED_UNSETTLED
    run_id = firing["run_id"]
    assert run_id
    ledger_seen = bool(_ledger_for(db, run_id))
    assert ledger_seen is False

    # The pane (the Gateway words the row with the same module).
    assert firing_view.state_label(firing["state"], firing["detail"]) == (
        "attested but unsettled"
    )
    assert firing_view.note(firing["state"], firing["detail"], ledger_seen) == (
        firing_view.UNSETTLED_NOTE
    )

    # The export.
    body = await _export(db, firing["session_id"])
    assert body["run_id"] == run_id
    assert "gate" not in body  # no gate row for the run
    settlement = body["settlement"]
    assert settlement["run_id"] == run_id
    assert settlement["state"] == "attested_unsettled"
    assert settlement["label"] == "attested but unsettled"
    assert settlement["gate_evaluated"] is False
    assert settlement["statement"] == firing_view.STATEMENT_UNSETTLED
    for key in MONEY_KEYS:
        assert key not in settlement, key
    for text in (
        json.dumps(settlement),
        json.dumps(body["firing"]),
        firing_view.UNSETTLED_NOTE,
    ):
        assert not DECISION_WORDS.search(text), text
    assert body["firing"]["state"] == "attested_unsettled"


async def test_with_the_gate_off_the_firing_and_its_export_say_attested_but_unsettled(
    office,
) -> None:
    db = office(gate=False)
    firing, envelope = await _fire(db, CITED)

    assert firing["run_id"] == envelope["run_id"]
    assert db.attestedsettlement.rows == []  # nothing judged the run
    await _says_attested_but_unsettled(db, firing)


async def test_a_run_the_gate_has_not_evaluated_says_attested_but_unsettled(
    office, monkeypatch
) -> None:
    db = office(gate=True)
    entered: list[str] = []

    async def gate_failed(**kwargs: Any) -> Any:
        entered.append(kwargs["run_id"])
        raise RuntimeError("the gate's database went away")

    # The observer swallows the gate's error (settle_gate.py, on_run_complete):
    # the run is sealed and no gate row exists for it.
    monkeypatch.setattr(settle_gate, "gate_verdict_only", gate_failed)
    firing, envelope = await _fire(db, CITED)

    assert entered == [envelope["run_id"]]  # the gate was asked, and failed
    assert db.attestedsettlement.rows == []
    await _says_attested_but_unsettled(db, firing)


@pytest.mark.parametrize(
    ("reply", "state", "detail"),
    [
        (CITED, rules.SETTLED, None),
        (UNCITED, rules.REFUSED, "declared_acceptance"),
    ],
)
async def test_an_unreconciled_row_never_says_no_decision_is_recorded(
    office, monkeypatch, reply, state, detail
) -> None:
    db = office(gate=True)
    table = db.attestedsettlement
    real_find_many = table.find_many
    failed: list[Any] = []

    async def find_many_fails_once(where: dict[str, Any], order: Any = None):
        # Only the firing's read by run fails; the gate's own claim check
        # (by ``settled_run_id``) goes through untouched.
        if not failed and "run_id" in where:
            failed.append(where)
            raise RuntimeError("the ledger read timed out")
        return await real_find_many(where, order)

    # The gate writes its row; the firing's read of it (``_judged``) fails.
    monkeypatch.setattr(table, "find_many", find_many_fails_once)
    firing, envelope = await _fire(db, reply)

    run_id = envelope["run_id"]
    assert failed == [{"run_id": run_id}]
    assert (firing["state"], firing["run_id"]) == (rules.ATTESTED_UNSETTLED, run_id)
    (row,) = _ledger_for(db, run_id)
    assert row["outcome"] == state

    # The pane: a row exists, so the note does not say none is recorded.
    assert firing_view.note(firing["state"], firing["detail"], True) is None
    # The export: the run-keyed gate shows the row; settlement says nothing.
    body = await _export(db, firing["session_id"])
    assert body["run_id"] == run_id
    assert "settlement" not in body
    assert body["gate"]["outcome"] == state
    assert body["gate"]["charged"] is False
    assert body["firing"]["state"] == "attested_unsettled"

    await _scheduler(db).sweep_restart()

    assert (firing["state"], firing["detail"], firing["run_id"]) == (
        state,
        detail,
        run_id,
    )
    body = await _export(db, firing["session_id"])
    settlement = body["settlement"]
    assert settlement["state"] == state
    assert settlement["gate_evaluated"] is True
    assert settlement["verdict_only"] is True
    assert settlement["label"] == (
        f"{firing_view.state_label(state, detail)} — {firing_view.VERDICT_ONLY}"
    )
    assert body["firing"]["state"] == state


async def test_with_the_gate_on_a_passing_firing_exports_settled_verdict_only(
    office,
) -> None:
    db = office(gate=True)
    firing, envelope = await _fire(db, CITED)

    run_id = envelope["run_id"]
    assert firing["state"] == rules.SETTLED
    (row,) = _ledger_for(db, run_id)
    assert row["call_id"] is None  # AD-12: a routine's MCP call charges nothing

    body = await _export(db, firing["session_id"])
    settlement = body["settlement"]
    assert settlement["label"] == "settled — verdict only, nothing charged"
    assert settlement["state"] == "settled"
    assert settlement["gate_evaluated"] is True
    assert settlement["verdict_only"] is True
    assert settlement["failed_checks"] == []
    assert settlement["failed_verdicts"] == []
    assert settlement["statement"] == firing_view.STATEMENT_VERDICT_ONLY
    assert (settlement["checks"], settlement["checks_label"]) == (
        "checked",
        "checked: citations_required",
    )
    assert body["gate"]["outcome"] == "settled"
    assert body["gate"]["charged"] is False
    assert body["firing"]["label"] == "settled"


async def test_with_the_gate_on_a_failing_firing_exports_refused_with_the_named_check(
    office,
) -> None:
    db = office(gate=True)
    firing, envelope = await _fire(db, UNCITED)

    run_id = envelope["run_id"]
    assert firing["state"] == rules.REFUSED
    (row,) = _ledger_for(db, run_id)
    assert row["call_id"] is None

    body = await _export(db, firing["session_id"])
    settlement = body["settlement"]
    assert settlement["label"] == (
        "refused — declared_acceptance — verdict only, nothing charged"
    )
    assert settlement["state"] == "refused"
    assert settlement["verdict_only"] is True
    assert settlement["failed_checks"] == [CHECK_VERDICT_FAIL]
    assert settlement["failed_verdicts"][0]["check"] == "declared_acceptance"
    assert settlement["statement"] == firing_view.STATEMENT_VERDICT_ONLY
    assert body["gate"]["outcome"] == "refused"
    assert body["gate"]["failed_checks"] == [CHECK_VERDICT_FAIL]
    assert body["gate"]["charged"] is False
    assert body["firing"]["label"] == "refused — declared_acceptance"


async def test_every_export_statement_these_firings_produce_claims_nothing(
    office,
) -> None:
    """The sweep runs inside ``_export`` on every export above; this test
    fires each kind of run once more and checks the sweep saw every
    statement a verdict-only routine can produce."""
    produced.clear()
    for gate, reply in ((False, CITED), (True, CITED), (True, UNCITED)):
        db = office(gate=gate)
        firing, _ = await _fire(db, reply)
        await _export(db, firing["session_id"])

    assert set(produced) == {
        firing_view.STATEMENT_UNSETTLED,
        firing_view.STATEMENT_VERDICT_ONLY,
    }
    for statement in produced:
        assert not CLAIM_WORDS.search(statement), statement
    assert not DECISION_WORDS.search(firing_view.STATEMENT_UNSETTLED)
