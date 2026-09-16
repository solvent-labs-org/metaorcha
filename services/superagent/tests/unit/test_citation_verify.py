"""Declared citation criteria replace the hardcoded rulebook-rag agent_id rule."""

from __future__ import annotations

import json

import pytest
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


def test_unknown_criterion_fails_closed():
    ok, reason = evaluate_declared_criteria({"not_a_criterion": True}, "no tests ran")
    assert ok is False
    assert reason == "unsupported criterion: not_a_criterion"


def test_typo_citation_required_fails_closed():
    ok, reason = evaluate_declared_criteria({"citation_required": True}, "no citations")
    assert ok is False
    assert reason == "unsupported criterion: citation_required"


def test_mixed_known_and_unknown_criterion_fails_closed():
    ok, reason = evaluate_declared_criteria(
        {"citations_required": True, "not_a_criterion": True},
        _cited_output(),
    )
    assert ok is False
    assert reason == "unsupported criterion: not_a_criterion"


def test_exit_zero_passes_on_zero():
    ok, reason = evaluate_declared_criteria(
        {"exit_zero": True}, json.dumps({"exit_code": 0, "stdout": "ok"})
    )
    assert ok is True
    assert reason == "ok"


def test_exit_zero_fails_on_nonzero():
    ok, reason = evaluate_declared_criteria(
        {"exit_zero": True}, json.dumps({"exit_code": 1, "stdout": "failed"})
    )
    assert ok is False
    assert reason == "nonzero exit: 1"


def test_exit_zero_returncode_alias():
    ok, reason = evaluate_declared_criteria(
        {"exit_zero": True}, json.dumps({"returncode": 0})
    )
    assert ok is True
    assert reason == "ok"


def test_exit_zero_missing_code_fails_closed():
    ok, reason = evaluate_declared_criteria({"exit_zero": True}, "no tests ran")
    assert ok is False
    assert reason == "no exit code in step output"


def test_exit_zero_bool_true_is_not_an_exit_code():
    ok, reason = evaluate_declared_criteria(
        {"exit_zero": True}, json.dumps({"exit_code": True})
    )
    assert ok is False
    assert reason == "no exit code in step output"


def test_mixed_citations_and_exit_zero_needs_both():
    ok, reason = evaluate_declared_criteria(
        {"citations_required": True, "exit_zero": True},
        _cited_output(),
    )
    assert ok is False
    assert reason == "no exit code in step output"


def test_superagent_message_request_rejects_unknown_criterion():
    from pydantic import ValidationError
    from superagent.api.models import MessageRequest

    with pytest.raises(ValidationError, match="unsupported criterion: not_a_criterion"):
        MessageRequest(
            user_id="u",
            message="run the suite",
            acceptance_criteria={"not_a_criterion": True},
        )


def test_superagent_message_request_rejects_mixed_unknown_key():
    from pydantic import ValidationError
    from superagent.api.models import MessageRequest

    with pytest.raises(ValidationError, match="unsupported criterion: not_a_criterion"):
        MessageRequest(
            user_id="u",
            message="run the suite",
            acceptance_criteria={"citations_required": True, "not_a_criterion": True},
        )


def test_superagent_message_request_accepts_known_criterion():
    from superagent.api.models import MessageRequest

    body = MessageRequest(
        user_id="u",
        message="cite your sources",
        acceptance_criteria={"citations_required": True},
    )
    assert body.acceptance_criteria == {"citations_required": True}


def test_superagent_message_request_accepts_exit_zero():
    from superagent.api.models import MessageRequest

    body = MessageRequest(
        user_id="u",
        message="run the suite",
        acceptance_criteria={"exit_zero": True},
    )
    assert body.acceptance_criteria == {"exit_zero": True}
