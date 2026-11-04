"""Unit tests for JWT helpers."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-32-bytes-1234567")
os.environ.setdefault("JWT_ALGORITHM", "HS256")
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/1")
os.environ.setdefault("SUPERAGENT_URL", "http://127.0.0.1:8001")
os.environ.setdefault("REGISTRY_URL", "http://127.0.0.1:8003")

import base64
import json
import time
import uuid
from datetime import UTC, datetime, timedelta

import jwt

from gateway.auth.jwt import (
    create_access_token,
    create_refresh_token,
    decode_access_token,
)
from gateway.config import settings


def test_access_token_round_trip():
    token, jti = create_access_token(user_id="user-123", email="user@example.com")
    payload = decode_access_token(token)
    assert payload.user_id == "user-123"
    assert payload.email == "user@example.com"
    assert payload.jti == jti


def test_access_token_jti_unique():
    _, jti1 = create_access_token("u1", "a@b.com")
    _, jti2 = create_access_token("u1", "a@b.com")
    assert jti1 != jti2


def test_tampered_token_raises():
    token, _ = create_access_token("user-1", "x@x.com")
    tampered = token[:-4] + "XXXX"
    with pytest.raises(ValueError, match="Invalid token"):
        decode_access_token(tampered)


def test_wrong_type_token_raises():
    """A token with type != 'access' should be rejected."""
    payload = {
        "sub": "user-1",
        "email": "x@x.com",
        "jti": str(uuid.uuid4()),
        "iat": datetime.now(UTC),
        "exp": datetime.now(UTC) + timedelta(days=1),
        "type": "refresh",  # wrong type
    }
    token = jwt.encode(
        payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm
    )
    with pytest.raises(ValueError, match="Not an access token"):
        decode_access_token(token)


def test_refresh_token_raw_and_hash_differ():
    raw, token_hash = create_refresh_token()
    assert raw != token_hash
    assert len(token_hash) == 64  # sha256 hex digest


def test_refresh_tokens_are_unique():
    raw1, hash1 = create_refresh_token()
    raw2, hash2 = create_refresh_token()
    assert raw1 != raw2
    assert hash1 != hash2


# --- parity with the python-jose Gateway --------------------------------------
# The Gateway signed and verified with python-jose until 2026-10 (no fixed
# release for GHSA-3qf3-8w2g-rqmx). These pin that the pyjwt swap keeps every
# rejection, and keeps verifying tokens python-jose already issued.


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _claims(**over) -> dict:
    now = int(time.time())
    claims = {
        "sub": "user-1",
        "email": "x@x.com",
        "jti": str(uuid.uuid4()),
        "iat": now,
        "exp": now + 3600,
        "type": "access",
    }
    claims.update(over)
    return claims


def test_expired_token_raises():
    token, _ = create_access_token("user-1", "x@x.com", expire_minutes=-1)
    with pytest.raises(ValueError, match="Invalid token"):
        decode_access_token(token)


def test_payload_changed_under_the_original_signature_raises():
    token, _ = create_access_token("user-1", "x@x.com")
    header, _payload, signature = token.split(".")
    forged = _b64(json.dumps(_claims(sub="someone-else")).encode())
    with pytest.raises(ValueError, match="Invalid token"):
        decode_access_token(f"{header}.{forged}.{signature}")


def test_alg_none_token_raises():
    header = _b64(json.dumps({"alg": "none", "typ": "JWT"}).encode())
    payload = _b64(json.dumps(_claims()).encode())
    with pytest.raises(ValueError, match="Invalid token"):
        decode_access_token(f"{header}.{payload}.")


@pytest.mark.filterwarnings("ignore::jwt.warnings.InsecureKeyLengthWarning")
def test_other_hmac_algorithm_with_the_right_secret_raises():
    # verification is pinned to settings.jwt_algorithm (HS256), not to the header
    token = jwt.encode(_claims(), settings.jwt_secret_key, algorithm="HS512")
    with pytest.raises(ValueError, match="Invalid token"):
        decode_access_token(token)


def test_wrong_secret_raises():
    token = jwt.encode(
        _claims(), "another-secret-key-32-bytes-12345", algorithm="HS256"
    )
    with pytest.raises(ValueError, match="Invalid token"):
        decode_access_token(token)


def test_issued_in_the_future_raises():
    # Stricter than python-jose, which only required iat to be an integer: pyjwt
    # rejects an iat ahead of the verifier's clock (no leeway). One Gateway mints
    # and verifies on one clock, and iat is floored to the second, so its own
    # tokens are never ahead; replicas with skewed clocks would reject a fresh
    # token for the length of the skew.
    now = int(time.time())
    token = jwt.encode(
        _claims(iat=now + 120, exp=now + 3600),
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )
    with pytest.raises(ValueError, match="Invalid token"):
        decode_access_token(token)


def test_guest_token_round_trip_with_a_cuid_subject():
    # the subject is the Prisma user id (String @default(cuid()))
    token, jti = create_access_token(
        "cm1abcdefghijklmnopqrstuv", "guest@example.com", guest=True
    )
    payload = decode_access_token(token)
    assert payload.user_id == "cm1abcdefghijklmnopqrstuv"
    assert payload.jti == jti
    assert payload.is_guest is True


# Minted 2026-10-06 by the python-jose Gateway itself: fd5cd3b's
# create_access_token under python-jose 3.5.0, HS256, secret
# "test-secret-key-32-bytes-1234567", expire_minutes = 75 years. Access tokens
# live for days (guests 24 h), so tokens issued before the switch are still in
# flight when it deploys and must keep verifying.
_JOSE_SECRET = "test-secret-key-32-bytes-1234567"
_JOSE_ACCESS = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJjbTFvbGRqb3NlMDAwMGV4YW1wbGUw"
    "MDAxIiwiZW1haWwiOiJpbmZsaWdodEBleGFtcGxlLmNvbSIsImp0aSI6ImI2Yzc1NTcwLTZlYjct"
    "NDhkNS04ZGE3LTFjNDM1MTFmZTE1ZSIsImlhdCI6MTc5MTI3NTc1OSwiZXhwIjo0MTU2NDc1NzU5"
    "LCJ0eXBlIjoiYWNjZXNzIn0.D-Ul2cWKDKWLb2asclXetbzCYmm75xcyK1Yv_sC6IhM"
)
_JOSE_GUEST = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJjbTFvbGRqb3NlMDAwMGV4YW1wbGUw"
    "MDAyIiwiZW1haWwiOiJndWVzdEBleGFtcGxlLmNvbSIsImp0aSI6IjQxMTM3ODgyLWVmNTMtNGQ4"
    "Zi05NGZkLTUwYWVhYzM1NDUwYyIsImlhdCI6MTc5MTI3NTc1OSwiZXhwIjo0MTU2NDc1NzU5LCJ0"
    "eXBlIjoiYWNjZXNzIiwiZ3Vlc3QiOnRydWV9.4tsQyKBqhO8Gh83dvT86RZKYDtVKTzcRvOUyJNCQxwc"
)


def test_tokens_minted_by_python_jose_still_verify(monkeypatch):
    monkeypatch.setattr(settings, "jwt_secret_key", _JOSE_SECRET)
    access = decode_access_token(_JOSE_ACCESS)
    assert (access.user_id, access.email, access.jti, access.is_guest) == (
        "cm1oldjose0000example0001",
        "inflight@example.com",
        "b6c75570-6eb7-48d5-8da7-1c43511fe15e",
        False,
    )
    guest = decode_access_token(_JOSE_GUEST)
    assert (guest.user_id, guest.jti, guest.is_guest) == (
        "cm1oldjose0000example0002",
        "41137882-ef53-4d8f-94fd-50aeac35450c",
        True,
    )


def test_a_python_jose_token_with_one_byte_changed_is_rejected(monkeypatch):
    # the fixture test above must not pass on a verifier that skips the signature
    monkeypatch.setattr(settings, "jwt_secret_key", _JOSE_SECRET)
    header, payload, signature = _JOSE_ACCESS.split(".")
    flipped = signature[:-2] + ("A" if signature[-2] != "A" else "B") + signature[-1]
    with pytest.raises(ValueError, match="Invalid token"):
        decode_access_token(f"{header}.{payload}.{flipped}")
