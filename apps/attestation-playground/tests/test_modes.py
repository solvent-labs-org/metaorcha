import asyncio

import playground.modes as modes


class _FakeResp:
    def __init__(self, status_code: int):
        self.status_code = status_code


class _UpClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url):
        return _FakeResp(200)


class _DownClient(_UpClient):
    async def get(self, url):
        raise modes.httpx.ConnectError("connection refused")


def test_probe_superagent_up(monkeypatch):
    monkeypatch.setattr(modes.httpx, "AsyncClient", _UpClient)
    assert asyncio.run(modes.probe_superagent("http://localhost:8002")) is True


def test_probe_superagent_down(monkeypatch):
    monkeypatch.setattr(modes.httpx, "AsyncClient", _DownClient)
    assert asyncio.run(modes.probe_superagent("http://localhost:8002")) is False
