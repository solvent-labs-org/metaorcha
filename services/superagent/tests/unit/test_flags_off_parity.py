"""Story 3.4 (FR-16): with every flag off, the stack runs as the stock service.

The claim, narrowed to what a test can hold. With ``RUN_ATTESTATION_ENABLED``,
``SETTLEMENT_REQUIRE_ATTESTATION`` and ``CONNECTIONS_ENABLED`` all off:

- boot installs no receipt or gate observer, and the validator package is
  never imported;
- an agent step goes through the real pre-flight and the real pipeline with
  no scope gate, no deferred charge — the stock per-call settle is
  scheduled — no receipt, and no refusal; an agent registered as a
  connection is refused by name (``connections_disabled``) before any
  request — the one place the flag speaks on this path;
- a platform tool's call does not reach the scope gate;
- a turn's done event is the stock one plus ``model`` (delta 9 below), and
  the session export carries no gate, settlement or firing block;
- the scheduler, reading the real settings, writes nothing to an empty table.

What this does **not** claim: behaviour that moved for every deployment on the
way here, flags or not. Each is attributable to a story and was accepted
there; this file is where the list lives so the parity claim is read with it.

1. A step's output is hashed before any observer sees it (AD-16, story 1.4)
   and resolved credentials are redacted out of the display copy (story
   1.6b).
2. A dispatch that fails is still emitted as a failed step (story 1.4).
3. The vault key under which a session credential is kept moved, the
   run-config key is ``__session_credentials``, and the permanent BYOK key is
   no longer hydrated into graph state (AD-14/AD-15, story 1.6b).
4. Lower-case auth strategy types (``http_bearer``, ``x_api_key``) now resolve
   a registered agent's token (story 1.3a).
5. A manifest carries ``tags`` and ``is_active``; an agent tagged
   ``connection`` is refused with the flag off (stories 1.3, 1.7); the
   per-variable vault delete propagates database errors (story 1.7).
6. The Registry rejects a harvested capability id outside
   ``[A-Za-z0-9_.:-]+`` on every registration (story 1.2). This one no
   configuration reverses.
7. Every session route resolves an office, creating a personal one on first
   use, and the Gateway's connect route resolves the office before it reads
   the flag (story 2.0).
8. The scheduler runs always: a routine row with ``schedule_enabled`` is
   claimed and its firing row written as ``error`` naming the flag, with
   the flags off; every resume stream is followed by a ``record_resume``
   read of the firing table (story 2.2).
9. Every turn that made an LLM call carries ``model`` on its SSE ``done``
   event, on the assistant transcript row's ``tool_inputs`` (previously
   null) and on the checkpointed AIMessage's ``response_metadata``, and the
   chat shows it as a caption (story 3.3).
10. A platform tool's call is emitted on the observer seam as a step, so the
    ledger and CDV observers (their own flags) see rows for it (story 3.1).
11. The export has new fields — ``coverage``, ``models``, run-evidence
    wording — and an unchecked step is no longer counted verified (stories
    2.6, 3.2, 3.3); the mailed summary's copy changed with it (story 3.2).
12. ``exit_zero`` is an accepted criterion in chat (story 3.1).

(The settle gate's retired recheck timer, story 2.7, ran only with the gate
flag on and is not a flags-off change.)
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from superagent.config import settings
from superagent.graph import runner as runner_mod
from superagent.middleware.connections import CONNECTIONS_DISABLED
from superagent.middleware.observers import (
    NoOpObserver,
    emit_run_complete,
    peek_published_run_id,
    set_observer,
)
from superagent.middleware.pipeline import ExecutionMiddleware
from superagent.middleware.preflight import PreFlightError, PreFlightManager
from superagent.nodes.execute_agent_calls import execute_agent_calls_node
from superagent.system_tools.registry import register_all_system_tools
from superagent.workflow import scheduler as scheduler_mod

from .test_criterion_step_coverage import _system_call
from .test_routine_scheduler import DB, _scheduler, _seed, _tick

AGENT = "did:orcha:agent:stock"
SESSION = "sess-34"
FLAGS = (
    "run_attestation_enabled",
    "settlement_require_attestation",
    "connections_enabled",
)
_REPO = Path(__file__).resolve().parents[4]


@pytest.fixture()
def flags_off(monkeypatch: pytest.MonkeyPatch) -> None:
    for flag in FLAGS:
        monkeypatch.setattr(settings, flag, False)
    set_observer(NoOpObserver())
    yield
    set_observer(NoOpObserver())


def _never(name: str) -> MagicMock:
    return MagicMock(side_effect=AssertionError(f"{name} ran with the flags off"))


# ---------------------------------------------------------------------------
# Boot
# ---------------------------------------------------------------------------

# Boot step 5 in a fresh interpreter, with the five observer flags set on
# the settings object itself: neither the shell nor a developer's
# ``services/superagent/.env`` (which pydantic-settings reads at import)
# can turn one on or off behind the probe's back.
_PROBE = """
import json, sys
flags = json.loads(sys.argv[1])
from superagent.config import settings
for name, value in flags.items():
    setattr(settings, name, value)
