"""Gateway settings: the connections flag is off until a deployer turns it on.

Story 3.4 (FR-16): the stock Gateway holds no connection — the connect route
refuses — and the flag is read from the environment, not from any ambient
shell value or ``.env`` file beside the service.
"""

from __future__ import annotations

import pytest

from gateway.config import Settings

_REQUIRED = {
    "database_url": "postgresql://test:test@localhost:5432/test",
    "redis_url": "redis://localhost:6379/1",
    "jwt_secret_key": "test-secret-key-32-bytes-1234567",
}


def test_connections_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CONNECTIONS_ENABLED", raising=False)
    assert Settings(_env_file=None, **_REQUIRED).connections_enabled is False


def test_the_flag_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONNECTIONS_ENABLED", "true")
    assert Settings(_env_file=None, **_REQUIRED).connections_enabled is True
