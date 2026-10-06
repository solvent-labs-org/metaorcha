"""Logs name a token key or a bearer only by digest, never by value.

The token key addresses the OAuth tokens stored for a session, so logs treat it
as a credential.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
import pytest
from src import mcp_client, token_store
from src.token_store import log_ref

KEY = "0b6f0e0a-1c2d-4e5f-8a9b-0c1d2e3f4a5b"
BEARER = "access-token-value-do-not-log"


def test_log_ref_is_a_short_stable_digest() -> None:
    assert log_ref(KEY) == log_ref(KEY)
    assert len(log_ref(KEY)) == 12
    assert int(log_ref(KEY), 16) >= 0
    assert log_ref(KEY) != log_ref(KEY + "x")
    assert KEY[:8] not in log_ref(KEY)


def test_the_token_store_logs_the_digest_not_the_key(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(token_store, "_DB_PATH", str(tmp_path / "tokens.db"))
    caplog.set_level(logging.INFO, logger=token_store.__name__)
    token_store.put_tokens(KEY, access_token=BEARER, refresh_token="r", expires_in=60)
    assert token_store.get_tokens(KEY).access_token == BEARER
    text = caplog.text
    assert "oauth_tokens_put" in text and "oauth_tokens_get" in text
    assert log_ref(KEY) in text
    assert KEY not in text
    assert BEARER not in text


class _Client:
    def __init__(self, **_: Any) -> None:
        pass

    async def __aenter__(self) -> _Client:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def post(self, url: str, **_: Any) -> httpx.Response:
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": 1, "result": {}},
            request=httpx.Request("POST", url),
        )


def test_an_mcp_post_logs_the_bearer_digest_not_its_prefix(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(mcp_client.httpx, "AsyncClient", _Client)
    caplog.set_level(logging.INFO, logger=mcp_client.__name__)
    client = mcp_client.WorkspaceMCPClient("http://127.0.0.1:1")
    asyncio.run(client._post({"method": "ping"}, BEARER, include_session=False))
    assert f"bearer_ref={log_ref(BEARER)}" in caplog.text
    assert BEARER[:12] not in caplog.text
