"""``emerge.criteria``: the one digest and the one evaluation (story 4.1, AD-20).

The digest case is pinned to the value the platform put in a real SM-2
envelope on 2026-09-21; the step and run rules are the ones the platform's
pipeline and run observer apply, now read from here by both producers.
"""

from __future__ import annotations

import json

import pytest
from emerge.criteria import (
    _EVALUATORS,
    CRITERIA_UNEVALUABLE,
    MISSING_CITATIONS,
    NO_EXIT_CODE,
    NO_STEP_APPLICABLE,
    NO_STEP_EXIT_CODE,
    NOT_APPLICABLE,
    compose_policy_version,
    criteria_digest,
    criteria_text,
    criteria_units,
    evaluate_criteria,
    has_valid_citations,
    parse_exit_code,
    parse_policy_version,
    run_declared_acceptance,
    step_declared_acceptance,
)
from emerge.run_attestation import canonical_json_bytes

# sha256 of the canonical bytes of {"exit_zero": true}; the platform composed
# exactly this into policy_version on the 2026-09-21 SM-2 walk.
EXIT_ZERO_DIGEST = "7ebe884e1812714ae129de1868289cea8317d555b25186ed62e53e20a8cb45c7"
CITED = json.dumps(
    {"citations": [{"chunk_id": "c1", "source_title": "t", "excerpt": "e"}]}
)
DEEP = "[" * 200_000 + "]" * 200_000


# ── The digest ───────────────────────────────────────────────────────────────


def test_the_digest_is_the_platforms_and_is_the_envelopes_canonical_form():
    assert criteria_digest({"exit_zero": True}) == EXIT_ZERO_DIGEST
    # key order and whitespace do not move it; a value does
    assert criteria_digest({"exit_zero": True, "a": 1}) == criteria_digest(
        {"a": 1, "exit_zero": True}
    )
    assert criteria_digest({"exit_zero": True}) != criteria_digest({"exit_zero": False})
    assert canonical_json_bytes({"exit_zero": True}) == b'{"exit_zero":true}'


def test_policy_version_composes_and_parses_both_halves():
    composed = compose_policy_version("run-attestation/1.0", EXIT_ZERO_DIGEST)
    assert composed == f"run-attestation/1.0+criteria:{EXIT_ZERO_DIGEST}"
    assert parse_policy_version(composed) == ("run-attestation/1.0", EXIT_ZERO_DIGEST)
    assert compose_policy_version("run-attestation/1.0", None) == "run-attestation/1.0"
    assert parse_policy_version("run-attestation/1.0") == ("run-attestation/1.0", None)
    # a malformed suffix is not a digest, and never silently becomes one
    assert parse_policy_version("p+criteria:nothex") == ("p+criteria:nothex", None)
    with pytest.raises(ValueError, match="64 lowercase hex"):
        compose_policy_version("p", "abc")


# ── Reading a step ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ('{"exit_code": 0}', 0),
        ('{"returncode": 3}', 3),
        ('{"exit": "2"}', 2),
        ('{"exit_code": " 0 "}', 0),
        ('{"exit_code": "-1"}', -1),
        ('{"exit_code": true}', None),  # bool is not an exit code
        ('{"exit_code": 1.0}', None),
        ('{"exit_code": "--1"}', None),  # int() would raise; never reached
        ('{"exit_code": "\\u00b2"}', None),  # isdigit() says yes; we say no
        ('{"stdout": "hi"}', None),
        ("not json", None),
        ("[1]", None),
        (DEEP, None),  # RecursionError inside the decoder
    ],
)
def test_parse_exit_code_reads_decimal_integers_and_never_raises(content, expected):
    assert parse_exit_code(content) == expected


def test_criteria_units_reads_blocks_and_the_mcp_wrapper_one_at_a_time():
    assert criteria_units(None) == []
    assert criteria_units("plain") == ["plain"]
    assert criteria_units({"exit_code": 1}) == ['{"exit_code": 1}']
    blocks = [{"type": "text", "text": "3 failed"}, {"type": "text", "text": "{}"}]
    assert criteria_units(blocks) == ["3 failed", "{}"]
    wrapper = {"content": blocks, "isError": True}
    assert criteria_units(wrapper) == ["3 failed", "{}"]
    # an agent's own document that happens to carry a "content" list is not
    # the transport's wrapper: it is read whole
    own = {"content": blocks, "citations": []}
    assert criteria_units(own) == [json.dumps(own, default=str)]
    assert criteria_text(blocks) == "3 failed\n{}"


