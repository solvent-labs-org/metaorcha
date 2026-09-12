"""Declared citation criteria replace the hardcoded rulebook-rag agent_id rule."""

from __future__ import annotations

import json

from superagent.middleware.criteria import evaluate_declared_criteria
from superagent.middleware.pipeline import _structural_verify

CITATIONS = {"citations_required": True}


def _cited_output() -> str:
    return json.dumps(
        {
            "answer": "Answer composed from seeded rulebook passages.",
            "citations": [
                {
                    "chunk_id": "chunk-1",
                    "source_title": "transaction-monitoring",
                    "excerpt": "Any single M2M transaction with a value of GBP 10,000...",
                }
            ],
            "verified": True,
        }
    )


def test_declared_citations_pass():
    ok, reason = evaluate_declared_criteria(CITATIONS, _cited_output())
    assert ok is True
    assert reason == "ok"


def test_declared_citations_empty_list_fails():
    content = json.dumps({"answer": "guessed", "citations": [], "verified": False})
    ok, reason = evaluate_declared_criteria(CITATIONS, content)
    assert ok is False
    assert reason == "missing citations"


def test_declared_citations_missing_key_fails():
    content = json.dumps({"answer": "no citation structure at all"})
    ok, reason = evaluate_declared_criteria(CITATIONS, content)
    assert ok is False
    assert reason == "missing citations"


def test_declared_citations_non_json_fails():
    ok, reason = evaluate_declared_criteria(
        CITATIONS, "The threshold is GBP 10,000, trust me."
    )
    assert ok is False
    assert reason == "missing citations"


def test_declared_citations_missing_a_field_fails():
    content = json.dumps(
        {
            "answer": "partial citations",
            "citations": [
                {"chunk_id": "chunk-1", "source_title": "agent-registration"}
            ],
        }
    )
    ok, reason = evaluate_declared_criteria(CITATIONS, content)
    assert ok is False
    assert reason == "missing citations"


def test_structural_verify_no_longer_keys_off_agent_id():
    """The hardcoded rulebook-rag substring rule is gone."""
    verified, reason = _structural_verify("plain text answer", False)
    assert verified is True
    assert reason == "ok"


def test_structural_error_and_empty_unchanged():
    assert _structural_verify("Error: boom", False) == (False, "Error: boom")
    assert _structural_verify("", False) == (False, "empty output")


def test_structural_canvas_still_verified():
    verified, reason = _structural_verify("canvas envelope", True)
    assert verified is True
    assert reason == "canvas output verified"


def test_no_declared_criteria_does_not_require_citations():
    ok, reason = evaluate_declared_criteria({}, "plain text answer")
    assert ok is True
    assert reason == "ok"
