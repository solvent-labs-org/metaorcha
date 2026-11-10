"""A routine that fires an approved workflow: it runs on a schedule under a ceiling you approved.

A routine may name a workflow instead of a goal of its own: ``metabot workflow schedule
REF``. REF, a name or a sha256, is pinned as the sha256 it finds when the routine is saved,
and the routine takes its schedule from that file. A version kept later under the
same name never moves the routine: save the routine again for that one.

Each firing, before any step runs:

* reads the workflow's bytes again and hashes them, and bytes that no longer hash to the
  pinned sha256 are refused;
* needs an approval in force for that sha256, signed with this home's key, as ``workflow
  run`` does (``metabot.workflow_approval``);
* needs the file to hold a schedule and a ceiling, and the routine's schedule to be the one
  in the file: nothing supplies a ceiling, or an amount of money, by default;
* counts the ceiling, and is refused past it: every firing used, the day it expires reached
  in the schedule's zone, or this month's money at or over the cap.

What the ceiling counts comes from the firings' seals. A firing that runs is one seal, kept
as ``wff-<16 digits of the sha256>-<time>-<hex>``, whose ``workflow_firing`` verdict signs
the sha256, the approval and the sha256 of its receipt file, the routine, the firing's
number and the ceiling as it stood, and whose ``workflow_run`` verdict signs what the steps
spent. A seal counts when it verifies, this home's key signed it and it names its own id.
One that does not refuses the next firing, since the ceiling cannot be counted. The firings
log can add to the count and never take from it: a firing it says ran still counts when its
seal is gone, and leaves this month's money not known, which refuses a capped firing.

The firings and the money are the workflow's, over every routine that names it. A refusal
is a ``refused`` row in the firings log, with its reason: nothing runs and nothing is
sealed. The cap holds inside a firing too, since each step takes what is left of it as its
money cap. No one is there to ask, so a write the workflow does not allow is declined.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import os
import secrets
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, tzinfo
from pathlib import Path
from typing import Any

from emerge.journal import utc_now
from emerge.preimage import redact_credentials

from metabot.approvals import DenyAll, printable
from metabot.live import begin_live, end_live
from metabot.paths import kept_receipts, metabot_home
from metabot.routines import (
    ID_SHAPE,
    NAME_RE,
    NOT_APPROVED,
    REASON_LIMIT,
    Firing,
    RoutineError,
    _append,
    _load,
    _locked,
    _sealed_receipt,
    _store,
    firings,
    is_workflow_routine,
    next_due,
    utc_stamp,
)
from metabot.text import ascii_digits
from metabot.workflow_approval import (
    PREFIX_DIGITS,
    Approval,
    NotApproved,
    _our_signer,
    _signed,
    _verdicts,
    approval_for_run,
    approval_state,
)
from metabot.workflows import (
    MAX_BYTES,
    SHA_RE,
    SHORT,
    Workflow,
    WorkflowError,
    here_problems,
    kept_path,
    load_workflow,
    parse_workflow,
)

SEAL_PREFIX = "wff"
FIRING_CHECK = "workflow_firing"
RUN_CHECK = "workflow_run"
RAN = frozenset({"running", "sealed", "error"})  # a row in one of these began a run
LOCK = ".firings.lock"


def _now() -> datetime:
    return datetime.now(UTC)


# ── saving a routine on a workflow ──────────────────────────────────────────


def save_workflow_routine(name: str, ref: str, *, enabled: bool = True) -> str:
    """Save a routine that fires the workflow ``ref`` finds, pinned by its sha256; its id.

    RoutineError when the workflow does not load, has no schedule, or is not approved.
    """
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise RoutineError(
            "name", "1-64 of A-Z, a-z, 0-9, '.', '_' and '-', starting with a letter or digit"
        )
    if ID_SHAPE.match(name):
        raise RoutineError("name", f"{name} has the shape of a routine id; choose another")
    try:
        wf = load_workflow(ref)
    except KeyError as exc:
        raise RoutineError("workflow", str(exc.args[0])) from None
    except WorkflowError as exc:
        raise RoutineError("workflow", str(exc)) from None
    if wf.schedule is None or wf.ceiling is None:
        raise RoutineError(
            "workflow",
            f"{wf.name} {wf.short} has no schedule: give the file a schedule and a ceiling, "
            "keep it with metabot workflow add, and approve it",
        )
    try:
        approval_for_run(wf)
    except NotApproved as exc:
        raise RoutineError("workflow", str(exc)) from None
    with _locked():
        table = _load()
        existing = next((k for k, r in table.items() if r.get("name") == name), None)
        rid = existing or f"rt-{secrets.token_hex(6)}"
        table[rid] = {
            "id": rid,
            "name": name,
            "workflow": wf.sha256,
            "workflow_name": wf.name,
            "cron": wf.schedule.cron,
            "tz": wf.schedule.tz,
            "enabled": enabled,
            "saved_at": utc_now(),
        }
        _store(table)
    return rid


# ── the ceiling ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CeilingState:
    """What a workflow's ceiling has counted, as a firing at ``today`` sees it."""

    used: int  # its firings that began a run
    month: str  # YYYY-MM in its schedule's zone
    spent_musd: int | None  # this month's money its firing seals sign; None: not known
    unknown: str | None  # why the month's money is not known
    today: date  # in its schedule's zone
    unreadable: list[str] = field(default_factory=list)  # its firing seals that do not verify