def test_has_valid_citations_needs_every_field_and_never_raises():
    assert has_valid_citations(CITED)
    assert not has_valid_citations('{"citations": []}')
    assert not has_valid_citations('{"citations": [{"chunk_id": "c1"}]}')
    assert not has_valid_citations("not json")
    assert not has_valid_citations(DEEP)


# ── The step rule ────────────────────────────────────────────────────────────


def test_exit_zero_is_n_a_without_a_code_fails_on_any_nonzero_block():
    assert evaluate_criteria({"exit_zero": True}, {"stdout": "read a file"}) == {
        "exit_zero": {"result": NOT_APPLICABLE, "detail": NO_EXIT_CODE}
    }
    assert evaluate_criteria({"exit_zero": True}, {"exit_code": 3})["exit_zero"] == {
        "result": "fail",
        "detail": "nonzero exit: 3",
    }
    assert evaluate_criteria({"exit_zero": True}, {"exit_code": 0})["exit_zero"] == {
        "result": "pass",
        "detail": "ok",
    }
    mixed = [
        {"type": "text", "text": '{"exit_code": 0}'},
        {"type": "text", "text": '{"exit_code": 1}'},
    ]
    assert (
        evaluate_criteria({"exit_zero": True}, mixed)["exit_zero"]["result"] == "fail"
    )


def test_citations_in_any_block_satisfy_the_step():
    blocks = [{"type": "text", "text": "a note"}, {"type": "text", "text": CITED}]
    assert evaluate_criteria({"citations_required": True}, blocks) == {
        "citations_required": {"result": "pass", "detail": "ok"}
    }
    assert evaluate_criteria({"citations_required": True}, "a note") == {
        "citations_required": {"result": "fail", "detail": MISSING_CITATIONS}
    }


class _Unrenderable:
    def __str__(self) -> str:
        raise RuntimeError("no text for you")


def test_unsupported_fails_closed_and_false_is_not_a_declaration():
    per = evaluate_criteria({"exit_zero": False, "bogus": True}, {"exit_code": 0})
    assert "exit_zero" not in per
    assert per["bogus"]["result"] == "fail"


def test_values_that_break_a_parser_are_n_a_never_a_raise():
    # the output is the agent's: "--1" and a 5000-digit "code" are not exit
    # codes (int() would raise on both); deep JSON is no exit code either
    for hostile in ({"exit_code": "--1"}, {"exit_code": "1" * 5000}, DEEP):
        assert evaluate_criteria({"exit_zero": True}, hostile)["exit_zero"] == {
            "result": NOT_APPLICABLE,
            "detail": NO_EXIT_CODE,
        }
    assert parse_exit_code('{"exit_code": "' + "1" * 5000 + '"}') is None
    entry = step_declared_acceptance(
        {"exit_zero": True, "citations_required": True}, DEEP
    )
    assert entry == {
        "result": "fail",
        "detail": MISSING_CITATIONS,
        "criteria": {"exit_zero": NOT_APPLICABLE, "citations_required": "fail"},
    }


def test_an_output_that_cannot_be_read_is_a_signed_fail_not_n_a(monkeypatch):
    # a block whose text cannot be rendered hides nothing: the step is
    # unevaluable (fail), not "no exit code" — else a run could pass on it
    unevaluable = {"result": "fail", "detail": CRITERIA_UNEVALUABLE}
    blocks = [{"type": "text", "text": '{"exit_code": 1}'}, _Unrenderable()]
    assert evaluate_criteria({"exit_zero": True}, blocks) == {"exit_zero": unevaluable}
    assert evaluate_criteria({"citations_required": True}, _Unrenderable()) == {
        "citations_required": unevaluable
    }
    assert criteria_units(_Unrenderable()) == []  # the reader itself never raises

    # and an evaluator that faults on readable bytes is signed the same way
    def _boom(_units):
        raise RuntimeError("evaluator fault")

    monkeypatch.setitem(_EVALUATORS, "exit_zero", _boom)
    assert evaluate_criteria({"exit_zero": True}, {"exit_code": 0}) == {
        "exit_zero": unevaluable
    }