from superagent.main import build_observers
observers = build_observers()
print(json.dumps({
    "observers": [type(o).__name__ for o in observers],
    "validator_imported": sorted(
        m for m in sys.modules if m == "validator" or m.startswith("validator.")
    ),
}))
"""
_OBSERVER_FLAGS = {
    "audit_ledger_enabled": False,
    "cdv_verification_enabled": False,
    "run_attestation_enabled": False,
    "settlement_require_attestation": False,
    "connections_enabled": False,
}


def _boot(**flags: bool) -> dict[str, Any]:
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"ATTESTATION_PRIVATE_KEY_B64", "ATTESTATION_ALLOW_EPHEMERAL_KEY"}
    }
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in (str(_REPO), os.environ.get("PYTHONPATH", "")) if p
    )
    env["ATTESTATION_ALLOW_EPHEMERAL_KEY"] = "1"  # a key for the control only
    out = subprocess.run(
        [sys.executable, "-c", _PROBE, json.dumps({**_OBSERVER_FLAGS, **flags})],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_the_stock_boot_installs_no_observer_and_never_imports_the_validator() -> None:
    assert _boot() == {"observers": [], "validator_imported": []}


def test_the_probe_sees_the_validator_when_a_receipt_is_asked_for() -> None:
    # the control: the same probe reports the import once the flag is on.
    # It fails, never skips, where the validator is not installed — that is
    # the seam-deps CI change missing, which the sealed-run tests need too.
    probe = _boot(run_attestation_enabled=True)
    assert probe["observers"] == ["RunAttestationObserver"]
    assert "validator.run_observer" in probe["validator_imported"]


def test_the_flags_off_process_composes_no_observer(flags_off, monkeypatch) -> None:
    from superagent.main import build_observers

    monkeypatch.setattr(settings, "audit_ledger_enabled", False)
    monkeypatch.setattr(settings, "cdv_verification_enabled", False)
    assert build_observers() == []


# ---------------------------------------------------------------------------
# A turn
# ---------------------------------------------------------------------------

STOCK_MANIFEST = {
    "name": "stock",
    "transport": {"type": "HTTP", "url": "http://127.0.0.1:1/"},
    "security": {"auth_strategies": []},
}
CONNECTION_MANIFEST = {**STOCK_MANIFEST, "name": "conn", "tags": ["connection"]}


async def _stock_step(
    raw: str,
    *,
    base_fee: Decimal,
    settle: AsyncMock,
    manifest: dict[str, Any] = STOCK_MANIFEST,
) -> dict[str, Any]:
    """A charged agent step through the real pre-flight and pipeline."""
    state = {"user_id": "u1", "session_id": SESSION, "_declared_criteria": {}}
    with (
        patch(
            "superagent.middleware.preflight.MANIFEST_CACHE.get_manifest",
            AsyncMock(return_value=dict(manifest)),
        ),
        patch.object(PreFlightManager, "_assert_healthy", AsyncMock()),
        patch(
            "superagent.middleware.preflight._redis_has_grant",
            AsyncMock(return_value=False),
        ),
        patch("superagent.middleware.scope_gate.scope_gate", _never("scope_gate")),
        patch("superagent.pricing.guard.payment_guard", AsyncMock()),
        patch("superagent.pricing.settlement.settle_invocation", settle),
        patch(
            "superagent.pricing.settlement.release_reserve",
            _never("release_reserve"),
        ),
        patch(
            "superagent.middleware.pipeline.InputGuard.validate",
            side_effect=lambda args, _schema: args,
        ),
        patch.object(
            ExecutionMiddleware, "_get_capability_schema", AsyncMock(return_value=None)
        ),
        patch.object(ExecutionMiddleware, "_dispatch", AsyncMock(return_value=raw)),
        patch.object(
            ExecutionMiddleware, "_resolve_base_fee", AsyncMock(return_value=base_fee)
        ),
        patch("superagent.vault.client.VaultClient"),
    ):
        result = await ExecutionMiddleware(state=state).execute(
            agent_id=AGENT,
            capability_id="answer",
            protocol="A2A",
            tool_name="delegate__did_orcha_agent_stock",
            args={"task": "say hi"},
            call_id="c-stock",
            config={"configurable": {}},
        )
        await asyncio.sleep(0)  # let the settle task the pipeline scheduled run
    return result


async def test_a_charged_step_takes_the_stock_settle_path(flags_off) -> None:
    settle = AsyncMock()
    result = await _stock_step("hi there", base_fee=Decimal("0.25"), settle=settle)

    assert result["content"] == "hi there"  # nothing redacted, nothing refused
    assert settle.await_count == 1
    charge = settle.await_args.kwargs
    assert charge["call_id"] == "c-stock"
    assert charge["base_fee"] == Decimal("0.25")
    assert charge["execution_success"] is True

    # the turn ends with nothing sealed
    await emit_run_complete(SESSION)
    assert peek_published_run_id(SESSION) is None


async def test_a_free_step_schedules_no_settlement(flags_off) -> None:
    settle = AsyncMock()
    result = await _stock_step("free", base_fee=Decimal("0"), settle=settle)
    assert result["content"] == "free"
    assert settle.await_count == 0


async def test_a_connection_is_refused_by_name_before_any_request(flags_off) -> None:
    # delta 5: the flag speaks here and nowhere else on the agent path —
    # the refusal names the flag, the gate never runs, nothing is charged
    settle = AsyncMock()
    with pytest.raises(PreFlightError, match=CONNECTIONS_DISABLED[:20]):
        await _stock_step(
            "never",
            base_fee=Decimal("0.25"),
            settle=settle,
            manifest=CONNECTION_MANIFEST,
        )
    assert settle.await_count == 0


async def test_a_platform_tool_call_does_not_reach_the_gate(flags_off) -> None:
    register_all_system_tools()
    with patch("superagent.middleware.scope_gate.scope_gate", _never("scope_gate")):
        updates = await execute_agent_calls_node(
            _system_call(
                "get_datetime", {"user_id": "u1", "session_id": SESSION}, "c-clock"
            ),
            {},
        )
    assert not updates["messages"][0].content.startswith("Error:")


class _Graph:
    """One turn: the orchestrator ran an LLM and recorded the model."""

    async def aget_state(self, _config: Any) -> Any:
        return SimpleNamespace(
            values={"messages": [HumanMessage(content="hi")]}, tasks=[]
        )

    def astream(self, _state: Any, _config: Any, stream_mode: Any = None):
        async def gen():
            yield (
                "values",
                {
                    "messages": [
                        HumanMessage(content="hi"),
                        AIMessage(content="hello"),
                    ],
                    "_turn_model": "local/qwen2.5:3b",
                },
            )

        return gen()


async def test_the_done_event_is_the_stock_one_plus_the_model(
    flags_off, monkeypatch
) -> None:
    # the event the runner emits (not the helper): delta 9 is the only
    # addition, and there is no run_id or attestation_path to fetch
    monkeypatch.setattr(runner_mod, "_merge_graph_config", lambda c: c)
    monkeypatch.setattr(
        runner_mod.SessionRunner, "_persist_transcript", AsyncMock(return_value=None)
    )
    run = runner_mod.SessionRunner(_Graph())
    events = [e async for e in run.run_turn(SESSION, "u", "hi")]
    assert events[-1] == {
        "type": "done",
        "session_id": SESSION,
        "model": "local/qwen2.5:3b",
    }


# ---------------------------------------------------------------------------
# Export and scheduler
# ---------------------------------------------------------------------------


async def test_the_export_of_a_stock_session_has_no_gate_block(flags_off) -> None:
    from superagent.api.audit import build_run_audit, load_settlement

    evidence = await load_settlement(SESSION, db=DB())
    body = build_run_audit(SESSION, [], evidence=evidence).model_dump(exclude_none=True)
    assert not {"gate", "settlement", "firing", "run_id", "models"} & body.keys()
    assert body["coverage"]["export"].startswith("No signed receipt was read")


async def test_the_scheduler_writes_nothing_to_an_empty_table(flags_off) -> None:
    db = DB()
    scheduler = _scheduler(db, flags=scheduler_mod._flags)  # the real settings read
    assert scheduler._flags() == (False, False)
    assert await scheduler._tick() == []
    assert db.log == []
    assert db.routinefiring.rows == []


async def test_a_seeded_schedule_is_the_accepted_delta_not_parity(flags_off) -> None:
    # delta 8 in the module docstring: the slot is claimed and its row ends
    # in ``error`` naming the flag — the turn itself never runs
    db = DB()
    _seed(db)
    scheduler = _scheduler(db, flags=scheduler_mod._flags)
    await _tick(scheduler)  # claims the slot and drains the started tasks
    (row,) = db.routinefiring.rows
    assert row["state"] == "error"
    assert row["detail"].startswith("connections_disabled")
    assert "runner.run_turn" not in db.log
