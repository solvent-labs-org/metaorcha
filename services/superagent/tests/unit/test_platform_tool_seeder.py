"""PlatformToolSeeder — bounded retry around the registry cold-start race."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import yaml
from superagent.startup.platform_tool_seeder import (
    _INITIAL_BACKOFF_S,
    _MAX_ATTEMPTS,
    PlatformToolSeeder,
)

_MANIFEST = {"identity": {"id": "did:orcha:system:test-tool"}}


def _make_tools_dir(tmp_path, manifests: dict[str, dict]) -> str:
    manifests_dir = tmp_path / "emerge-tools" / "manifests"
    manifests_dir.mkdir(parents=True)
    for name, manifest in manifests.items():
        (manifests_dir / name).write_text(yaml.dump(manifest))
    return str(tmp_path / "emerge-tools")


def _http_status_error(status_code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://registry/api/v1/agents/register")
    return httpx.HTTPStatusError(
        "err", request=request, response=httpx.Response(status_code, request=request)
    )


async def test_transient_connection_error_is_retried_until_success(tmp_path):
    client = AsyncMock()
    client.upsert_agent.side_effect = [httpx.ConnectError("refused"), None]
    seeder = PlatformToolSeeder(
        client, _make_tools_dir(tmp_path, {"a.yaml": _MANIFEST})
    )

    with patch(
        "superagent.startup.platform_tool_seeder.asyncio.sleep", new=AsyncMock()
    ) as sleep:
        await seeder.seed()

    assert client.upsert_agent.await_count == 2
    sleep.assert_awaited_once_with(_INITIAL_BACKOFF_S)


async def test_client_4xx_is_a_permanent_skip_never_retried(tmp_path):
    client = AsyncMock()
    client.upsert_agent.side_effect = _http_status_error(422)
    seeder = PlatformToolSeeder(
        client, _make_tools_dir(tmp_path, {"a.yaml": _MANIFEST})
    )

    with patch(
        "superagent.startup.platform_tool_seeder.asyncio.sleep", new=AsyncMock()
    ) as sleep:
        await seeder.seed()  # must not raise — boot semantics preserved

    assert client.upsert_agent.await_count == 1
    sleep.assert_not_awaited()


async def test_persistent_outage_gives_up_after_max_attempts_without_raising(tmp_path):
    client = AsyncMock()
    client.upsert_agent.side_effect = httpx.ConnectError("refused")
    seeder = PlatformToolSeeder(
        client, _make_tools_dir(tmp_path, {"a.yaml": _MANIFEST})
    )

    with patch(
        "superagent.startup.platform_tool_seeder.asyncio.sleep", new=AsyncMock()
    ) as sleep:
        await seeder.seed()

    assert client.upsert_agent.await_count == _MAX_ATTEMPTS
    assert sleep.await_count == _MAX_ATTEMPTS - 1


async def test_5xx_is_treated_as_transient(tmp_path):
    client = AsyncMock()
    client.upsert_agent.side_effect = [_http_status_error(503), None]
    seeder = PlatformToolSeeder(
        client, _make_tools_dir(tmp_path, {"a.yaml": _MANIFEST})
    )

    with patch(
        "superagent.startup.platform_tool_seeder.asyncio.sleep", new=AsyncMock()
    ):
        await seeder.seed()

    assert client.upsert_agent.await_count == 2


async def test_missing_platform_env_key_skips_without_calling_registry(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("TEST_TOOL_API_KEY", raising=False)
    manifest = {
        **_MANIFEST,
        "security": {
            "auth_strategies": [
                {"type": "platform_env", "config": {"env_key": "TEST_TOOL_API_KEY"}}
            ]
        },
    }
    client = AsyncMock()
    seeder = PlatformToolSeeder(client, _make_tools_dir(tmp_path, {"a.yaml": manifest}))

    await seeder.seed()

    client.upsert_agent.assert_not_awaited()
