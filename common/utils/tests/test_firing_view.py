"""Stories 2.5 / 2.6: what a routine firing says, one set of rules for every surface.

The pane, the scheduler and the export word a firing through
``common.utils.src.firing_view``. These tests pin the words themselves:

- AC1: every state has exactly one label from the AD-22 vocabulary, and
  nothing a firing says reads "done", "success" or "verified";
- a refused firing always names its check, and never reads bare;
- AC3: no criteria → "recorded, unchecked"; "checked: …" only where a
  declared criterion's signed verdict compared something; a ledger row with
  ``call_id`` NULL is verdict-only.
"""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any

import pytest

from common.utils.src import firing_view as fv

# AD-22's states and the 2.6 wording, copied here on purpose: never derived
# from the module under test, so a label that drifts fails.
STATES = (
    "scheduled",
    "running",
    "attested_unsettled",
    "settled",
    "refused",
    "paused",
    "skipped",
    "error",
)
VOCABULARY = {
    "scheduled",
    "running",
    "attested but unsettled",
    "settled",
    "refused",
    "paused",
    "skipped",
    "error",
}
FORBIDDEN = re.compile(r"\b(done|success|verified)\b", re.IGNORECASE)

COUNTS_FAIL = {
    "check": "counts_match",
    "result": "fail",
    "detail": "left=11 right=10 key=open_issues left_source=a#x right_source=b#y",
}


def _envelope(*verdicts: dict[str, Any]) -> dict[str, Any]:
    return {"run_id": "r1", "verdicts": list(verdicts)}


def _counts(detail: str, result: str = "fail") -> dict[str, Any]:
    return _envelope({"check": "counts_match", "result": result, "detail": detail})


# Every (criteria, envelope) → (kind, label) case of AC3.
CHECKS_CASES = [
    ({}, _envelope(), ("unchecked", "recorded, unchecked")),
    # declared off: passes save, declares nothing
    ({"citations_required": False}, _envelope(), ("unchecked", "recorded, unchecked")),
    (
        {"counts_match": True},
        _envelope(COUNTS_FAIL),
        ("checked", "checked: counts_match"),
    ),
    (
        {"counts_match": True},
        _counts("left=11 right=11 key=k left_source=a#x", "pass"),
        ("checked", "checked: counts_match"),
    ),
    # signed as fail, though nothing was compared
    (
        {"counts_match": True},
        _counts("no operands declared"),
        ("not_evaluated", "declared, not evaluated: counts_match"),
    ),
    (
        {"counts_match": True},
        _counts("counts_match could not be evaluated"),
        ("not_evaluated", "declared, not evaluated: counts_match"),
    ),
    (
        {"counts_match": True},
        _counts("left=unreadable(never called) right=3 left_source=a#x"),
        ("not_evaluated", "declared, not evaluated: counts_match"),
    ),
    (
        {"counts_match": True},
        _counts("left=3 right=unreadable(no path declared) left_source=a#x"),
        ("not_evaluated", "declared, not evaluated: counts_match"),
    ),
    # the source was called and its output read: the check ran, and failed
    (
        {"counts_match": True},
        _counts("left=unreadable(not JSON) right=3 left_source=a#x"),
        ("checked", "checked: counts_match"),
    ),
    # every dispatch failed: no declared_acceptance verdict was signed
    (
        {"citations_required": True},
        _envelope({"check": "structural_verification", "result": "fail"}),
        ("not_evaluated", "declared, not evaluated: citations_required"),
    ),
    (
        {"citations_required": True},
        _envelope({"check": "declared_acceptance", "result": "fail"}),
        ("checked", "checked: citations_required"),
    ),
    (
        {"citations_required": True, "counts_match": True},
        _envelope(
            {"check": "declared_acceptance", "result": "pass"},
            {
                "check": "counts_match",
                "result": "fail",
                "detail": "no operands declared",
            },
        ),
        ("not_evaluated", "declared, not evaluated: counts_match"),
    ),
    (
        {"citations_required": True, "counts_match": True},
        _envelope({"check": "declared_acceptance", "result": "pass"}, COUNTS_FAIL),
        ("checked", "checked: citations_required, counts_match"),
    ),
    # nothing sealed: nothing to say
    ({"counts_match": True}, None, (None, None)),
    ({}, None, (None, None)),
]


