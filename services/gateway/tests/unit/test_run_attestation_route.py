"""Unit tests for the run-attestation proxy — bytes through, 404 passthrough."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-32-bytes-1234567")
os.environ.setdefault("JWT_ALGORITHM", "HS256")
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/1")
os.environ.setdefault("SUPERAGENT_URL", "http://127.0.0.1:8001")
os.environ.setdefault("REGISTRY_URL", "http://127.0.0.1:8003")

_REPO = Path(__file__).resolve().parents[4]
_SDK_SRC = _REPO / "sdk" / "src"
if str(_SDK_SRC) not in sys.path:
    sys.path.insert(0, str(_SDK_SRC))
from emerge.run_attestation import verify_run_attestation
_GOLDEN = _REPO / "docs" / "spec" / "test-vectors" / "run-attestation-golden.json"
_ENVELOPE_BYTES = (
    b'{"format":"orcha.run-attestation/v1","run_id":"run-bytes","signature":"x"}'
)


@pytest_asyncio.fixture
async def client_with_mocks():
    from gateway.auth.jwt import create_access_token
    from gateway.main import app

    redis = AsyncMock()
    redis.sismember = AsyncMock(return_value=False)

    app.state.redis = redis
    app.state.superagent = AsyncMock()

    token, _ = create_access_token(user_id="user-001", email="test@example.com")
    headers = {"Authorization": f"Bearer {token}"}

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac, app.state.superagent, headers


def _sa_bytes_response(
    status_code: int,
    body: bytes,
    extra_headers: dict[str, str] | None = None,
) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.content = body
    resp.headers = {
        "content-type": "application/json",
        "content-disposition": 'attachment; filename="run-bytes.json"',
        "cache-control": "no-store",
        **(extra_headers or {}),
    }
    resp.raise_for_status = MagicMock()
    return resp


async def test_attestation_proxy_forwards_bytes_untouched(client_with_mocks):
    ac, sa, headers = client_with_mocks
    sa.get = AsyncMock(return_value=_sa_bytes_response(200, _ENVELOPE_BYTES))

    resp = await ac.get("/api/v1/runs/run-bytes/attestation", headers=headers)

    assert resp.status_code == 200
    assert resp.content == _ENVELOPE_BYTES
    assert (
        resp.headers["content-disposition"] == 'attachment; filename="run-bytes.json"'
    )
    assert resp.headers["cache-control"] == "no-store"
    sa.get.assert_awaited_once_with(
        "/runs/run-bytes/attestation", params={"user_id": "user-001"}
    )


async def test_attestation_proxy_superagent_404_maps_to_404(client_with_mocks):
    ac, sa, headers = client_with_mocks
    sa.get = AsyncMock(
        return_value=_sa_bytes_response(404, b'{"detail":"Attestation not found"}')
    )

    resp = await ac.get("/api/v1/runs/missing/attestation", headers=headers)

    assert resp.status_code == 404


async def test_attestation_proxy_requires_auth(client_with_mocks):
    ac, _, _ = client_with_mocks
    resp = await ac.get("/api/v1/runs/run-bytes/attestation")
    assert resp.status_code in (401, 403)


async def test_attestation_proxy_golden_bytes_verify(client_with_mocks):
    """Downloaded bytes are the golden envelope and still verify offline."""
    golden = json.loads(_GOLDEN.read_text())["valid"]
    body = json.dumps(golden, separators=(",", ":")).encode()
    ac, sa, headers = client_with_mocks
    sa.get = AsyncMock(
        return_value=_sa_bytes_response(
            200,
            body,
            extra_headers={
                "content-disposition": 'attachment; filename="run-9c2e-example.json"',
            },
        )
    )

    resp = await ac.get("/api/v1/runs/run-9c2e-example/attestation", headers=headers)

    assert resp.status_code == 200
    assert resp.content == body
    verdict = verify_run_attestation(json.loads(resp.content))
    assert verdict.valid is True
