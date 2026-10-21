"""Run one job on one engine and seal its receipt, whatever happens.

The runner owns the run: it writes the policy, opens the control socket,
drives the engine, decides every approval the gate hook or the switchboard
asks for, and records every step. The receipt is sealed on success, on an
engine error and on cancellation alike, so a run that went wrong still has
a record of what it did.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from emerge.journal import utc_now

from metabot.approvals import Approver
from metabot.connections import ConnectionStore
from metabot.control import ControlServer, socket_path
from metabot.decisions import Decider
from metabot.engines import get_engine
from metabot.engines.base import CliEngine, EngineJob, run_cli
from metabot.events import ErrorEvent, Event, Final, Notice, Started, ToolCall, Usage
from metabot.gate import Policy
from metabot.paths import claim_run_dir, metabot_home, new_run_id, safe_id, write_private
from metabot.plugins import plugin_skills
from metabot.receipts import (
    approval_verdict,
    declined_and_failed,
    ensure_key,
    model_verdict,
    project_copy,
    seal_and_store,
)
from metabot.steps import MAY_HAVE_RUN, NO_RESULT, StepRecorder
from metabot.switchboard import Switchboard, write_skills

STEP_FIELDS = ("call_id", "tool", "args", "output", "success", "latency_ms")
MAX_NOTICES = 20
NOTICE_LIMIT = 500


@dataclass
class RunResult:
    run_id: str
    receipt: Path
    final_text: str
    ok: bool
    steps: int
    error: str | None
    session_id: str | None
    tokens: int = 0
    limit: str | None = None
    project_receipt: Path | None = None  # the copy in the project, when one was asked for
    project_error: str | None = None  # why that copy is missing
    declined: int = 0  # calls the gate declined; the answer may say they ran
    failed: int = 0  # other calls that did not succeed


@dataclass(frozen=True)
class Limits:
    """A run's hard limits. A run that passes one ends, with the limit as its error."""

    max_tool_calls: int | None = None
    max_tokens: int | None = None


def _count(usage: Mapping[str, Any], key: str) -> int:
    value = usage.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def usage_tokens(data: Mapping[str, Any]) -> int:
    """The tokens one usage report counts, the way Code Desks' meter counts them.

    Input and output, with cache writes and cache reads. The ``codex`` and
    ``openai`` engines already count cached input inside their input.
    """
    usage = data.get("usage", data) if isinstance(data, Mapping) else None
    if not isinstance(usage, Mapping):
        return 0

    def n(*keys: str) -> int:
        return sum(_count(usage, key) for key in keys)

    if "prompt_tokens" in usage or "completion_tokens" in usage:
        return n("prompt_tokens", "completion_tokens")
    if "inputTokens" in usage or "outputTokens" in usage:
        return n("inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens")
    if "cached_input_tokens" in usage:
        return n("input_tokens", "cache_write_input_tokens", "output_tokens")
    return n(
        "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens"
    )


class RunCancelled(asyncio.CancelledError):
    """The run was cancelled; its receipt was still sealed."""

    def __init__(self, result: RunResult) -> None:
        super().__init__("run cancelled")
        self.result = result


@dataclass
class _Run:
    engine: Any  # a CliEngine, or the in-process OpenAIEngine
    decider: Decider
    recorder: StepRecorder
    errors: list[str] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)
    final: Final | None = None
    model: str | None = None  # what the engine says it ran; else what was asked for
    model_requested: str | None = None
    session_id: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    tokens: int = 0  # summed over every usage report
    tool_calls: int = 0
    limit: str | None = None

    def breach(self, limits: Limits | None) -> str | None:
        if limits is None:
            return None
        if limits.max_tool_calls is not None and self.tool_calls > limits.max_tool_calls:
            return f"limit: {limits.max_tool_calls} tool calls"
        if limits.max_tokens is not None and self.tokens > limits.max_tokens:
            return f"limit: {limits.max_tokens} tokens"
        return None

    def on_step(self, step: dict[str, Any]) -> None:
        if all(k in step for k in STEP_FIELDS):
            self.recorder.add_external({k: step[k] for k in STEP_FIELDS})

    def on_event(self, event: Event) -> None:
        self.recorder.on_event(event)
        if isinstance(event, Started):
            self.session_id = event.session_id or self.session_id
            self.model = event.model or self.model
        elif isinstance(event, Final):
            self.final = event
            self.session_id = event.session_id or self.session_id
        elif isinstance(event, ErrorEvent):
            self.errors.append(event.message)
        elif isinstance(event, Notice) and len(self.notices) < MAX_NOTICES:
            self.notices.append(event.text[:NOTICE_LIMIT])
        elif isinstance(event, Usage):
            self.usage = dict(event.data)
            self.tokens += usage_tokens(event.data)
        elif isinstance(event, ToolCall):
            self.tool_calls += 1


def _metabot_command(*args: str) -> list[str]:
    # The engine runs these in the person's project: -I keeps a folder there
    # named "metabot" from standing in for the gate or the switchboard.
    return [sys.executable, "-I", "-m", "metabot", *args]


