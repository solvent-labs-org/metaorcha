"""Receipt-issuing SuperAgent refuses to boot on an ephemeral key."""

from __future__ import annotations

import pytest


def test_startup_refuses_when_attestation_on_and_key_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from validator import signer

    monkeypatch.delenv(signer.PRIVATE_KEY_ENV, raising=False)
    monkeypatch.delenv(signer.ALLOW_EPHEMERAL_KEY_ENV, raising=False)
    signer._reset_signing_key_for_tests()
    with pytest.raises(signer.EphemeralAttestationKeyRefused):
        signer.require_signing_key_for_receipts()


def test_startup_accepts_persistent_seed(monkeypatch: pytest.MonkeyPatch) -> None:
    from validator import signer

    seed = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="
    monkeypatch.setenv(signer.PRIVATE_KEY_ENV, seed)
    monkeypatch.delenv(signer.ALLOW_EPHEMERAL_KEY_ENV, raising=False)
    signer._reset_signing_key_for_tests()
    signer.require_signing_key_for_receipts()
    _, pub_first = signer.get_signing_key()
    signer._reset_signing_key_for_tests()
    _, pub_second = signer.get_signing_key()
    assert pub_first == pub_second
