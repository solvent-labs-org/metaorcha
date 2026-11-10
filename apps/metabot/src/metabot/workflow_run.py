"""Running an approved workflow: its steps in order, then one seal over their runs.

Each step is a run MetaBot already makes, started as its own command starts it: an ask
step as ``metabot ask``, a ladder step as ``ask --ladder``, an office step as ``office
run``. Each seals its own receipt (a ladder or an office run its own seal). A step that
does not end well stops the workflow, and the steps after it never start.

The workflow's seal is an ordinary seal (``metabot.seals``) whose members are the steps'
runs, in order. Besides the ``seal`` verdict, it signs ``workflow_run``: the workflow's
sha256, the approval it ran under and the sha256 of that approval's receipt file, how many
steps it has and how many ran, what they spent when every step's cost is known, and the
step it stopped at. A firing on a schedule adds its own verdicts and meta to the seal, and
the money left of its month's cap: each step's run then takes the smaller of its own cap and
what is left, and a step that would start with nothing left never starts.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from emerge.journal import utc_now

from metabot.approvals import Approver, pick_approver
from metabot.paths import kept_receipts, new_run_id, safe_id
from metabot.seals import Member, seal_runs
from metabot.steps import ascii_text
from metabot.workflow_approval import Approval
from metabot.workflows import Step, Workflow, ask_engine, open_office

STEP_ROLE = "step"  # a member's tool is metabot.member.step


@dataclass
class StepResult:
    name: str
    kind: str
    run_id: str | None = None  # the run, ladder or office run; None when none started
    receipt: Path | None = None  # its receipt, or its seal
    ok: bool = False
    answer: str | None = None
    cost_musd: int | None = None  # None: not known
    error: str | None = None


@dataclass
class WorkflowRun:
    run_id: str
    wf: Workflow
    approval: Approval
    steps: list[StepResult] = field(default_factory=list)
    seal: Path | None = None
    seal_error: str | None = None
    verdicts: list[dict[str, str]] = field(default_factory=list)  # signed beside workflow_run
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return len(self.steps) == len(self.wf.steps) and all(s.ok for s in self.steps)

    @property
    def stopped(self) -> str | None:
        return next((s.name for s in self.steps if not s.ok), None)

    def spent_musd(self) -> int | None:
        costs = [s.cost_musd for s in self.steps]
        return None if any(c is None for c in costs) else sum(c or 0 for c in costs)


def _note(trace: Any, text: str) -> None:
    note = getattr(trace, "note", None)
    if callable(note):
        note(text)


async def _ask(wf: Workflow, step: Step, out: StepResult, approver: Approver, trace: Any) -> None:
    from metabot.commands import ask_once
    from metabot.runner import Limits

    engine, name, model = ask_engine(step)
    _note(trace, f"[metabot] step {step.name}: ask on {name}{'/' + model if model else ''}")
    result = await ask_once(
        engine_name=name,
        prompt=step.prompt,
        cwd=Path(wf.cwd),
        model=model,
        allow_write=step.allow_write,
        allows=list(step.allow),
        read_only=step.read_only,
        connections=step.connections,
        plugins=step.plugins,
        trace=trace,
        role=step.role,
        approver=approver,
        engine=engine,
        limits=Limits(max_cost_musd=step.max_cost_musd) if step.max_cost_musd else None,
        extra_meta={"workflow": wf.name, "workflow_sha256": wf.sha256, "workflow_step": step.name},
    )
    out.run_id, out.receipt, out.ok = result.run_id, Path(result.receipt), result.ok
    out.answer = result.final_text or None
    out.cost_musd = result.cost_musd
    out.error = result.error


async def _ladder(
    wf: Workflow, step: Step, out: StepResult, approver: Approver, trace: Any
) -> None:
    from metabot.commands import ladder_run, ladder_setup

    args = argparse.Namespace(
        engine=None, model=None, suggest=False, resume=None, role=step.role, cwd=wf.cwd,
        check=list(step.ladder_checks), read_only=step.read_only, allow_write=step.allow_write,
        allow=list(step.allow), no_connections=not step.connections,
        no_plugins=not step.plugins, project_receipt=None, max_cost=step.max_cost_musd,
    )  # fmt: skip
    _note(trace, f"[metabot] step {step.name}: ladder")
    setup = ladder_setup(args, trace, step.prompt)
    out.run_id = setup.ladder_id
    result = await ladder_run(setup, args, trace, approver=approver)
    out.receipt = Path(result.seal) if result.seal else None
    out.ok = result.answered and result.seal is not None
    out.answer = result.final.final_text if result.final is not None else None
    out.cost_musd = None if result.unpriced else result.spent_musd
    out.error = result.stopped or result.seal_error


async def _office(
    wf: Workflow, step: Step, out: StepResult, approver: Approver, trace: Any
) -> None:
    from metabot.config import load_config
    from metabot.office_commands import roster_of
    from metabot.office_run import ROUNDS_DEFAULT, run_fan, run_relay

    office = open_office(step)  # its office.yaml as the workflow pinned it, read again now
    _note(trace, f"[metabot] step {step.name}: office {office.name}")
    out.run_id = new_run_id()
    kw: dict[str, Any] = {} if step.fan else {"advisors": step.advisors}
    runner = run_fan if step.fan else run_relay
    result = await runner(
        office,
        step.prompt,
        approver=approver,
        desks=list(step.desks) if step.desks else None,
        allow_write=step.allow_write,
        allows=list(step.allow),
        roster=roster_of(load_config()),
        connections=step.connections,
        plugins=step.plugins,
        on_event=trace,
        checks=step.office_checks,
        rounds=(step.rounds or ROUNDS_DEFAULT) if step.until_checks else 1,
        max_cost_musd=step.max_cost_musd,
        office_run_id=out.run_id,
        **kw,
    )
    out.receipt = result.seal
    out.ok = result.ok and result.record_error is None and result.seal is not None
    worked = [r for r in result.rows if r.advises is None and r.next_round is None]
    out.answer = next((r.answer for r in reversed(worked) if r.answer), None)
    unknown = result.cost_unknown or any(r.cost_musd is None for r in result.rows if r.run_id)
    out.cost_musd = None if unknown else sum(r.cost_musd or 0 for r in result.rows)
    out.error = result.record_error


KINDS = {"ask": _ask, "ladder": _ladder, "office": _office}


def _capped(step: Step, run: WorkflowRun, left_musd: int | None) -> Step | str:
    """``step`` with the money left as its cap when that is less than its own; why it may
    not start when nothing is left, or what the steps before it spent is not known."""
    if left_musd is None:
        return step
    before = run.steps[:-1]  # the last is the step about to start
    if any(s.cost_musd is None for s in before):
        return "what the steps before it spent is not known, so the month's cap cannot hold"
    left = left_musd - sum(s.cost_musd or 0 for s in before)
    if left <= 0:
        return "nothing is left of the month's cap"
    if step.max_cost_musd is not None and step.max_cost_musd <= left:
        return step
    return replace(step, max_cost_musd=left)


def _settle(out: StepResult) -> None:
    """After a step that broke off: the receipt its run left, or no run when it left none."""
    if out.receipt is not None or out.run_id is None:
        return
    kept = kept_receipts() / f"{safe_id(out.run_id)}.json"
    if kept.is_file():
        out.receipt = kept
    else:
        out.run_id = None  # it never started, or never sealed: no member of the seal


def _seal(run: WorkflowRun, started_at: str) -> None:
    members = [
        Member(s.run_id, STEP_ROLE, s.receipt, s.ok) for s in run.steps if s.run_id is not None
    ]
    spent = run.spent_musd()
    detail = (
        f"sha256={run.wf.sha256};approval={run.approval.run_id};"
        f"approval_receipt={run.approval.digest};steps={len(run.wf.steps)};"
        f"ran={len(members)};spent_musd={'none' if spent is None else spent}"
    )
    if run.stopped is not None:
        detail += f";stopped={run.stopped}"
    try:
        run.seal = seal_runs(
            run.run_id,
            members,
            delivered=run.ok,
            kind_detail=f"kind=workflow;name={ascii_text(run.wf.name)}",
            verdicts=[{"check": "workflow_run", "result": "pass", "detail": detail}, *run.verdicts],
            started_at=started_at,
            meta={
                **run.meta,
                "sealed": "workflow",
                "workflow": run.wf.name,
                "workflow_sha256": run.wf.sha256,
                "approval": run.approval.run_id,
                **({"stopped": run.stopped} if run.stopped else {}),
            },
        )
    except (OSError, ValueError) as exc:
        run.seal_error = f"the workflow run could not be sealed: {exc}"


async def run_workflow(
    wf: Workflow,
    approval: Approval,
    *,
    trace: Any = None,
    approver: Approver | None = None,
    run_id: str | None = None,
    verdicts: Sequence[dict[str, str]] = (),
    meta: Mapping[str, Any] | None = None,
    money_left_musd: int | None = None,
) -> WorkflowRun:
    """Run ``wf``'s steps in order under ``approval``, then seal them.

    The caller has checked that ``approval`` is in force for ``wf``'s sha256. A stop still
    seals what ran, then goes on up. ``verdicts`` and ``meta`` join the seal's;
    ``money_left_musd`` caps the money every step together may spend.
    """
    from metabot.runner import NoPrice

    approver = approver if approver is not None else pick_approver()
    run = WorkflowRun(run_id or new_run_id(), wf, approval, verdicts=list(verdicts))
    run.meta = dict(meta or {})
    started_at = utc_now()
    try:
        for step in wf.steps:
            out = StepResult(step.name, step.kind)
            run.steps.append(out)
            capped = _capped(step, run, money_left_musd)
            if isinstance(capped, str):  # nothing left of the cap: the step never starts
                out.ok, out.error = False, capped
                break
            try:
                await KINDS[step.kind](wf, capped, out, approver, trace)
            except (NoPrice, ValueError, OSError) as exc:  # ConfigError, OfficeError, git, ...
                out.ok, out.error = False, str(exc)
                _settle(out)
            if not out.ok:
                break
    except asyncio.CancelledError as exc:  # RunCancelled is one: its run sealed its receipt
        result = getattr(exc, "result", None)
        if run.steps and result is not None and run.steps[-1].run_id is None:
            run.steps[-1].run_id, run.steps[-1].receipt = result.run_id, Path(result.receipt)
        if run.steps:
            run.steps[-1].ok, run.steps[-1].error = False, "stopped"
            _settle(run.steps[-1])
        _seal(run, started_at)
        raise
    _seal(run, started_at)
    return run
