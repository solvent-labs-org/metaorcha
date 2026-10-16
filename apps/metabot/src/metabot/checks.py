"""An office gate's checks, run on a desk's work, behind a per-machine trust record.

Code Desks runs a gate's ``checks:`` command when a person delivers that
item. A headless office run has nobody delivering, so it runs them only
when this machine has agreed to: ``metabot office trust`` shows every
command and records them under ``METABOT_HOME/trust/``, never in the office
folder, where extra keys in Code Desks' own ``.office/trust.yaml`` would
make ``desk observe`` refuse the office. A run is refused while any check
was added, changed or removed since, and the refusal lists the difference.

The checks run in the desk's worktree once the desk's work is committed,
from the office's folder there, and never from a folder that leads out of
the worktree. ``pytest`` there runs code the desk wrote, as the person, so
trusting such a check trusts the desks' code to run on this machine; the
trust prompt says so. The commands come from the office as the person's
checkout has it, never from the desk's worktree, so a desk cannot change
the command that judges it. A check runs only when it is one line of
printable text, so the trust prompt shows every command whole, exactly as
it runs; and a command the shell classifier calls destructive never runs.
Neither runs trusted or not.

``{gate}``, ``{item}``, ``{thread}`` (the desk's run id) and ``{estate}``
(where the check runs) are filled in shell-quoted, as Code Desks does, and
set as ``DESK_GATE``, ``DESK_ITEM``, ``DESK_THREAD`` and ``DESK_ESTATE``. A
check that names ``{evidence}`` needs what a person names when delivering,
so a headless run skips it and says so.

A desk's checks are sealed as their own run, ``<desk run>.checks``, whose
parent is the desk's run. Each check is one step, ``metabot/check``, with
its exit code, stdout and stderr, and the run declares ``exit_zero``. The
platform's one rule judges every step of a run, and every step of this run
is a check, so whoever holds its journal can judge it again and get the
same ``declared_acceptance`` verdict. A check that timed out reports exit
code 124, and one that could not start reports 127; both fail it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shlex
import time
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from emerge.journal import utc_now

from metabot.approvals import printable
from metabot.connections import ConnectionStore
from metabot.engines.base import DROPPED_ENV
from metabot.office import OfficeSpec
from metabot.paths import metabot_home, private_dir, write_private
from metabot.receipts import ensure_key, project_copy, seal_and_store
from metabot.scope_classes import ScopeClass
from metabot.shell import TIMEOUT_EXIT, run_shell, stop_group
from metabot.shell_policy import classify_shell

CHECK_TOOL = "metabot/check"
CHECK_ENGINE = "metabot"  # what the index shows as the engine of a checks run
CHECKS_SUFFIX = ".checks"  # a desk's run id, then this: desk names never hold a dot
CANNOT_START_EXIT = 127  # what sh reports for a command it could not run
CHECK_TIMEOUT_S = 600.0
CHECK_OUTPUT_LIMIT = 16_000
CHECK_CRITERIA = {"exit_zero": True}
EVIDENCE = "{evidence}"
NEEDS_EVIDENCE = "needs {evidence}, which a person names when delivering"
TRUST_FORMAT = "metabot.trust/1"
DESTRUCTIVE = "destructive"
NOT_ONE_LINE = "not one line of printable text"


class CheckError(ValueError):
    pass


def shown(text: str) -> str:
    """A name or a command for the terminal: whole, with nothing in it that is not printable."""
    return printable(text, len(text))


def trust_path(office: OfficeSpec) -> Path:
    digest = hashlib.sha256(str(office.root).encode("utf-8")).hexdigest()[:16]
    return metabot_home() / "trust" / f"{digest}.json"


def _trust_folder(path: Path) -> None:
    """The trust folder is a real directory, and a record is a real file."""
    folder = path.parent
    if folder.is_symlink() or (folder.exists() and not folder.is_dir()):
        raise CheckError(f"{folder} is not a trust folder; remove it and run: metabot office trust")
    if path.is_symlink():
        raise CheckError(f"{path} does not read; delete it and run: metabot office trust")


def recorded(office: OfficeSpec) -> dict[str, str] | None:
    """The checks this machine agreed to run for this office, or None before any."""
    path = trust_path(office)
    _trust_folder(path)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    commands = data.get("commands") if isinstance(data, dict) else None
    if not isinstance(commands, dict) or data.get("office") != str(office.root):
        raise CheckError(f"{path} does not read; delete it and run: metabot office trust")
    return {str(k): str(v) for k, v in commands.items()}


@dataclass
class TrustDiff:
    added: dict[str, str] = field(default_factory=dict)
    removed: dict[str, str] = field(default_factory=dict)
    changed: dict[str, tuple[str, str]] = field(default_factory=dict)  # key: (recorded, now)

    @property
    def empty(self) -> bool:
        return not (self.added or self.removed or self.changed)

    def lines(self) -> list[str]:
        out = [f"  + {shown(k)}: {shown(v)}" for k, v in self.added.items()]
        for k, (old, new) in self.changed.items():
            out += [f"  ~ {shown(k)}: {shown(old)}", f"    now: {shown(new)}"]
        out += [
            f"  - {shown(k)}: {shown(v)}  (no longer in office.yaml)"
            for k, v in self.removed.items()
        ]
        return out


def trust_diff(office: OfficeSpec) -> TrustDiff:
    """The office's checks now, against what this machine agreed to."""
    now, before = office.checks(), recorded(office) or {}
    diff = TrustDiff()
    for key, command in now.items():
        if key not in before:
            diff.added[key] = command
        elif before[key] != command:
            diff.changed[key] = (before[key], command)
    for key, command in before.items():
        if key not in now:
            diff.removed[key] = command
    return diff