def _says_nothing_forbidden(text: str | None) -> bool:
    return text is None or FORBIDDEN.search(text) is None


# -- AC1: one vocabulary state per firing ------------------------------------


def test_the_module_words_exactly_the_ad22_states() -> None:
    assert set(fv.STATE_LABELS) == set(STATES)


@pytest.mark.parametrize("detail", [None, "x"])
@pytest.mark.parametrize("state", STATES)
def test_every_state_has_one_label_and_none_says_done_success_or_verified(
    state: str, detail: str | None
) -> None:
    label = fv.state_label(state, detail)
    assert label.split(" — ")[0] in VOCABULARY
    assert _says_nothing_forbidden(label)


def test_nothing_a_firing_says_reads_done_success_or_verified() -> None:
    said: list[str | None] = [
        fv.VERDICT_ONLY,
        fv.UNCHECKED,
        fv.UNSETTLED_NOTE,
        fv.REFUSED_UNNAMED,
        fv.STATEMENT_UNSETTLED,
        fv.STATEMENT_VERDICT_ONLY,
        fv.STATEMENT_CHARGED,
        fv.STATEMENT_ROW_UNREAD,
        *fv.STATE_LABELS.values(),
    ]
    for state in STATES:
        for detail in (None, "x"):
            said.append(fv.state_label(state, detail))
            for seen in (True, False, None):
                said.append(fv.note(state, detail, seen))
    for criteria, envelope, _ in CHECKS_CASES:
        said.extend(fv.checks_view(criteria, envelope))
    offending = [s for s in said if not _says_nothing_forbidden(s)]
    assert offending == []


def test_the_forbidden_word_check_catches_what_it_should() -> None:
    # The sweep above is only as good as its pattern.
    for bad in ("Done", "a success", "VERIFIED", "run done."):
        assert not _says_nothing_forbidden(bad)
    for fine in ("successful call", "unverifiable", "abandoned"):
        assert _says_nothing_forbidden(fine)


def test_refused_names_its_check() -> None:
    assert fv.state_label("refused", "counts_match") == "refused — counts_match"
    assert (
        fv.state_label("refused", "counts_match, signature")
        == "refused — counts_match, signature"
    )


@pytest.mark.parametrize("detail", [None, "", "   ", 3])
def test_refused_without_a_check_never_reads_bare(detail: Any) -> None:
    assert fv.state_label("refused", detail) == "refused — check not recorded"


def test_a_state_outside_the_vocabulary_is_shown_as_it_is() -> None:
    # Never mapped to a friendlier word: the row is the truth.
    assert fv.state_label("mystery") == "mystery"
    assert fv.state_label(None) == "None"


def test_only_the_refused_label_carries_the_detail() -> None:
    assert fv.state_label("error", "restart") == "error"
    assert fv.state_label("attested_unsettled", "x") == "attested but unsettled"


# -- naming the failing check -------------------------------------------------


@pytest.mark.parametrize(
    ("checks", "failing", "expected"),
    [
        (["verdict_fail"], ["counts_match"], ["counts_match"]),
        (
            ["verdict_fail", "signature"],
            ["counts_match"],
            ["counts_match", "signature"],
        ),
        (["verdict_fail"], [], ["verdict_fail"]),
        (
            ["verdict_fail"],
            ["structural_verification", "declared_acceptance"],
            ["structural_verification", "declared_acceptance"],
        ),
        # duplicates collapse, order kept
        (
            ["verdict_fail", "verdict_fail", "signature"],
            ["counts_match", "counts_match"],
            ["counts_match", "signature"],
        ),
        (["signature", "signature"], [], ["signature"]),
        # a gate id equal to a verdict name is not repeated
        (["counts_match", "verdict_fail"], ["counts_match"], ["counts_match"]),
        # failing names are used only to replace verdict_fail
        (["signature"], ["counts_match"], ["signature"]),
        ([], ["counts_match"], []),
        # non-string or empty names are ignored
        (["verdict_fail"], ["", None, 3], ["verdict_fail"]),
    ],
)
def test_expand_checks(checks, failing, expected) -> None:
    assert fv.expand_checks(checks, failing) == expected