def test_step_entry_summarises_fail_then_n_a_then_pass():
    both = {"exit_zero": True, "citations_required": True}
    assert step_declared_acceptance(both, {"exit_code": 0}) == {
        "result": "fail",
        "detail": MISSING_CITATIONS,
        "criteria": {"exit_zero": "pass", "citations_required": "fail"},
    }
    assert step_declared_acceptance({"exit_zero": True}, "just text") == {
        "result": NOT_APPLICABLE,
        "detail": NO_EXIT_CODE,
        "criteria": {"exit_zero": NOT_APPLICABLE},
    }
    assert step_declared_acceptance({}, "anything") == {
        "result": "pass",
        "detail": "ok",
        "criteria": {},
    }


# ── The run rule ─────────────────────────────────────────────────────────────


def _entries(criteria, *outputs):
    return [step_declared_acceptance(criteria, o) for o in outputs]


def test_run_verdict_fails_when_any_applicable_step_failed():
    entries = _entries({"exit_zero": True}, {"exit_code": 3}, {"exit_code": 0})
    assert run_declared_acceptance(entries) == {
        "check": "declared_acceptance",
        "result": "fail",
        "detail": "exit_zero: nonzero exit: 3",
    }


def test_run_verdict_passes_when_one_applied_and_none_failed():
    entries = _entries({"exit_zero": True}, {"stdout": "x"}, {"exit_code": 0})
    assert run_declared_acceptance(entries) == {
        "check": "declared_acceptance",
        "result": "pass",
        "detail": "ok",
    }


def test_run_verdict_fails_when_no_step_was_applicable():
    entries = _entries({"exit_zero": True}, {"stdout": "only reads"})
    assert run_declared_acceptance(entries) == {
        "check": "declared_acceptance",
        "result": "fail",
        "detail": NO_STEP_EXIT_CODE,
    }
    # a criterion n/a on every step (a producer's platform-step entry, or a
    # journal with no steps) is named, not read as exit_zero's wording
    entries = [
        {
            "result": NOT_APPLICABLE,
            "detail": "platform step",
            "criteria": {"citations_required": NOT_APPLICABLE},
        }
    ]
    assert run_declared_acceptance(entries) == {
        "check": "declared_acceptance",
        "result": "fail",
        "detail": f"{NO_STEP_APPLICABLE} citations_required",
    }
    # a failing criterion is named before an unapplied one
    entries = _entries({"citations_required": True, "exit_zero": True}, "")
    assert run_declared_acceptance(entries)["detail"] == (
        f"citations_required: {MISSING_CITATIONS}"
    )


def test_nothing_declared_signs_no_verdict():
    assert run_declared_acceptance([]) is None
    # every key false: not a declaration (3.1's rule), so no verdict either
    assert run_declared_acceptance(
        _entries({"exit_zero": False}, {"exit_code": 0})
    ) is (None)


def test_legacy_entries_without_a_criteria_map_fall_back_to_fail_wins_else_last():
    # a fail anywhere wins, even when a later step passed
    legacy = [{"result": "fail", "detail": "x"}, {"result": "pass", "detail": "ok"}]
    assert run_declared_acceptance(legacy) == {
        "check": "declared_acceptance",
        "result": "fail",
        "detail": "x",
    }
    assert run_declared_acceptance(legacy[1:]) == {
        "check": "declared_acceptance",
        "result": "pass",
        "detail": "ok",
    }
    assert run_declared_acceptance([{"result": "n/a"}]) is None


def test_a_run_mixing_legacy_and_per_criterion_entries_falls_back_and_never_raises():
    # a producer upgraded mid-run: one entry carries the criteria map, one does
    # not; the per-criterion rule needs every entry's map, so the run takes the
    # legacy rule — and reads the entries it has rather than raising on the one
    # it cannot
    mixed = [
        {"result": "pass", "detail": "ok"},
        *_entries({"exit_zero": True}, {"exit_code": 3}),
    ]
    assert run_declared_acceptance(mixed) == {
        "check": "declared_acceptance",
        "result": "fail",
        "detail": "nonzero exit: 3",
    }