def _zone(wf: Workflow) -> tzinfo:
    from metabot.cron import zone

    return zone(wf.schedule.tz) if wf.schedule is not None else UTC


def firing_seals(sha256: str) -> tuple[list[tuple[str, dict[str, str]]], list[str]]:
    """Each firing seal of ``sha256`` that verifies, with its ``workflow_firing`` detail and
    the ``spent_musd`` its ``workflow_run`` verdict signs; then the ids of those that do not."""
    folder = kept_receipts()
    if not folder.is_dir():
        return [], []
    our = _our_signer()
    good: list[tuple[str, dict[str, str]]] = []
    bad: list[str] = []
    for path in sorted(folder.glob(f"{SEAL_PREFIX}-{sha256[:PREFIX_DIGITS]}-*.json")):
        run_id = path.name[: -len(".json")]
        envelope = _signed(path, our)
        fired = _verdicts(envelope, FIRING_CHECK) if envelope is not None else []
        ran = _verdicts(envelope, RUN_CHECK) if envelope is not None else []
        if (
            len(fired) != 1
            or len(ran) != 1
            or fired[0].get("sha256") != sha256
            or ran[0].get("sha256") != sha256
        ):
            bad.append(run_id)
            continue
        good.append((run_id, {**fired[0], "spent_musd": ran[0].get("spent_musd", "none")}))
    return good, bad