def refused(checks: Mapping[str, str]) -> dict[str, str]:
    """The checks that never run, trusted or not, each with why."""
    out: dict[str, str] = {}
    for key, command in checks.items():
        if not command.isprintable():
            out[key] = NOT_ONE_LINE
        elif classify_shell(command) is ScopeClass.DESTRUCTIVE:
            out[key] = DESTRUCTIVE
    return out


def refusal(checks: Mapping[str, str]) -> str | None:
    """Why some of these checks never run, one line each; None when every one may."""
    bad = refused(checks)
    if not bad:
        return None
    lines = [f"  ! {shown(k)}: {shown(checks[k])}  ({why})" for k, why in bad.items()]
    return "these checks never run, trusted or not; change them in office.yaml:\n" + "\n".join(
        lines
    )


def _refuse(checks: Mapping[str, str]) -> None:
    message = refusal(checks)
    if message is not None:
        raise CheckError(message)


def record_trust(office: OfficeSpec) -> Path:
    """Record the office's checks as the ones this machine agrees to run."""
    checks = office.checks()
    _refuse(checks)
    path = trust_path(office)
    _trust_folder(path)
    private_dir(path.parent)
    record = {
        "format": TRUST_FORMAT,
        "office": str(office.root),
        "commands": checks,
        "recorded_at": utc_now(),
    }
    write_private(path, json.dumps(record, indent=2, sort_keys=True) + "\n")
    return path


def runnable(office: OfficeSpec) -> tuple[dict[str, str], dict[str, str]]:
    """The checks a headless run runs, and those it skips with why; CheckError when it may not."""
    checks = office.checks()
    if not checks:
        return {}, {}
    _refuse(checks)
    diff = trust_diff(office)
    if not diff.empty:
        raise CheckError(
            "these checks are not trusted on this machine; read them, then run: "
            "metabot office trust\n" + "\n".join(diff.lines())
        )
    run = {k: v for k, v in checks.items() if EVIDENCE not in v}
    skipped = {k: NEEDS_EVIDENCE for k, v in checks.items() if EVIDENCE in v}
    return run, skipped


def render(template: str, values: Mapping[str, str]) -> str:
    """Fill each ``{name}`` with its value, shell-quoted."""
    out = template
    for name, value in values.items():
        out = out.replace("{" + name + "}", shlex.quote(value))
    return out


def check_env(values: Mapping[str, str]) -> dict[str, str]:
    """The person's environment for a check, with Code Desks' ``DESK_*`` values set.

    A headless run has no evidence, so ``DESK_EVIDENCE`` is never passed on
    from the shell that started it.
    """
    env = {k: v for k, v in os.environ.items() if k not in DROPPED_ENV and k != "DESK_EVIDENCE"}
    env.update({f"DESK_{name.upper()}": value for name, value in values.items()})
    return env


def _folder(cwd: Path, inside: Path | None) -> Path | None:
    """Where a check runs: ``cwd`` resolved, when it is a folder inside ``inside``."""
    where = cwd.resolve()
    if inside is not None and not where.is_relative_to(inside.resolve()):
        return None
    return where if where.is_dir() else None


def _cannot_start(reason: str) -> dict[str, Any]:
    return {
        "exit_code": CANNOT_START_EXIT,
        "stdout": "",
        "stderr": f"metabot could not start the check: {reason}",
    }