def test_failing_verdicts_keeps_the_fails_in_envelope_order() -> None:
    envelope = _envelope(
        {"check": "structural_verification", "result": "pass", "detail": "c1: ok"},
        {"check": "declared_acceptance", "result": "fail", "detail": "missing"},
        {"check": "scope_approval", "result": "warn"},
        COUNTS_FAIL,
        {"check": "declared_acceptance", "result": "fail", "detail": "again"},
        {"check": "structural_verification", "result": "fail"},
    )
    assert fv.failing_verdicts(envelope) == [
        {"check": "declared_acceptance", "detail": "missing"},
        {"check": "counts_match", "detail": COUNTS_FAIL["detail"]},
        {"check": "structural_verification"},  # no detail: none invented
    ]


def test_failing_verdicts_reads_a_prisma_json_payload() -> None:
    wrapped = SimpleNamespace(data=_envelope(COUNTS_FAIL))
    assert [v["check"] for v in fv.failing_verdicts(wrapped)] == ["counts_match"]


@pytest.mark.parametrize(
    "envelope",
    [
        None,
        "not a dict",
        [],
        {},
        {"verdicts": None},
        {"verdicts": "fail"},
        {
            "verdicts": [
                None,
                3,
                "x",
                {"result": "fail"},
                {"check": "", "result": "fail"},
            ]
        },
        {"verdicts": [{"check": 7, "result": "fail"}]},
        {"verdicts": [{"check": "counts_match", "result": "FAIL"}]},
    ],
)
def test_failing_verdicts_of_a_malformed_envelope_is_empty(envelope: Any) -> None:
    assert fv.failing_verdicts(envelope) == []


def test_failing_verdicts_drops_a_detail_that_is_not_text() -> None:
    envelope = _envelope({"check": "counts_match", "result": "fail", "detail": 11})
    assert fv.failing_verdicts(envelope) == [{"check": "counts_match"}]


# -- the line under the label ------------------------------------------------


@pytest.mark.parametrize("state", ["error", "skipped", "paused"])
def test_a_note_shows_the_detail_where_the_detail_is_the_reason(state: str) -> None:
    assert fv.note(state, "restart") == "restart"
    assert fv.note(state, None) is None
    assert fv.note(state, "") is None


def test_attested_unsettled_says_so_only_when_the_ledger_was_read_and_empty() -> None:
    assert fv.note("attested_unsettled", None, ledger_seen=False) == fv.UNSETTLED_NOTE
    # a row exists (not yet reconciled): the note would be false
    assert fv.note("attested_unsettled", None, ledger_seen=True) is None
    # the read failed: it proves nothing
    assert fv.note("attested_unsettled", None, ledger_seen=None) is None
    assert fv.note("attested_unsettled", None) is None


@pytest.mark.parametrize("state", ["scheduled", "running", "settled", "refused"])
def test_other_states_carry_no_note(state: str) -> None:
    for seen in (True, False, None):
        assert fv.note(state, "x", seen) is None


def test_the_unsettled_note_never_claims_nothing_was_charged() -> None:
    for text in (fv.UNSETTLED_NOTE, fv.STATEMENT_UNSETTLED):
        assert "charge" not in text.lower()
        assert "settle" in text.lower()


# -- AC3: unchecked, checked, not evaluated -----------------------------------


@pytest.mark.parametrize(("criteria", "envelope", "expected"), CHECKS_CASES)
def test_checks_view(criteria, envelope, expected) -> None:
    assert fv.checks_view(criteria, envelope) == expected


def test_checks_view_reads_prisma_json_wrappers() -> None:
    criteria = SimpleNamespace(data={"counts_match": True})
    envelope = SimpleNamespace(data=_envelope(COUNTS_FAIL))
    assert fv.checks_view(criteria, envelope) == ("checked", "checked: counts_match")


def test_a_criteria_document_that_is_not_an_object_declares_nothing() -> None:
    for criteria in (None, [], "counts_match", {"counts_match": 1}):
        assert fv.declared(criteria) == []
    assert fv.checks_view(None, _envelope()) == ("unchecked", "recorded, unchecked")


def test_declared_is_sorted_and_only_true() -> None:
    criteria = {"counts_match": True, "citations_required": True, "x": False}
    assert fv.declared(criteria) == ["citations_required", "counts_match"]