@contextlib.asynccontextmanager
async def _switchboard(log_path: Path, **kwargs: Any) -> AsyncIterator[Switchboard]:
    """The switchboard, its servers' chatter in the run's log, as ``metabot mcp serve`` has it."""
    write_private(log_path, "")
    with open(log_path, "a", encoding="utf-8") as errlog:
        async with Switchboard(ConnectionStore(), errlog=errlog, **kwargs) as board:
            yield board


@contextlib.asynccontextmanager
async def _events(
    engine: Any,
    job: EngineJob,
    server: ControlServer,
    approve: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
    run: _Run,
    switchboard: bool,
    connections: bool = True,
    skills: Mapping[str, Path] | None = None,
    only: Sequence[str] | None = None,
) -> AsyncIterator[AsyncIterator[Event]]:
    """The engine's event stream, with whatever it needs running around it.

    A CLI engine runs as a subprocess and reaches the runner through the
    control socket. The in-process engine runs the tool loop here, with the
    switchboard in this process.
    """
    if isinstance(engine, CliEngine):
        try:
            await server.start()
            async with contextlib.aclosing(run_cli(engine, job)) as stream:
                yield stream
        finally:
            await server.close()  # also when it never started: it gives back its folder
        return

    async def record(step: dict[str, Any]) -> None:
        run.on_step(step)

    try:
        async with contextlib.AsyncExitStack() as stack:
            board = None
            if switchboard:
                assert job.work_dir is not None  # run_job gives every run its directory
                board = await stack.enter_async_context(
                    _switchboard(
                        job.work_dir / "switchboard.log",
                        approve=approve,
                        record=record,
                        skills=skills,
                        connections=connections,
                        only=only,
                    )  # fmt: skip
                )
            stream = engine.run(job, board=board, approve=approve)
            yield await stack.enter_async_context(contextlib.aclosing(stream))
    finally:
        await server.close()  # never served here, but its socket's folder is given back


NEW_ID_TRIES = 5


def _claim_new_run() -> tuple[str, Path]:
    """A new run id and its folder; an id another run took first is drawn again."""
    for attempt in range(NEW_ID_TRIES):
        run_id = new_run_id()
        try:
            return run_id, claim_run_dir(run_id)
        except ValueError:
            if attempt == NEW_ID_TRIES - 1:
                raise
    raise AssertionError("unreachable")


