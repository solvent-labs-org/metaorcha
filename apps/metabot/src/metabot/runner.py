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
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from emerge.journal import utc_now

from metabot.approvals import Approver
from metabot.config import load_config
from metabot.connections import ConnectionStore, merged_secrets, record_secrets
from metabot.control import WITHDRAWN, ControlServer, socket_path
from metabot.decisions import Decider
from metabot.engines import get_engine
from metabot.engines.base import CliEngine, EngineJob, run_cli
from metabot.events import ErrorEvent, Event, Final, Notice, Started, TextEvent, ToolCall, Usage
from metabot.gate import Policy
from metabot.paths import claim_run_dir, metabot_home, new_run_id, safe_id, write_private
from metabot.plugins import plugin_skills
from metabot.prices import (
    Price,
    named_as_it_starts,
    price_for,
    prices_digest,
    read_prices,
    same_model,
)
from metabot.receipts import (
    approval_verdict,
    budget_verdict,
    declined_and_failed,
    ensure_key,
    model_verdict,
    project_copy,
    provenance_verdicts,
    scrub,
    seal_and_store,
    usage_verdict,
)
from metabot.sandbox import plan_for
from metabot.spend import Spend, dollars, spend_of
from metabot.steps import MAY_HAVE_RUN, NO_RESULT, StepRecorder
from metabot.switchboard import Switchboard, write_skills

STEP_FIELDS = ("call_id", "tool", "args", "output", "success", "latency_ms")
MAX_NOTICES = 20
MAX_RAN_ON = 8  # the models a run's tokens are named as having run on, at most
MAX_EARLY = 1000  # the reports kept from before a run names its model, at most
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
    cost_musd: int | None = None  # None: no price for what ran, or no usage it could count
    cost_source: str = "unknown"  # engine | table | config | local | plan | unsent | unknown
    usage_reported: bool = False  # the engine sent a usage report, readable or not
    lost: bool = False  # a request went out and never said what it spent
    named_late: bool = False  # tokens came before the engine named a model pricing them otherwise
    unpriced: list[str] = field(default_factory=list)  # each model a price is missing for


class NoPrice(ValueError):
    """A money cap on a run MetaBot cannot price: it would never act, so the run never starts."""


@dataclass(frozen=True)
class Limits:
    """A run's hard limits. A run that passes one ends, with the limit as its error."""

    max_tool_calls: int | None = None
    max_tokens: int | None = None
    max_cost_musd: int | None = None  # micro-dollars


def usage_tokens(data: Mapping[str, Any]) -> int:
    """The tokens one usage report counts, the way Code Desks' meter counts them.

    Input and output, with cache writes and cache reads, each counted once.
    """
    return spend_of(data).tokens


def unpriced(price: Price, engine: str, model: str | None) -> bool:
    """Under a money cap, the run cannot start: nothing prices it, or its named model has no
    rate to count it by as it goes. An engine that names its model as it starts, as claude
    does with your account's default or an alias such as ``haiku``, is checked then, at its
    first tool call."""
    if price.source in ("plan", "unknown"):
        return True
    return price.rate is None and bool(model) and not named_as_it_starts(engine, model)