def test_a_criterion_with_no_known_verdict_is_never_evaluated() -> None:
    envelope = _envelope({"check": "exit_zero", "result": "pass"})
    assert fv.evaluated("exit_zero", envelope) is False
    assert fv.checks_view({"exit_zero": True}, envelope) == (
        "not_evaluated",
        "declared, not evaluated: exit_zero",
    )


def test_one_evaluated_verdict_is_enough_among_several() -> None:
    envelope = _envelope(
        {"check": "counts_match", "result": "fail", "detail": "no operands declared"},
        COUNTS_FAIL,
    )
    assert fv.evaluated("counts_match", envelope) is True


def test_every_criterion_names_the_verdict_that_proves_it() -> None:
    assert fv.VERDICT_FOR_CRITERION == {
        "citations_required": "declared_acceptance",
        "counts_match": "counts_match",
    }


# -- the gate's row ------------------------------------------------------------


def test_gate_kind() -> None:
    assert fv.gate_kind(None) is None
    assert fv.gate_kind(SimpleNamespace(call_id=None)) == "verdict_only"
    assert fv.gate_kind(SimpleNamespace(call_id="c1")) == "charged"
    assert fv.gate_kind({"call_id": None}) == "verdict_only"
    assert fv.gate_kind({"call_id": "c1"}) == "charged"


def test_gate_checks() -> None:
    assert fv.gate_checks(None) == []
    assert fv.gate_checks(SimpleNamespace(failed_checks=["verdict_fail"])) == [
        "verdict_fail"
    ]
    wrapped = SimpleNamespace(failed_checks=SimpleNamespace(data=["a", 3, "b"]))
    assert fv.gate_checks(wrapped) == ["a", "b"]
    assert fv.gate_checks({"failed_checks": "verdict_fail"}) == []


def _rows(*spec: tuple[str, list[str]]) -> list[SimpleNamespace]:
    return [SimpleNamespace(outcome=o, failed_checks=c) for o, c in spec]


@pytest.mark.parametrize(
    ("rows", "expected"),
    [
        ([], None),
        (None, None),
        (_rows(("settled", [])), ("settled", [])),
        (_rows(("refused", ["verdict_fail"])), ("refused", ["verdict_fail"])),
        # a replay of a settled run adds a refusal; the run stays settled
        (_rows(("settled", []), ("refused", ["already_settled"])), ("settled", [])),
        # a claim rolled back with its credit, then settled on retry
        (_rows(("refused", ["credit_write_error"]), ("settled", [])), ("settled", [])),
        # two refusals: the oldest decides
        (
            _rows(("refused", ["verdict_fail"]), ("refused", ["signature"])),
            ("refused", ["verdict_fail"]),
        ),
    ],
)
def test_deciding_row(rows, expected) -> None:
    row = fv.deciding_row(rows)
    if expected is None:
        assert row is None
    else:
        assert (row.outcome, row.failed_checks) == expected


def test_deciding_row_reads_dict_rows() -> None:
    rows = [{"outcome": "refused", "id": 1}, {"outcome": "settled", "id": 2}]
    assert fv.deciding_row(rows) == {"outcome": "settled", "id": 2}


def test_an_untrusted_envelope_gives_no_checks() -> None:
    envelope = {
        "verdicts": [
            {"check": "counts_match", "result": "pass", "detail": "left=1 right=1"}
        ]
    }
    assert fv.checks_view({"counts_match": True}, envelope, untrusted=True) == (
        None,
        None,
    )
    assert fv.checks_view({}, envelope, untrusted=True) == (None, None)
    assert fv.checks_view({"counts_match": True}, envelope)[0] == "checked"


@pytest.mark.parametrize(
    ("checks", "untrusted"),
    [
        (["signature"], True),
        (["verdict_fail", "steps_root"], True),
        (["verify_error"], True),
        (["verdict_fail"], False),
        (["signer_did"], False),  # the platform's own seal: its verdicts stand
        ([], False),
    ],
)
def test_envelope_untrusted(checks: list[str], untrusted: bool) -> None:
    assert (
        fv.envelope_untrusted({"outcome": "refused", "failed_checks": checks})
        is untrusted
    )
    assert fv.envelope_untrusted(None) is False
