"""SuperAgent settings tests (Stories 1.2 + 1.3 — charter-hash setting, flag defaults)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from superagent.config import Settings

_REQUIRED = {
    "openrouter_api_key": "sk-test",
    "redis_url": "redis://localhost:6379/0",
    "database_url": "postgresql://postgres:postgres@localhost:5432/orcha",
    "vault_key": "dGVzdC1rZXktMzItYnl0ZXMtZm9yLXRlc3Rpbmch",
    "pnd_service_url": "http://localhost:8010",
}

# Any valid 64-hex value — deliberately NOT the demo-charter fixture hash
# (the fixture pin lives in services/validator/tests/test_charter_fixture.py).
VALID_HASH = "a" * 64


@pytest.fixture(autouse=True)
def _clean_attestation_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Defaults must not be shadowed by ambient env in dev shells or CI."""
    monkeypatch.delenv("RUN_ATTESTATION_ENABLED", raising=False)
    monkeypatch.delenv("RUN_ATTESTATION_CHARTER_HASH", raising=False)
    monkeypatch.delenv("SETTLEMENT_REQUIRE_ATTESTATION", raising=False)


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **{**_REQUIRED, **overrides})


def test_charter_hash_defaults_to_none() -> None:
    assert _settings().run_attestation_charter_hash is None


def test_charter_hash_accepts_64_lower_hex() -> None:
    assert _settings(
        run_attestation_charter_hash=VALID_HASH
    ).run_attestation_charter_hash == (VALID_HASH)


def test_charter_hash_rejects_invalid_values() -> None:
    for bad in ("not-hex", "A" * 64, "b" * 63, "b" * 65):
        with pytest.raises(ValidationError):
            _settings(run_attestation_charter_hash=bad)


def test_charter_hash_empty_or_blank_normalizes_to_none() -> None:
    # The standard "unset" idiom (VAR= in .env / compose) must behave as unset.
    assert (
        _settings(run_attestation_charter_hash="").run_attestation_charter_hash is None
    )
    assert (
        _settings(run_attestation_charter_hash="   ").run_attestation_charter_hash
        is None
    )


def test_charter_hash_strips_surrounding_whitespace() -> None:
    # Surrounding whitespace/newline is normalized away — the sealed value is
    # always clean 64-hex, never a 65-char string (regex "$" newline trap).
    assert (
        _settings(
            run_attestation_charter_hash="a" * 64 + "\n"
        ).run_attestation_charter_hash
        == "a" * 64
    )


def test_charter_hash_rejects_interior_whitespace() -> None:
    with pytest.raises(ValidationError):
        _settings(run_attestation_charter_hash="a" * 32 + "\n" + "b" * 32)


def test_run_attestation_disabled_by_default() -> None:
    # NFR-9 anchor: the flag defaults off. (Observer-install gating and
    # "no rows written" are structural — see Story 1.3 Dev Agent Record;
    # runtime proof is Epic 3's dual-run smoke.)
    assert _settings().run_attestation_enabled is False


def test_settlement_require_attestation_defaults_false() -> None:
    # AD-2: gate flag default off keeps stock OSS settle behaviour.
    assert _settings().settlement_require_attestation is False
