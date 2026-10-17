"""Gateway MessageRequest bounds on acceptance_criteria."""

from __future__ import annotations

import os

import pytest
from pydantic import ValidationError

os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-32-bytes-1234567")
os.environ.setdefault("JWT_ALGORITHM", "HS256")
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/1")
os.environ.setdefault("SUPERAGENT_URL", "http://127.0.0.1:8001")
os.environ.setdefault("REGISTRY_URL", "http://127.0.0.1:8003")

from gateway.sessions.models import MessageRequest


def test_accepts_known_criterion():
    body = MessageRequest(
        message="cite your sources",
        acceptance_criteria={"citations_required": True},
    )
    assert body.acceptance_criteria == {"citations_required": True}


def test_accepts_exit_zero():
    body = MessageRequest(
        message="run the suite",
        acceptance_criteria={"exit_zero": True},
    )
    assert body.acceptance_criteria == {"exit_zero": True}


def test_rejects_unknown_criterion():
    with pytest.raises(ValidationError, match="unsupported criterion: not_a_criterion"):
        MessageRequest(
            message="run the suite",
            acceptance_criteria={"not_a_criterion": True},
        )


def test_rejects_typo_citation_required():
    with pytest.raises(
        ValidationError, match="unsupported criterion: citation_required"
    ):
        MessageRequest(
            message="cite",
            acceptance_criteria={"citation_required": True},
        )


def test_rejects_mixed_known_and_unknown():
    with pytest.raises(ValidationError, match="unsupported criterion: not_a_criterion"):
        MessageRequest(
            message="cite",
            acceptance_criteria={"citations_required": True, "not_a_criterion": True},
        )


def test_rejects_non_boolean_value():
    with pytest.raises(ValidationError, match="must be a boolean"):
        MessageRequest(
            message="cite",
            acceptance_criteria={"citations_required": "yes"},
        )


def test_rejects_too_many_keys():
    too_many = {f"k{i}": True for i in range(9)}
    with pytest.raises(ValidationError, match="at most 8 keys"):
        MessageRequest(message="cite", acceptance_criteria=too_many)