def _no_price(engine: str, model: str | None, as_asked: bool = True) -> Price:
    return Price("unknown")


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
    stopping: bool = False  # the run is ending: nothing more is asked or allowed
    sandbox: Any = None  # a CLI engine's SandboxPlan, signed as a verdict
    price_of: Callable[..., Price] = field(default_factory=lambda: _no_price)
    spend: Spend = field(default_factory=Spend)
    estimate: int = 0  # micro-dollars, by the rate, report by report
    prices: str = ""  # the digest of the rates it was priced by
    cap_musd: int | None = None
    ran_on: list[str] = field(default_factory=list)  # the models its messages say they ran on
    off_price: list[str] = field(default_factory=list)  # those counted at no price, at most 8
    named: bool = False  # a start has named the model the run is on
    early: list[Spend] = field(default_factory=list)  # reports before that, by no model
    early_more: bool = False  # more of them came than were kept
    named_late: bool = False  # the model then named prices them otherwise than they were
    answered: bool = False  # the model gave text, a call or a usage report
    unsent: bool = False  # an error said nothing reached the model
    lost: bool = False  # an error said a request went out and what it spent never came back

    def cost(self) -> tuple[int | None, str]:
        """What the run cost in micro-dollars, and where that comes from; None when unknown.

        A rate times no token counted is no cost: a run whose engine reported no
        usage MetaBot could read has an unknown one, and a cap over it is not kept;
        so has a run with a request that went out and never said what it spent.
        A run that never reached its model, so that nothing ran on it, cost nothing.
        """
        if self.spend.reported_musd is not None:
            return self.spend.reported_musd, "engine"
        if self.unsent and not self.answered:
            return 0, "unsent"
        price = self.price_of(self.engine.name, self.model)
        if price.rate is None:
            return None, "plan" if price.source == "plan" else "unknown"
        unknown = self.lost or self.off_price or self.named_late or not self.spend.tokens
        if unknown and price.source != "local":
            return None, "unknown"
        return self.estimate, "table" if price.source == "engine" else price.source

    def check(self, limits: Limits | None, at_call: bool) -> None:
        """Note the first limit the run is past."""
        if self.limit is None:
            self.limit = self.breach(limits, at_call)
            if self.limit:
                self.errors.append(self.limit)

    def breach(self, limits: Limits | None, at_call: bool = False) -> str | None:
        """The limit the run is past, if any. A run whose spend cannot be counted is past
        its money cap at a call, which it would go on with: an answer is kept."""
        if limits is None:
            return None
        if limits.max_tool_calls is not None and self.tool_calls > limits.max_tool_calls:
            return f"limit: {limits.max_tool_calls} tool calls"
        if limits.max_tokens is not None and self.tokens > limits.max_tokens:
            return f"limit: {limits.max_tokens} tokens"
        cap = limits.max_cost_musd
        if cap is not None and at_call and self.uncounted():
            if self.lost:
                return f"limit: {dollars(cap)}: a request that went out never said what it spent"
            if self.named_late:
                return (
                    f"limit: {dollars(cap)}: {self.engine.name} reported tokens before it named "
                    "its model"
                )
            names = self.unpriced() or [self.model or "its default"]
            named = " and ".join(f"{self.engine.name}/{name}" for name in names)
            return f"limit: {dollars(cap)}: no price for {named} to count it by"
        if cap is not None and (self.cost()[0] or self.estimate) > cap:
            return f"limit: {dollars(cap)}"
        return None

    def unpriced(self) -> list[str]:
        """Each model a price is missing for, which leaves the cost unknown: each other model a
        report was counted by at none, and the run's own, which is priced as the one asked
        for, by whatever name its reports give it."""
        price = self.price_of(self.engine.name, self.model)
        missing = price.rate is None and price.source != "plan"
        names = [name for name in self.off_price if not (missing and same_model(name, self.model))]
        own = self._asked() or self.model
        if missing and own and own not in names:
            names.append(own)
        return names

    def uncounted(self) -> bool:
        """No rate counts what the run spends as it goes, or a request never said what it
        spent, and no engine has said it yet. On this machine nothing is spent."""
        price = self.price_of(self.engine.name, self.model)
        if price.source == "local" or self.spend.reported_musd is not None:
            return False
        return price.rate is None or bool(self.off_price) or self.named_late or self.lost

    def _on_usage(self, event: Usage) -> None:
        self.usage = dict(event.data)
        self.lost = self.lost or event.lost
        spend = spend_of(event.data)
        rate = self.price_of(self.engine.name, self.model).rate
        if event.model and spend.tokens:  # priced by the model that ran it
            if event.model not in self.ran_on and len(self.ran_on) < MAX_RAN_ON:
                self.ran_on.append(event.model)
            # by the name it gives: another model's, or the one asked for, named a way that may
            # have a price
            if not same_model(event.model, self.model) or rate is None:
                rate = self.price_of(self.engine.name, event.model, False).rate
        if event.total:  # the run's whole so far: what it holds past what was counted is priced
            # here, as one request, since how that splits is unknown, and what was counted
            # stands, by the kinds it was priced as; the engine's own figure is the run's
            whole, spend = spend, spend.beyond(self.spend)
            self.spend = replace(self.spend + spend, reported_musd=whole.reported_musd)
        else:
            self.spend = self.spend + spend
        if spend.tokens and not (event.model or self.named):
            # priced by the model asked for, as none is named yet; kept to price again by the
            # one the run names, one report at a time, as a rate's tier goes by the report
            if len(self.early) < MAX_EARLY:
                self.early.append(spend)
            else:
                self.early_more = True
        elif rate is None and spend.tokens:
            # counted at no price: the cost stays unknown, also once the run names a model that
            # has one. A model a report names has no price of its own; else neither has the
            # one the run named, nor the one asked for, by which every named one is priced
            self._off_price(event.model or self._asked() or self.model)
        self.estimate += rate.cost(spend) if rate else 0
        self.tokens = self.spend.tokens

    def _off_price(self, name: str | None) -> None:
        if name and name not in self.off_price and len(self.off_price) < MAX_RAN_ON:
            self.off_price.append(name)

    def _asked(self) -> str | None:
        """The model asked for, or the engine's own: a model with no price is priced as it."""
        return self.model_requested or getattr(self.engine, "model", None)

    def _name(self, named: str) -> None:
        """The run names the model it is on. The tokens reported before were priced by the
        model asked for: when ``named`` prices them otherwise, or none was asked for, what they
        cost is unknown, and a model named after it does not change that. When ``named`` has
        no price, neither has the one asked for (a model with none of its own is priced as
        that one): they were counted at none, whatever came first, and that is the model to
        price."""
        self.named = True
        if not self.early and not self.early_more:
            return
        before = self.price_of(self.engine.name, self.model).rate
        after = self.price_of(self.engine.name, named).rate
        asked = self._asked()
        if after is None and asked:
            self._off_price(asked)
        elif after is None or before is None or self.early_more:
            self.named_late = after is None or before != after
        else:
            self.named_late = sum(map(before.cost, self.early)) != sum(map(after.cost, self.early))

    def on_step(self, step: dict[str, Any]) -> None:
        if all(k in step for k in STEP_FIELDS):
            self.recorder.add_external({k: step[k] for k in STEP_FIELDS})

    def on_event(self, event: Event) -> None:
        self.recorder.on_event(event)
        if isinstance(event, (TextEvent, ToolCall, Usage)):
            self.answered = True  # the model said something, so it ran
        if isinstance(event, Started):
            self.session_id = event.session_id or self.session_id
            if event.model and not self.named:
                self._name(event.model)
            self.model = event.model or self.model
        elif isinstance(event, Final):
            self.final = event
            self.session_id = event.session_id or self.session_id
        elif isinstance(event, ErrorEvent):
            self.errors.append(event.message)
            self.unsent = self.unsent or event.unsent
            self.lost = self.lost or event.lost
        elif isinstance(event, Notice) and len(self.notices) < MAX_NOTICES:
            self.notices.append(event.text[:NOTICE_LIMIT])
        elif isinstance(event, Usage):
            self._on_usage(event)
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

        def stopping() -> None:  # before the engine is stopped, which may be asking still
            run.stopping = True
            server.withdraw()

        try:
            await server.start()
            async with contextlib.aclosing(run_cli(engine, job, stopping)) as stream:
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
    it already, and its step says so. A CLI engine's call through the
    switchboard is the switchboard's to record: once it took it, and never,
    when the run stopped before it did. The in-process engine records its own,
    unanswered, and the switchboard's step takes its place once it took it.
    ``criteria`` are judged on every step, as on the platform, and sealed as
    one ``declared_acceptance`` verdict. ``connection_ids`` keeps the
    switchboard to those connections; None means every active one. With
    ``project``, the sealed receipt is also copied into that folder's project,
    under ``.orcha/receipts/``.
    """
    if run_id is not None:
        safe_id(run_id)
    cli = get_engine(engine) if isinstance(engine, str) else engine
    mine = read_prices(load_config().prices)
    base_url = getattr(cli, "base_url", None)

    asked = model or getattr(cli, "model", None)

    def price_of(name: str, ran: str | None, as_asked: bool = True) -> Price:
        price = price_for(name, ran or asked, base_url, mine)
        if price.rate is None and ran and asked and ran != asked and as_asked:
            # an engine may name the model its own way: the one asked for has the price
            price = price_for(name, asked, base_url, mine)
        return price

    cap = limits.max_cost_musd if limits is not None else None
    if cap is not None and unpriced(price_of(cli.name, None), cli.name, asked):
        raise NoPrice(
            f"no price for {cli.name}/{asked or 'default'}: add its rates under prices in "
            "config.json"
        )
    # An engine that loads a plugin itself gets its folder; any other gets its
    # skills as two read tools on the switchboard, which then runs for them alone.
    loads = getattr(cli, "loads_plugins", False)
    skills = {} if loads else plugin_skills(list(plugin_dirs or []))
    board = switchboard or bool(skills)
    # An empty list of connections opens none, here and in ``metabot mcp serve``.
    opens = switchboard and (connection_ids is None or len(connection_ids) > 0)
    # A plan that cannot be made, or a key that does not read, stops the run before it
    # takes a folder.
    plan = (
        plan_for(cli, Path(cwd), enabled=load_config().sandbox)
        if isinstance(cli, CliEngine)
        else None
    )
    key, signer = ensure_key()
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
        price_of=price_of,
        prices=prices_digest(mine),
        cap_musd=cap,
        sandbox=plan,
    )

    async def approve(msg: dict[str, Any]) -> dict[str, Any]:
        run.check(limits, at_call=True)  # it may ask before the run hears of the call
        call_id = str(msg.get("call_id") or "unknown")
        if run.limit:  # past a limit, nothing more is allowed
            decider.verdicts.append(approval_verdict(call_id, approved=False, reason=run.limit))
            return {"allow": False, "reason": run.limit}
        if run.stopping:  # nor once the run is ending, and no one is asked
            decider.verdicts.append(approval_verdict(call_id, approved=False, reason="cancelled"))
            return dict(WITHDRAWN)
        return await decider(msg)

    # Every token the person keeps, also when no connection is in this run: an
    # engine can still read one from disk, and it must not reach the record.
    # So does every key the settings name for a model, the one this engine
    # sends among them.
    engine_keys: dict[str, str] = getattr(cli, "redaction_map", dict)()
    secrets = merged_secrets(record_secrets(), engine_keys, secrets or {})
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
    job.sandbox = plan
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
                run.check(limits, at_call=isinstance(event, ToolCall))
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
    cost, source = run.cost()
    money = [usage_verdict(run.spend, cost, source, run.prices)]
    if run.cap_musd is not None:
        money.append(budget_verdict(run.cap_musd, cost))
    placed = {**(extra or {}), **(getattr(run.engine, "receipt_meta", None) or {})}
    sandboxed = run.sandbox.verdict() if run.sandbox is not None else None
    receipt = seal_and_store(
        run_id=run_id,
        steps=steps,
        verdicts=[
            model_verdict(run.engine.name, run.model, run.ran_on),
            *provenance_verdicts(placed),
            *([sandboxed] if sandboxed is not None else []),
            *money,
            *run.decider.verdicts,
        ],
        started_at=started_at,
        finished_at=utc_now(),
        key=key,
        secrets=secrets,
        criteria=criteria,
        commit=("prompt", "final"),
        meta={
            **(extra or {}),
            **_chosen(run, extra),
            **(getattr(run.engine, "receipt_meta", None) or {}),
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
            "spend": run.spend.as_dict(),
            "cost_musd": cost,
            "cost_source": source,
            "tool_calls": run.tool_calls,
            "limit": run.limit,
            "notices": run.notices,
        },
    )
    declined, failed = declined_and_failed(steps, run.decider.verdicts)
    # The answer and the error as the journal keeps them: whatever takes them on, an
    # office's record, the next desk, a routine's firing, a page, keeps no secret either.
    return RunResult(
        run_id=run_id,
        receipt=receipt,
        final_text=scrub(final_text, secrets),
        ok=ok,
        steps=len(steps),
        error=scrub(error, secrets),
        session_id=run.session_id,
        tokens=run.tokens,
        limit=run.limit,
        declined=declined,
        failed=failed,
        cost_musd=cost,
        cost_source=source,
        usage_reported=bool(run.usage),
        lost=run.lost,
        named_late=run.named_late,
        unpriced=run.unpriced() if source == "unknown" else [],
    )