async def check_steps(
    checks: Mapping[str, str],
    cwd: Path,
    thread: str,
    timeout: float | None = None,
    inside: Path | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Run each check in ``cwd``, one after another; one step each.

    With ``inside``, a check whose folder leads out of it does not run: the
    folder is looked at again before every check, since a check can run
    code that changes it.
    """
    timeout = CHECK_TIMEOUT_S if timeout is None else timeout
    for n, (key, template) in enumerate(checks.items(), 1):
        gate, _, item = key.partition(":")
        values = {"gate": gate, "item": item, "thread": thread, "estate": str(cwd)}
        command = render(template, values)
        began = time.monotonic()
        output: dict[str, Any]
        where = _folder(cwd, inside)
        if where is None:
            output = _cannot_start("the office folder is not a folder inside the desk's worktree")
        else:
            try:
                done = await run_shell(
                    command, where, timeout, limit=CHECK_OUTPUT_LIMIT, env=check_env(values)
                )
            except OSError as exc:  # the folder went, or the shell cannot start
                output = _cannot_start(exc.strerror or type(exc).__name__)
            else:
                if done.left_running:  # a check leaves nothing running behind it
                    await stop_group(done.pgid)
                code = TIMEOUT_EXIT if done.timed_out else done.exit_code
                output = {"exit_code": code, "stdout": done.stdout, "stderr": done.stderr}
                if done.timed_out:
                    output["timed_out"] = True
        yield {
            "call_id": f"check-{n}",
            "tool": CHECK_TOOL,
            "args": {"gate": gate, "item": item, "command": command},
            "output": output,
            "success": output["exit_code"] == 0,
            "latency_ms": int((time.monotonic() - began) * 1000),
        }


def summary(steps: list[dict[str, Any]]) -> str | None:
    """``pass``, or ``fail`` naming each check that did not exit 0; None with no checks."""
    if not steps:
        return None
    failed = [
        f"{s['args']['gate']}:{s['args']['item']} "
        + ("timed out" if s["output"].get("timed_out") else f"exit {s['output']['exit_code']}")
        for s in steps
        if not s["success"]
    ]
    return "fail: " + ", ".join(failed) if failed else "pass"


@dataclass(frozen=True)
class GateRun:
    """A desk's checks, sealed as their own run."""

    run_id: str
    receipt: Path
    summary: str | None  # pass, or fail naming each check; None when cancelled first
    project_receipt: Path | None = None  # its copy in the project, when one was asked for
    project_error: str | None = None


class ChecksCancelled(asyncio.CancelledError):
    """The checks were cancelled; what ran was still sealed, with no verdict."""

    def __init__(self, result: GateRun) -> None:
        super().__init__("cancelled")
        self.result = result


async def run_gate(
    checks: Mapping[str, str],
    cwd: Path,
    *,
    desk_run_id: str,
    inside: Path | None = None,
    meta: Mapping[str, Any] | None = None,
    timeout: float | None = None,
    project: Path | None = None,
) -> GateRun:
    """Run a desk's checks in ``cwd`` and seal them as ``<desk run>.checks``.

    The run declares ``exit_zero``. With ``inside`` (the desk's worktree), a
    check never runs from a folder that leads out of it. A cancel stops the
    check that is running and seals the checks that ran with no verdict,
    since not every check ran; then ChecksCancelled carries the receipt.
    With ``project``, the receipt is also copied into that folder's project.
    """
    run_id = desk_run_id + CHECKS_SUFFIX
    started_at = utc_now()
    steps: list[dict[str, Any]] = []
    cancelled = False
    try:
        async for step in check_steps(checks, cwd, desk_run_id, timeout, inside):
            steps.append(step)
    except asyncio.CancelledError:
        cancelled = True
    outcome = None if cancelled else summary(steps)
    status = "cancelled" if cancelled else ("ok" if outcome == "pass" else "error")
    key, _ = ensure_key()
    receipt = seal_and_store(
        run_id=run_id,
        steps=steps,
        verdicts=[],
        started_at=started_at,
        finished_at=utc_now(),
        key=key,
        secrets=ConnectionStore().redaction_map(),
        criteria=None if cancelled else CHECK_CRITERIA,
        meta={
            **(meta or {}),
            "parent": desk_run_id,
            "engine": CHECK_ENGINE,
            "status": status,
            "prompt": "the gate's checks: " + ", ".join(checks),
            "cwd": str(cwd),
            "checks": outcome,
        },
    )
    result = GateRun(run_id, receipt, outcome, *project_copy(receipt, project))
    if cancelled:
        raise ChecksCancelled(result)
    return result
