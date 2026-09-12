import asyncio
import json

import playground.real_runner as real_runner
import pytest
from playground.real_runner import RealModeError
from playground.sim_runner import build_sim_envelope


class _FakeConn:
    def __init__(self, row):
        self._row = row

    async def fetchrow(self, query, *args):
        return self._row

    async def close(self):
        pass


def test_fetch_envelope_str_payload(monkeypatch):
    envelope = build_sim_envelope("fetch test")

    async def fake_connect(dsn):
        return _FakeConn({"payload": json.dumps(envelope)})

    monkeypatch.setattr(real_runner.asyncpg, "connect", fake_connect)
    out = asyncio.run(
        real_runner.fetch_envelope("dsn", "sess-1", attempts=1, interval=0)
    )
    assert out["run_id"] == envelope["run_id"]
    assert out["signature"] == envelope["signature"]


def test_fetch_envelope_dict_payload(monkeypatch):
    envelope = build_sim_envelope("fetch test dict")

    async def fake_connect(dsn):
        return _FakeConn({"payload": envelope})

    monkeypatch.setattr(real_runner.asyncpg, "connect", fake_connect)
    out = asyncio.run(
        real_runner.fetch_envelope("dsn", "sess-1", attempts=1, interval=0)
    )
    assert out["signer"]["did"] == envelope["signer"]["did"]


def test_fetch_envelope_exhausts_attempts(monkeypatch):
    async def fake_connect(dsn):
        return _FakeConn(None)

    monkeypatch.setattr(real_runner.asyncpg, "connect", fake_connect)
    with pytest.raises(RealModeError, match="no attestation row"):
        asyncio.run(real_runner.fetch_envelope("dsn", "sess-x", attempts=2, interval=0))


def test_create_session_success(monkeypatch):
    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"session_id": "sess-42"}

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json=None):
            assert url == "/sessions"
            assert json["user_id"] == "playground"
            return _Resp()

    monkeypatch.setattr(real_runner.httpx, "AsyncClient", _Client)
    assert asyncio.run(real_runner.create_session("http://x", "hello")) == "sess-42"


def test_create_session_http_error(monkeypatch):
    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json=None):
            raise real_runner.httpx.ConnectError("down")

    monkeypatch.setattr(real_runner.httpx, "AsyncClient", _Client)
    with pytest.raises(RealModeError, match="failed to create superagent session"):
        asyncio.run(real_runner.create_session("http://x", "hello"))
