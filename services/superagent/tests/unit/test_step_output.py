"""Story 1.4 / AD-16: a step commits to the raw, redacted output.

The pipeline hands the observer the handler's raw result — credentials
resolved for the call replaced by ``[REDACTED:<VAR>]`` — before the
OutputNormalizer builds the 280-character display copy. A dispatch that
raises is recorded as a ``success: false`` step and the error still
propagates; an interrupt is neither.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
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
from superagent.middleware.step_output import (
    call_credentials,
    step_output_preimage,
)

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


def test_the_preimage_is_the_sdks_and_the_sdk_is_not_optional() -> None:
    # AD-20: one implementation. The service declares orcha-sdk, so the
    # pre-image is never a fallback to the display copy for want of an import.
    import importlib.metadata as md
    import tomllib

    from emerge.preimage import output_preimage

    service = Path(__file__).resolve().parents[2]
    deps = tomllib.loads((service / "pyproject.toml").read_text())["project"][
        "dependencies"
    ]
    assert "orcha-sdk" in deps
    assert md.version("orcha-sdk")
    raw = {"b": 1, "a": [{"x": 2}]}
    assert step_output_preimage(raw, []) == output_preimage(raw)
    # the SDK is an editable workspace member (a .pth into sdk/src), so the
    # image must carry sdk/ to resolve the lock and again to import at runtime
    dockerfile = (service / "Dockerfile").read_text()
    assert "COPY sdk ./sdk" in dockerfile
    assert "/app/sdk /app/sdk" in dockerfile


def test_the_preimage_is_total_on_an_agents_value() -> None:
    # the SDK renders what it cannot read as the value's type name; nothing
    # an agent returns can raise out of the step after dispatch
    class Unrenderable:
        def __str__(self) -> str:
            raise RuntimeError("no")

        def __repr__(self) -> str:
            raise RuntimeError("no")

    out = step_output_preimage(Unrenderable(), [])
    assert isinstance(out, str) and out.endswith("Unrenderable")


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


async def _execute(
    dispatch: AsyncMock,
    *,
    normalized: str = "display copy",
    call_id: str = "call_1",
    capability: str = "search_repos",
):
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
            capability_id=capability,
            protocol="MCP",
            tool_name=f"gh__{capability}",
            args={"q": "orcha"},
            call_id=call_id,
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


@pytest.mark.asyncio
async def test_pipeline_to_signed_receipt_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both sides of the seam at once: pipeline → observer → seal → SDK verify."""
    run_observer = pytest.importorskip("validator.run_observer")
    signer = pytest.importorskip("validator.signer")
    from emerge.run_attestation import (
        canonical_json_bytes,
        sha256_hex,
        verify_run_attestation,
    )

    monkeypatch.delenv(signer.PRIVATE_KEY_ENV, raising=False)
    monkeypatch.setenv(signer.ALLOW_EPHEMERAL_KEY_ENV, "1")
    signer._reset_signing_key_for_tests()
    table = SimpleNamespace(
        create=AsyncMock(side_effect=lambda data: SimpleNamespace(id="att-1", **data))
    )
    observer = run_observer.RunAttestationObserver(
        db=SimpleNamespace(attestation=table)
    )
    set_observer(observer)
    try:
        raw = {
            "call_a": "x" * 400 + f" {SECRET}",
            "call_b": {"items": [f"Bearer {SECRET}"]},
            "call_c": b"\x89PNG binary",
        }
        await _execute(
            AsyncMock(return_value=raw["call_a"]),
            call_id="call_a",
            capability="search_repos",
            normalized=raw["call_a"][:280],
        )
        await _execute(
            AsyncMock(return_value=raw["call_b"]),
            call_id="call_b",
            capability="list_issues",
        )
        await _execute(
            AsyncMock(return_value=raw["call_c"]),
            call_id="call_c",
            capability="get_file",
        )
        await observer.on_run_complete("s1")
    finally:
        set_observer(NoOpObserver())
        signer._reset_signing_key_for_tests()

    (envelope,) = observer.envelopes.values()
    assert verify_run_attestation(envelope).valid
    marker = "[REDACTED:Authorization]"
    expected = [
        "x" * 400 + f" {marker}",
        {"items": [marker]},
        sha256_hex(b"\x89PNG binary"),
    ]
    steps = envelope["steps"]
    assert [s["tool"] for s in steps] == [
        "did:orcha:agent:gh#search_repos",
        "did:orcha:agent:gh#list_issues",
        "did:orcha:agent:gh#get_file",
    ]
    assert [s["output_hash"] for s in steps] == [
        sha256_hex(canonical_json_bytes(v)) for v in expected
    ]
    assert SECRET not in canonical_json_bytes(envelope).decode()
