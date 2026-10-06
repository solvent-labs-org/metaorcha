"""FR-8 (story 3.2): the verifier says what a receipt does not cover.

The statement is export text: computed from the envelope by whoever reads it,
never stored in the envelope, never emitted by a producer. It is the same for
every producer, so it names nothing a producer-neutral reader cannot know.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from emerge.cli import main
from emerge.run_attestation import (
    _ENVELOPE_FIELDS,
    COVERAGE_STATEMENT,
    coverage_statement,
)

GOLDEN_PATH = (
    Path(__file__).resolve().parents[2]
    / "docs"
    / "spec"
    / "test-vectors"
    / "run-attestation-golden.json"
)


@pytest.fixture()
def golden() -> dict:
    return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))


def _flat(text: str) -> str:
    return " ".join(text.split())


def test_the_statement_names_what_is_not_a_step() -> None:
    text = COVERAGE_STATEMENT.lower()
    assert "model (llm) calls are not steps" in text
    assert "does not show that it did not happen" in text
    for word in ("complete", "full", "verified", "transcript"):
        assert word not in text


def test_coverage_reads_the_steps_of_the_envelope(golden) -> None:
    assert coverage_statement(golden["valid"]) == {
        "statement": COVERAGE_STATEMENT,
        "steps": 2,
        "tools": ["search_docs", "summarize"],
    }


@pytest.mark.parametrize(
    "envelope",
    [None, [], "x", {}, {"steps": "nope"}, {"steps": None}],
)
def test_an_unreadable_envelope_still_gets_the_statement(envelope) -> None:
    assert coverage_statement(envelope) == {
        "statement": COVERAGE_STATEMENT,
        "steps": None,
        "tools": [],
    }


def test_tools_are_distinct_in_first_appearance_order() -> None:
    envelope = {
        "steps": [
            {"tool": "b"},
            {"tool": "did:orcha:agent:x#a"},
            {"tool": "b"},
            "junk",
            {"tool": 7},
            {},
        ]
    }
    assert coverage_statement(envelope)["steps"] == 6
    assert coverage_statement(envelope)["tools"] == ["b", "did:orcha:agent:x#a"]


def test_the_statement_is_not_an_envelope_field() -> None:
    # never stored in the signed bytes: the envelope's closed field set has
    # no coverage member, and producing one would fail the schema check
    assert not any("coverage" in name for name in _ENVELOPE_FIELDS)


def _write(tmp_path: Path, payload: dict) -> str:
    path = tmp_path / "envelope.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


@pytest.mark.parametrize("which", ["valid", "tampered"])
def test_verify_prints_the_coverage_block(golden, tmp_path, capsys, which) -> None:
    main(["verify", _write(tmp_path, golden[which])])
    out = capsys.readouterr().out
    assert "\nCoverage:\n" in out
    assert COVERAGE_STATEMENT in _flat(out)
    assert "tools in steps: search_docs, summarize" in out


def test_verify_json_carries_coverage(golden, tmp_path, capsys) -> None:
    main(["verify", "--json", _write(tmp_path, golden["valid"])])
    result = json.loads(capsys.readouterr().out)
    assert result["coverage"] == coverage_statement(golden["valid"])