async def run_job(
    *,
    engine: str | Any,
    prompt: str,
    cwd: Path,
    policy: Policy,
    approver: Approver,
    model: str | None = None,
    read_only: bool = False,
    resume: str | None = None,
    plugin_dirs: list[Path] | None = None,
    switchboard: bool = False,
    secrets: Mapping[str, str] | None = None,
    on_event: Callable[[Event], None] | None = None,
    run_id: str | None = None,
    meta: Mapping[str, Any] | None = None,
    limits: Limits | None = None,
    criteria: Mapping[str, bool] | None = None,
    connection_ids: Sequence[str] | None = None,
    project: Path | None = None,
) -> RunResult:
    """Run one job and seal its receipt.

    ``run_id`` names the run instead of a fresh id; it must be safe and unused.
    ``meta`` adds fields to the journal and the index, never replacing the
    run's own. ``limits`` end the run once passed: the tool call after the
    cap is recorded and never answered. The local engine never runs it, nor
    does an engine whose hook asks the gate; any other engine may have run
    it already, and its step says so.
    ``criteria`` are judged on every step, as on the platform, and sealed as
    one ``declared_acceptance`` verdict. ``connection_ids`` keeps the
    switchboard to those connections; None means every active one. With
    ``project``, the sealed receipt is also copied into that folder's project,
    under ``.orcha/receipts/``.
    """
    if run_id is not None:
        safe_id(run_id)
    cli = get_engine(engine) if isinstance(engine, str) else engine
    # An engine that loads a plugin itself gets its folder; any other gets its
    # skills as two read tools on the switchboard, which then runs for them alone.
    loads = getattr(cli, "loads_plugins", False)
    skills = {} if loads else plugin_skills(list(plugin_dirs or []))
    board = switchboard or bool(skills)
    # An empty list of connections opens none, here and in ``metabot mcp serve``.
    opens = switchboard and (connection_ids is None or len(connection_ids) > 0)
    key, signer = ensure_key()  # a key that does not read stops the run before it takes a folder
    if run_id is None:
        run_id, rdir = _claim_new_run()
    else:
        rdir = claim_run_dir(run_id)
    decider = Decider(policy, approver, signer)
    write_private(rdir / "policy.json", json.dumps(decider.policy.to_json(), indent=2) + "\n")
    run = _Run(
        engine=cli,
        decider=decider,
        # The switchboard records its own calls; without one, a tool that only
        # shares its name is the engine's call and goes in the record.
        recorder=StepRecorder(
            cli.tool_prefix,
            cli.skip_tools if board else (),
            NO_RESULT if getattr(cli, "own_tools_gated", False) else MAY_HAVE_RUN,
        ),
        model=model,
        model_requested=model,
    )

    async def approve(msg: dict[str, Any]) -> dict[str, Any]:
        if run.limit:  # past a limit, nothing more is allowed
            call_id = str(msg.get("call_id") or "unknown")
            decider.verdicts.append(approval_verdict(call_id, approved=False, reason=run.limit))
            return {"allow": False, "reason": run.limit}
        return await decider(msg)

    # Every token the person keeps, also when no connection is in this run: an
    # engine can still read one from disk, and it must not reach the record.
    # So does the key an engine sends to its model.
    engine_keys: dict[str, str] = getattr(cli, "redaction_map", dict)()
    secrets = {**ConnectionStore().redaction_map(), **engine_keys, **(secrets or {})}
    if board and skills:
        write_skills(rdir, skills)
    # The socket may take a temp folder, which closing the server gives back,
    # so the work above that can fail is done before it is taken.
    sock = socket_path(rdir)
    server = ControlServer(
        sock, on_approve=approve, on_step=run.on_step, cleanup_dir=sock.parent != rdir
    )
    job = EngineJob(
        prompt=prompt,
        cwd=Path(cwd),
        model=model,
        plugin_dirs=list(plugin_dirs or []),
        read_only=read_only,
        resume=resume,
        work_dir=rdir,
    )
    if getattr(cli, "supports_hooks", False):
        job.gate_hook = _metabot_command(
            "hook", "pre-tool-use", "--control", str(sock), "--prefix", cli.tool_prefix,
            *(["--switchboard"] if board else []),
        )  # fmt: skip
    if board:
        only = [a for did in connection_ids or [] for a in ("--only", did)]
        job.switchboard = _metabot_command(
            "mcp", "serve", "--home", str(metabot_home()), "--run-dir", str(rdir),
            "--control", str(sock), *([] if opens else ["--no-connections"]), *only,
        )  # fmt: skip
    started_at = utc_now()
    cancelled = False
    try:
        async with _events(
            cli, job, server, approve, run, board, connections=opens, skills=skills,
            only=connection_ids,
        ) as stream:  # fmt: skip
            async for event in stream:
                run.on_event(event)
                if on_event is not None:
                    on_event(event)
                if run.limit is None:
                    run.limit = run.breach(limits)
                    if run.limit:
                        run.errors.append(run.limit)
                # Past a limit the run ends at its next tool call; a final answer
                # that comes first is kept.
                if run.limit and isinstance(event, ToolCall):
                    break
    except asyncio.CancelledError:
        cancelled = True
        run.errors.append("cancelled")
    finally:
        result = _seal(
            run, run_id, prompt, cwd, started_at, key, secrets, cancelled, meta, criteria
        )
        result.project_receipt, result.project_error = project_copy(result.receipt, project)
    if cancelled:
        raise RunCancelled(result)
    return result


def _chosen(run: Any, extra: Mapping[str, Any] | None) -> dict[str, Any]:
    """Beside a suggestion, what actually ran: its engine and the model asked for."""
    if not extra or "suggested" not in extra:
        return {}
    return {"chosen": {"engine": run.engine.name, "model": run.model_requested}}


def _seal(
    run: _Run,
    run_id: str,
    prompt: str,
    cwd: Path,
    started_at: str,
    key: Any,
    secrets: Mapping[str, str] | None,
    cancelled: bool,
    extra: Mapping[str, Any] | None = None,
    criteria: Mapping[str, bool] | None = None,
) -> RunResult:
    if run.final is None and not run.errors:
        run.errors.append(f"{run.engine.name} ended without a final answer")
    error = "; ".join(run.errors) or None
    ok = error is None
    status = "cancelled" if cancelled else ("ok" if ok else "error")
    steps = run.recorder.steps()
    final_text = run.final.text if run.final else ""
    receipt = seal_and_store(
        run_id=run_id,
        steps=steps,
        verdicts=[model_verdict(run.engine.name, run.model), *run.decider.verdicts],
        started_at=started_at,
        finished_at=utc_now(),
        key=key,
        secrets=secrets,
        criteria=criteria,
        meta={
            **(extra or {}),
            **_chosen(run, extra),
            "engine": run.engine.name,
            "model": run.model,
            "model_requested": run.model_requested,
            "status": status,
            "prompt": prompt,
            "cwd": str(cwd),
            "session_id": run.session_id,
            "final": final_text,
            "error": error,
            "usage": run.usage,
            "tokens": run.tokens,
            "tool_calls": run.tool_calls,
            "limit": run.limit,
            "notices": run.notices,
        },
    )
    declined, failed = declined_and_failed(steps, run.decider.verdicts)
    return RunResult(
        run_id=run_id,
        receipt=receipt,
        final_text=final_text,
        ok=ok,
        steps=len(steps),
        error=error,
        session_id=run.session_id,
        tokens=run.tokens,
        limit=run.limit,
        declined=declined,
        failed=failed,
    )
