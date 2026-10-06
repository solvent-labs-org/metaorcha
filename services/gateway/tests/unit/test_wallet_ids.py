"""A transfer's action id reaches the wallet API URL only as one plain segment.

The action id comes from the client's request path and is placed in an API URL
that carries the app's credentials, so ``..``, ``?`` and friends are refused
before any credential is read or any request is made.
"""

from __future__ import annotations

import os
import types
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-32-bytes-1234567")
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/1")
os.environ.setdefault("SUPERAGENT_URL", "http://127.0.0.1:8001")
os.environ.setdefault("REGISTRY_URL", "http://127.0.0.1:8003")

from gateway.auth.models import TokenPayload  # noqa: E402
from gateway.dependencies import require_auth  # noqa: E402
from gateway.wallet import privy_client  # noqa: E402
from gateway.wallet import routes as wallet_routes  # noqa: E402

NOT_ONE_SEGMENT = [
    "..",
    ".",
    "a/b",
    "../wallets/w2",
    "a?b=1",
    "a#b",
    "%2e%2e",
    "",
    "x" * 129,
]


@pytest.mark.parametrize("value", ["pnorcqhbaanmsbbk7u3p0zu9", "act_9-Z", "x" * 128])
def test_a_plain_id_is_accepted(value: str) -> None:
    assert privy_client.checked_id(value) == value


@pytest.mark.parametrize("value", NOT_ONE_SEGMENT)
def test_an_id_that_is_not_one_segment_is_refused(value: str) -> None:
    with pytest.raises(ValueError):
        privy_client.checked_id(value)


class _Response:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return {"status": "succeeded", "transaction_hash": "0xabc"}


class _Client:
    urls: list[str] = []

    async def __aenter__(self) -> _Client:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def get(self, url: str, **_: Any) -> _Response:
        _Client.urls.append(url)
        return _Response()


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    _Client.urls = []
    monkeypatch.setattr(privy_client.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(privy_client, "_creds", lambda: ("app", "secret"))
    return _Client.urls


@pytest.mark.asyncio
async def test_status_polling_builds_the_url_from_plain_ids(api: list[str]) -> None:
    assert await privy_client.get_transfer_status("w1", "act1") == (
        "succeeded",
        "0xabc",
    )
    assert [u.split("/v1/")[1] for u in api] == ["wallets/w1/actions/act1"]


@pytest.mark.asyncio
@pytest.mark.parametrize("action_id", NOT_ONE_SEGMENT)
async def test_status_polling_refuses_before_any_request(
    api: list[str], action_id: str
) -> None:
    with pytest.raises(ValueError):
        await privy_client.get_transfer_status("w1", action_id)
    assert api == []


def test_the_status_route_answers_400_for_an_id_that_is_not_one_segment(
    api: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(wallet_routes, "_MOCK_MODE", False)
    app = FastAPI()
    app.include_router(wallet_routes.router)
    user = types.SimpleNamespace(id="u1", privy_wallet_id="w1")
    app.state.db = types.SimpleNamespace(
        user=types.SimpleNamespace(find_unique=lambda **_: _async(user))
    )
    app.dependency_overrides[require_auth] = lambda: TokenPayload(
        user_id="u1", email="u@example.com", jti="j1"
    )
    client = TestClient(app)
    # "%2E%2E" arrives as the path parameter ".."
    assert client.get("/wallet/transfer/%2E%2E/status").status_code == 400
    assert client.get("/wallet/transfer/a%3Fb/status").status_code == 400
    assert api == []
    ok = client.get("/wallet/transfer/act1/status")
    assert ok.status_code == 200
    assert [u.split("/v1/")[1] for u in api] == ["wallets/w1/actions/act1"]


async def _async(value: Any) -> Any:
    return value
