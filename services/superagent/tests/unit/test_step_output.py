"""Story 1.4 / AD-16: a step commits to the raw, redacted output.

The pipeline hands the observer the handler's raw result — credentials
resolved for the call replaced by ``[REDACTED:<VAR>]`` — before the
OutputNormalizer builds the 280-character display copy. A dispatch that
raises is recorded as a ``success: false`` step and the error still
propagates; an interrupt is neither.
"""

from __future__ import annotations

import builtins
from unittest.mock import AsyncMock, patch

import pytest
from langgraph.errors import GraphInterrupt
from superagent.middleware.observers import (
    NoOpObserver,
    StepResult,
    set_observer,
)
from superagent.middleware.pipeline import ExecutionMiddleware
from superagent.middleware.step_events import step_result_payload
from superagent.middleware.step_output import call_credentials, step_output_preimage

SECRET = "ghp_exampleTokenValue123"


class Recorder:
    def __init__(self) -> None:
        self.records: list[StepResult] = []

    async def on_step_complete(self, record: StepResult) -> None:
        self.records.append(record)


@pytest.fixture
def recorder():
    rec = Recorder()
    set_observer(rec)
    yield rec
    set_observer(NoOpObserver())


# -- call_credentials -------------------------------------------------------


def test_auth_headers_and_their_bare_token_are_credentials() -> None:
    pairs = call_credentials({}, None, {"Authorization": f"Bearer {SECRET}"})
    assert ("Authorization", f"Bearer {SECRET}") in pairs
    assert ("Authorization", SECRET) in pairs


def test_placeholder_env_values_are_credentials_named_by_the_placeholder() -> None:
    manifest = {
        "transport": {
            "type": "stdio",
            "env": {
                "GITHUB_TOKEN": "${GITHUB_TOKEN}",
                "DB_URL": "postgres://app:${DB_PASSWORD}@db:5432/app",
                "PAIR": "${USER_A}:${USER_B}",
                "LOG_LEVEL": "info",
            },
        }
    }
    resolved = {
        "GITHUB_TOKEN": SECRET,
        "DB_URL": "postgres://app:hunter22@db:5432/app",
        "PAIR": "alice:bob",
        "LOG_LEVEL": "info",
    }
    pairs = call_credentials(manifest, resolved, {})
    assert ("GITHUB_TOKEN", SECRET) in pairs
    # One placeholder inside literal text: the exact secret, not the URL.
    assert ("DB_PASSWORD", "hunter22") in pairs
    # Several placeholders: the split is ambiguous, so the whole value.
    assert ("PAIR", "alice:bob") in pairs
    # A literal value is configuration, not a credential.
    assert all(secret != "info" for _, secret in pairs)


def test_platform_env_values_are_credentials() -> None:
    manifest = {
        "transport": {"env": {}},
        "security": {
            "auth_strategies": [
                {"type": "platform_env", "config": {"env_key": "SEARCH_API_KEY"}}
            ]
        },
    }
    pairs = call_credentials(manifest, {"SEARCH_API_KEY": "sk-platform-1"}, {})
    assert pairs == [("SEARCH_API_KEY", "sk-platform-1")]


def test_call_credentials_never_raises_on_malformed_input() -> None:
    assert call_credentials(None, None, None) == []
    assert call_credentials({"transport": "x"}, {"K": 1}, {"H": None}) == []
    assert (
        call_credentials({"security": {"auth_strategies": "x"}}, {"K": "v"}, []) == []
    )


# -- step_output_preimage ---------------------------------------------------


def test_preimage_is_the_raw_result_with_credentials_redacted() -> None:
    raw = {"echo": f"used {SECRET}", "rows": [1, 2]}
    out = step_output_preimage(raw, [("GITHUB_TOKEN", SECRET)])
    assert out == {"echo": "used [REDACTED:GITHUB_TOKEN]", "rows": [1, 2]}


def test_preimage_is_none_without_the_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    real_import = builtins.__import__

    def no_sdk(name, *args, **kwargs):
        if name == "emerge.preimage":
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_sdk)
    assert step_output_preimage("anything", []) is None


def test_kafka_payload_strips_the_preimage() -> None:
    record = StepResult(
        call_id="c1",
        agent_id="did:orcha:agent:gh",
        capability_id="search_repos",
        protocol="MCP",
        tool_name="gh__search_repos",
        success=True,
        content="ok",
        output_preimage="full raw output",
        args={"q": "x"},
    )
    payload = step_result_payload(record)
    assert "output_preimage" not in payload
    assert "args" not in payload


# -- the pipeline -----------------------------------------------------------


def _preflight(headers: dict[str, str]):
    class FakePreFlightManager:
        def __init__(self, _vault) -> None:
            pass

        async def run(self, **_kwargs):
            return {
                "manifest": {"transport": {}},
                "headers": headers,
                "resolved_env": None,
            }

    return FakePreFlightManager


async def _execute(dispatch: AsyncMock, *, normalized: str = "display copy"):
    with (
        patch(
            "superagent.middleware.pipeline.PreFlightManager",
            _preflight({"Authorization": f"Bearer {SECRET}"}),
        ),
        patch(
            "superagent.middleware.pipeline.InputGuard.validate",
            side_effect=lambda args, _schema: args,
        ),
        patch.object(
            ExecutionMiddleware, "_get_capability_schema", AsyncMock(return_value=None)
        ),
        patch.object(ExecutionMiddleware, "_dispatch", dispatch),
        patch(
            "superagent.middleware.pipeline.OutputNormalizer.normalize",
            AsyncMock(return_value={"content": normalized}),
        ),
        patch("superagent.vault.client.VaultClient"),
    ):
        return await ExecutionMiddleware(
            state={"user_id": "u1", "session_id": "s1"}
        ).execute(
            agent_id="did:orcha:agent:gh",
            capability_id="search_repos",
            protocol="MCP",
            tool_name="gh__search_repos",
            args={"q": "orcha"},
            call_id="call_1",
        )


@pytest.mark.asyncio
async def test_step_carries_the_untruncated_redacted_output(recorder) -> None:
    raw = "x" * 500 + f" token={SECRET}"
    result = await _execute(AsyncMock(return_value=raw), normalized=raw[:280])

    assert result["content"] == raw[:280]  # the display copy is unchanged
    (record,) = recorder.records
    assert record.success is True
    assert record.content == raw[:280]
    assert record.output_preimage == "x" * 500 + " token=[REDACTED:Authorization]"


@pytest.mark.asyncio
async def test_failed_dispatch_is_a_failed_step_and_still_raises(recorder) -> None:
    boom = ConnectionError(f"refused with {SECRET}")
    with pytest.raises(ConnectionError):
        await _execute(AsyncMock(side_effect=boom))

    (record,) = recorder.records
    assert record.success is False
    assert record.call_id == "call_1"
    assert record.content == f"Error: refused with {SECRET}"
    assert record.output_preimage == "Error: refused with [REDACTED:Authorization]"
    assert record.verdict == {"verified": False, "reason": record.content[:120]}


@pytest.mark.asyncio
async def test_an_interrupt_during_dispatch_records_no_step(recorder) -> None:
    with pytest.raises(GraphInterrupt):
        await _execute(AsyncMock(side_effect=GraphInterrupt(())))
    assert recorder.records == []