def _month_of(stamp: str | None, tz: tzinfo) -> str | None:
    try:
        when = datetime.strptime(str(stamp), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None  # a row whose time does not read is taken as this month's
    return when.astimezone(tz).strftime("%Y-%m")


def ceiling_state(wf: Workflow, now: datetime) -> CeilingState:
    """What ``wf``'s ceiling has counted at ``now``: from its firing seals that verify, and
    the firings log where that says more."""
    tz = _zone(wf)
    local = now.astimezone(tz)
    month = local.strftime("%Y-%m")
    good, bad = firing_seals(wf.sha256)
    sealed = {run_id for run_id, _ in good}
    ran = [f for f in firings() if f.workflow == wf.sha256 and f.state in RAN and f.run_id]
    spent, unknown = 0, None
    for run_id, detail in good:
        if detail.get("month") != month:
            continue
        value = detail.get("spent_musd", "none")
        if not ascii_digits(value):
            unknown = unknown or f"firing {run_id} spent an amount not known"
            continue
        spent += int(value)
    for row in ran:
        if row.run_id in sealed or _month_of(row.at, tz) not in (month, None):
            continue
        unknown = unknown or f"firing {row.firing_id} ran and left no receipt that verifies"
    return CeilingState(
        used=max(len(good), len(ran)),
        month=month,
        spent_musd=None if unknown else spent,
        unknown=unknown,
        today=local.date(),
        unreadable=bad,
    )


def ceiling_refusal(wf: Workflow, state: CeilingState) -> str | None:
    """Why a firing of ``wf`` is past its ceiling, or None when it is not."""
    from metabot.spend import dollars

    ceiling = wf.ceiling
    if ceiling is None:
        return "it has no ceiling, and a scheduled firing needs one"
    if state.unreadable:
        return (
            f"a firing receipt of it does not verify ({', '.join(state.unreadable)}), "
            "so its ceiling cannot be counted"
        )
    if ceiling.expires is not None and state.today >= date.fromisoformat(ceiling.expires):
        return f"expired: its ceiling ended on {ceiling.expires}"
    if ceiling.firings is not None and state.used >= ceiling.firings:
        return f"firings used up: {state.used} of {ceiling.firings}"
    cap = ceiling.month_cap_musd
    if cap is not None:
        if state.spent_musd is None:
            return (
                f"this month's money is not known ({state.unknown}), "
                f"so its cap of {dollars(cap)} cannot hold"
            )
        if state.spent_musd >= cap:
            return (
                f"this month's money is at or over the cap: {dollars(state.spent_musd)} of "
                f"{dollars(cap)} in {state.month}"
            )
    return None


# ── a firing ────────────────────────────────────────────────────────────────


@contextlib.contextmanager
def _firing_lock() -> Iterator[None]:
    """One firing at a time between its checks and its ``running`` row."""
    fd = os.open(metabot_home() / LOCK, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


@dataclass(frozen=True)
class Checked:
    wf: Workflow
    approval: Approval
    state: CeilingState


def _read_pinned(sha256: str) -> Workflow | str:
    """The workflow kept as ``sha256``, its bytes hashed again; why not, when they are gone,
    changed or no workflow."""
    path = kept_path(sha256)
    try:
        with path.open("rb") as fh:
            raw = fh.read(MAX_BYTES + 1)
    except OSError as exc:
        return f"the workflow kept as {sha256[:SHORT]} cannot be read ({exc.strerror or exc})"
    now = hashlib.sha256(raw).hexdigest()
    if now != sha256:
        return (
            f"the workflow kept as {sha256[:SHORT]} has changed since it was approved "
            f"(its bytes now hash to {now[:SHORT]}), so nothing ran"
        )
    try:
        return parse_workflow(raw, path)
    except WorkflowError as exc:
        return f"the workflow kept as {sha256[:SHORT]} does not read: {exc}"


def check_firing(record: Mapping[str, Any], now: datetime) -> Checked | str:
    """Whether a routine on a workflow may fire at ``now``: what it fires under, or why not."""
    sha = record.get("workflow")
    if not (isinstance(sha, str) and SHA_RE.match(sha)):
        return "the routine names no workflow sha256"
    if not ID_SHAPE.match(str(record.get("id"))):
        return "the routine's id is not one MetaBot gives"
    wf = _read_pinned(sha)
    if isinstance(wf, str):
        return wf
    try:
        approval = approval_for_run(wf)
    except NotApproved as exc:
        return f"workflow {wf.name} {wf.short}: {exc}"
    if wf.schedule is None or wf.ceiling is None:
        return (
            f"workflow {wf.name} {wf.short} has no schedule or no ceiling: a scheduled firing "
            "needs a schedule and a ceiling in the file you approve"
        )
    said = (record.get("cron"), record.get("tz"))
    if said != (wf.schedule.cron, wf.schedule.tz):
        return (
            f"the routine's schedule ({said[0]} {said[1]}) is not the schedule you approved "
            f"({wf.schedule.cron} {wf.schedule.tz})"
        )
    if any(f.workflow == sha and f.state == "running" for f in firings()):
        return "overlap: another firing of this workflow is running"
    state = ceiling_state(wf, now)
    refused = ceiling_refusal(wf, state)
    if refused is not None:
        return refused
    problems = here_problems(wf)
    if problems:
        return "cannot run here: " + "; ".join(problems)
    return Checked(wf, approval, state)


def _seal_id(sha256: str) -> str:
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return f"{SEAL_PREFIX}-{sha256[:PREFIX_DIGITS]}-{stamp}-{secrets.token_hex(3)}"


def lineage_verdict(checked: Checked, rid: str, firing_id: str, due: str | None) -> dict[str, str]:
    """The signed lineage of one firing: what it fired, under which approval, and its ceiling."""
    wf, approval, state = checked.wf, checked.approval, checked.state
    ceiling = wf.ceiling
    assert ceiling is not None
    fields = {
        "sha256": wf.sha256,
        "approval": approval.run_id,
        "approval_receipt": approval.digest,
        "routine": rid,
        "firing": str(state.used + 1),
        "firing_id": firing_id,
        "due": due or "none",
        "firings_used": str(state.used),
        "firings_max": str(ceiling.firings) if ceiling.firings is not None else "none",
        "expires": ceiling.expires or "none",
        "month": state.month,
        "month_spent_musd": "none" if state.spent_musd is None else str(state.spent_musd),
        "month_cap_musd": "none" if ceiling.month_cap_musd is None else str(ceiling.month_cap_musd),
    }
    return {
        "check": FIRING_CHECK,
        "result": "pass",
        "detail": ";".join(f"{k}={v}" for k, v in fields.items()),
    }


def _tokens() -> dict[str, str]:
    """Each secret this home knows, to keep out of a firing's reason."""
    from metabot.config import ConfigError, configured_keys, load_config
    from metabot.connections import ConnectionFileError, ConnectionStore, merged_secrets

    try:
        tokens = ConnectionStore().redaction_map()
    except ConnectionFileError:
        tokens = {}
    try:
        return merged_secrets(tokens, configured_keys(load_config()))
    except ConfigError:
        return tokens


def _stopped(run: Any) -> str | None:
    if run.ok:
        return None
    step = next((s for s in run.steps if not s.ok), None)
    if step is None:
        return "it did not run every step"
    return f"stopped at step {step.name}: {step.error or 'it did not end well'}"


async def fire_workflow(
    record: Mapping[str, Any],
    *,
    due: datetime | None = None,
    on_event: Callable[[Any], None] | None = None,
) -> Firing:
    """Fire a routine on a workflow once: its checks, a ``running`` row, the run, its seal."""
    import asyncio

    from metabot.console import Trace
    from metabot.workflow_run import run_workflow

    rid = str(record.get("id"))
    firing_id = f"fr-{secrets.token_hex(6)}"
    due_text = utc_stamp(due) if due is not None else None
    sha = record.get("workflow")
    held: dict[str, Any] = {"approval": None, "number": None}
    tokens = _tokens()

    def row(state: str, run_id: str | None = None, reason: str | None = None,
            receipt: Path | str | None = None) -> Firing:  # fmt: skip
        if reason is not None and tokens:
            reason = redact_credentials(reason, tokens)
        return _append(
            Firing(
                firing_id,
                rid,
                due_text,
                state,
                run_id,
                None if reason is None else reason[:REASON_LIMIT],
                utc_now(),
                None if receipt is None else str(receipt),
                None,
                workflow=sha if isinstance(sha, str) else None,
                approval=held["approval"],
                number=held["number"],
            )
        )

    with _firing_lock():
        checked = check_firing(record, _now())
        if isinstance(checked, str):
            return row("refused", reason=checked)
        wf = checked.wf
        held["approval"], held["number"] = checked.approval.run_id, checked.state.used + 1
        seal_id = _seal_id(wf.sha256)
        row("running", seal_id)
    cap = wf.ceiling.month_cap_musd if wf.ceiling is not None else None
    left = None if cap is None else cap - (checked.state.spent_musd or 0)
    begin_live("routine", firing_id, name=str(record.get("name")),
               current=f"workflow {wf.name} {wf.short}")  # fmt: skip
    try:
        run = await run_workflow(
            wf,
            checked.approval,
            trace=on_event if on_event is not None else Trace(quiet=True),
            approver=DenyAll(),  # no one is there: a write it does not allow is declined
            run_id=seal_id,
            verdicts=[lineage_verdict(checked, rid, firing_id, due_text)],
            meta={
                "routine": rid,
                "routine_name": record.get("name"),
                "firing": firing_id,
                "firing_number": held["number"],
                "due": due_text,
            },
            money_left_musd=left,
        )
    except asyncio.CancelledError:  # run_workflow sealed what ran
        row("error", seal_id, "stopped", _sealed_receipt(seal_id))
        raise
    except Exception as exc:  # the run broke: what it sealed stays on the row
        return row("error", seal_id, f"{type(exc).__name__}: {exc}", _sealed_receipt(seal_id))
    finally:
        end_live(firing_id)
    if run.seal is None:
        return row("error", seal_id, run.seal_error or "the firing could not be sealed")
    return row("sealed", seal_id, _stopped(run), run.seal)


# ── what lists a routine ────────────────────────────────────────────────────


@dataclass
class Summary:
    """A routine as a list shows it: on a workflow, with its approval and ceiling, or not."""

    record: Mapping[str, Any]
    label: str  # NOT_APPROVED, or "workflow NAME SHORT, <its approval>"
    wf: Workflow | None = None
    approval: str | None = None  # approved, revoked, not approved, or why it cannot be read
    approved_by: Approval | None = None
    state: CeilingState | None = None
    problem: str | None = None  # why it does not fire now, when that is known here


def summarize(record: Mapping[str, Any], now: datetime | None = None) -> Summary:
    """What a list says of a routine. It never raises for a routine that does not read."""
    now = now or _now()
    if not is_workflow_routine(record):
        return Summary(record, NOT_APPROVED, approval=NOT_APPROVED)
    sha = record.get("workflow")
    named = str(record.get("workflow_name") or "?")
    if not (isinstance(sha, str) and SHA_RE.match(sha)):
        return Summary(record, f"workflow {named}, names no sha256", problem="names no sha256")
    wf = _read_pinned(sha)
    if isinstance(wf, str):
        return Summary(record, f"workflow {named} {sha[:SHORT]}, CHANGED or gone", problem=wf)
    approvals = approval_state(sha)
    approved_by = None
    if approvals.unreadable_revokes:
        said = "taken as revoked"
    elif approvals.in_force:
        said = "approved"
        approved_by = max(approvals.in_force, key=lambda a: (a.signed_at, a.run_id))
    elif approvals.revoked:
        said = "revoked"
    else:
        said = NOT_APPROVED
    state = ceiling_state(wf, now) if wf.schedule is not None else None
    problem = None if state is None else ceiling_refusal(wf, state)
    return Summary(
        record, f"workflow {wf.name} {wf.short}, {said}", wf, said, approved_by, state, problem
    )


def used_text(summary: Summary, month_word: str | None = None) -> str:
    """Firings used of the most, and this month's money against its cap: ``firings 3 of 40,
    $1.20 of $20.00 this month``; empty for a routine that is not on a workflow."""
    from metabot.spend import dollars

    wf, state = summary.wf, summary.state
    if wf is None or state is None or wf.ceiling is None:
        return ""
    ceiling = wf.ceiling
    most = f" of {ceiling.firings}" if ceiling.firings is not None else ""
    said = [f"firings {state.used}{most}"]
    when = month_word or f"in {state.month}"
    spent = "not known" if state.spent_musd is None else dollars(state.spent_musd)
    if ceiling.month_cap_musd is not None:
        said.append(f"{spent} of {dollars(ceiling.month_cap_musd)} {when}")
    else:
        said.append(f"{spent} {when}")
    if ceiling.expires is not None:
        said.append(f"until {ceiling.expires}")
    return ", ".join(said)


def firing_label(f: Any) -> str:
    """A firing row's label: the workflow, its number and approval, or ``not approved``."""
    if not f.workflow:
        return NOT_APPROVED
    said = f"workflow {printable(str(f.workflow), 12)[:12]}"
    if isinstance(f.number, int):
        said += f" firing {f.number}"
    if f.approval:
        said += f", approval {printable(str(f.approval), 80)}"
    return said


def next_text(record: Mapping[str, Any], now: datetime | None = None) -> str:
    from metabot.cron import CronError

    try:
        return next_due(record, now or datetime.now(UTC)).strftime("%a %Y-%m-%d %H:%M %Z")
    except (CronError, KeyError, TypeError, ValueError):
        return "-"
