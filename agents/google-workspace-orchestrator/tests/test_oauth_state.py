"""The OAuth state's session id becomes a path segment of the resume URL.

The state arrives on the callback's query string, so only a single safe
segment may come back out of the parser; anything else resumes nothing.
"""

from __future__ import annotations

import pytest
from src.oauth_routes import _parse_state

SESSION = "0b6f0e0a-1c2d-4e5f-8a9b-0c1d2e3f4a5b"


def test_a_session_and_a_did_are_parsed() -> None:
    state = f"{SESSION}:did:orcha:agent:workspace:nonce123"
    assert _parse_state(state) == (SESSION, "did:orcha:agent:workspace")


@pytest.mark.parametrize(
    "session_id",
    ["..", "../../admin", "a/b", "a?b=1", "a#b", "%2e%2e", "a b", "x" * 129],
)
def test_a_session_id_that_is_not_one_segment_resumes_nothing(session_id: str) -> None:
    assert _parse_state(f"{session_id}:did:orcha:agent:workspace:nonce") == (None, None)


def test_a_state_without_an_agent_resumes_nothing() -> None:
    assert _parse_state(f"{SESSION}::nonce") == (None, None)
    assert _parse_state(f"{SESSION}:nonce") == (None, None)
