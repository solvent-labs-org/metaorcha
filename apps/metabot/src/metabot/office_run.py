"""Run an office's desks headless, each desk in its own worktree.

A relay runs desks in order. Each desk works in a worktree on its own branch,
started from the previous desk's branch, so it builds on what came before.
The answers of the desks before it wait as files in ``.metabot/inbox/``. When
a desk ends, MetaBot commits what it changed, and the next desk starts. A
desk that fails, or passes a limit, stops the relay; what it did stays on its
branch.

A desk with ``fan: N`` runs as N copies at once, ``<desk>.1`` to ``<desk>.N``,
each from the same start on its own branch. The next desk starts where the
copies started, with every good copy's answer in its inbox and their branches
in its prompt, and picks between them; so can the person, with ``desks
pick``. A failed copy is kept and reported, and the relay goes on while at
least one copy of the desk did its work.

A fan runs every chosen desk at once from one base, with nothing handed
between them, so the person can compare their work side by side.

An advisor desk (``advises:``) never takes a turn of its own. In a relay,
once a desk it advises has done its work, the advisor reviews that work,
read-only, in a worktree with no branch: ``<advisor>.on-<desk>``. Its advice
goes into the next desks' inboxes and to the person, and it files nothing:
no commit, nothing for ``desks pick``, and whatever it changed in its
worktree is put back. An advisor of ``you`` reviews the office's work once
every desk is done, and its advice goes to the person only. Advisors do not
run in a fan.

With ``checks``, the office's gate checks run on each desk's work once the
desk ends with an answer: its work is committed first, the checks judge that
commit, and the worktree goes back to it afterwards, so nothing a check
leaves becomes the desk's work. They are sealed as the desk's checks run,
``<desk run>.checks`` (see ``metabot.checks``). A desk whose checks fail is
not ok, so a relay stops there.

A desk with a ``kit`` gets its skills as a plugin for its runs (see
``metabot.kits``). Every running desk's kit is found and copied before any
desk starts, so a kit that cannot load refuses the office run, and the
copies go when it ends.

Desk runs going at once never number more than this machine's ``fan_cap``.
Every approval prompt names the desk that asks; a terminal shows them one at
a time.

When an office run ends, the person is offered the desk runs whose branches
hold its work, for ``desks pick``. A run that ended in error is not offered,
nor one whose branch is still at the base, nor one whose commit another
offered branch holds. A relay's last desk holds what came before it, so it
is the one offered, unless copies did work that no later desk took up.

Every desk run is an ordinary run with its own receipt. Its run id is
``<office run>.<desk>``, or ``<office run>.<desk>.<n>`` for a copy, so each
signed receipt names the office run it belongs to and still verifies alone.
Its ``parent`` is the run id of the desk before it, or the good copies' run
ids joined by commas. ``<METABOT_HOME>/offices/<office run>.json`` lists the
desks in order, for ``receipts show``. A caller that must know the office run
id before the run starts, as ``metabot ui`` must, may give it; an id already in
use is refused.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
from collections.abc import Callable, Coroutine, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from emerge.journal import utc_now

from metabot.approvals import Approver, Labelled, printable
from metabot.checks import CheckError, ChecksCancelled, run_gate, runnable
from metabot.config import Config, load_config, project_receipts_on
from metabot.connections import ConnectionStore
from metabot.desks import (
    HANDOFF_DIR,
    DeskGitError,
    advisor_label,
    commit_desk,
    create_worktree,
    desk_branch,
    desk_label,
    diff_stat,
    is_ancestor,
    resolve_commit,
    restore_worktree,
)
from metabot.engines import get_engine
from metabot.events import Event, Notice
from metabot.gate import Policy
from metabot.gitcmd import GitError, toplevel
from metabot.kits import KitError, build_kit, kits_root
from metabot.office import (
    YOU,
    DeskSpec,
    OfficeError,
    OfficeSpec,
    desk_engine,
    desk_problem,
    fan_cap_of,
)
from metabot.paths import (
    metabot_home,
    new_run_id,
    private_dir,
    receipts_dir,
    run_path,
    safe_id,
    write_private,
)
from metabot.receipts import journal_matches, scrub, verify_file
from metabot.runner import Limits, RunCancelled, run_job

OFFICE_FORMAT = "metabot.office/1"
ROLE_LIMIT = 64 * 1024
ARTEFACT = "artefact.md"


@dataclass
class DeskResult:
    desk: str
    copy: int | None
    run_id: str
    branch: str | None  # None for an advisor: it files nothing
    engine: str
    model: str | None
    ok: bool = False
    error: str | None = None
    receipt: Path | None = None
    commit: str | None = None
    stat: tuple[int, int, int] | None = None
    artefact: Path | None = None
    checks: str | None = None  # pass, or fail naming each check; None when none ran
    checks_receipt: Path | None = None  # the checks' own run, <run_id>.checks
    advises: str | None = None  # the desk this advisor reviewed, or you; None for a desk
    answer: str = ""

    @property
    def label(self) -> str:
        if self.advises is not None:
            return advisor_label(self.desk, self.advises)
        return desk_label(self.desk, self.copy)

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("answer")
        data["label"] = self.label
        data["receipt"] = str(self.receipt) if self.receipt else None
        data["artefact"] = str(self.artefact) if self.artefact else None
        data["checks_receipt"] = str(self.checks_receipt) if self.checks_receipt else None
        data["stat"] = list(self.stat) if self.stat else None
        return data


@dataclass
class OfficeResult:
    office_run_id: str
    mode: str
    rows: list[DeskResult] = field(default_factory=list)
    checks_skipped: dict[str, str] = field(default_factory=dict)
    project_copies: list[Path] = field(default_factory=list)  # receipts copied into the project
    project_errors: list[str] = field(default_factory=list)  # why some were not
    offer: list[str] = field(default_factory=list)  # labels of the runs to pick from

    @property
    def ok(self) -> bool:
        return bool(self.rows) and all(row.ok for row in self.rows)


def office_journal_path(office_run_id: str) -> Path:
    return metabot_home() / "offices" / f"{safe_id(office_run_id, 'office run id')}.json"


def _save(record: dict[str, Any], rows: list[DeskResult]) -> None:
    record["desks"] = [row.to_json() for row in rows]
    private_dir(metabot_home() / "offices")
    path = office_journal_path(record["office_run_id"])
    write_private(path, json.dumps(record, indent=2) + "\n")


def _claim(record: dict[str, Any]) -> None:
    """Write the office run's first record only if its id is free, so two runs never share one."""
    office_run_id = record["office_run_id"]
    private_dir(metabot_home() / "offices")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    try:
        fd = os.open(office_journal_path(office_run_id), flags, 0o600)
    except FileExistsError:
        raise OfficeError("office run id", f"office run {office_run_id} exists already") from None
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({**record, "desks": []}, indent=2) + "\n")


def chosen_desks(
    office: OfficeSpec,
    desks: Sequence[str] | None,
    roster: Mapping[str, Mapping[str, str]] | None = None,
) -> list[str]:
    """The desks a relay runs, in order: every one that can run, refused as a whole."""
    names = (
        list(desks)
        if desks is not None
        else [n for n, d in office.desks.items() if not d.is_advisor]
    )
    if not names:
        raise OfficeError("desks", "no desk to run")
    if len(set(names)) != len(names):
        raise OfficeError("desks", "a desk is named twice")
    unknown = [n for n in names if n not in office.desks]
    if unknown:
        raise OfficeError("desks", f"no desk named {', '.join(map(repr, unknown))}")
    advisors = [n for n in names if office.desks[n].is_advisor]
    if advisors:
        raise OfficeError(
            "desks",
            f"{', '.join(map(repr, advisors))}: an advisor runs after the desks it advises, "
            "not on its own",
        )
    problems = [f"{n}: {p}" for n in names if (p := desk_problem(office.desks[n], roster))]
    if problems:
        raise OfficeError("desks", "cannot run here: " + "; ".join(problems))
    return names


def _role_text(office: OfficeSpec, desk: DeskSpec) -> str:
    if desk.role_file is None:
        return ""
    path = desk.role_file.resolve()
    if not path.is_relative_to(office.top) or not path.is_file():
        raise OfficeError(f"desks.{desk.name}.role", "is no longer a file inside the project")
    data = path.read_bytes()[: ROLE_LIMIT + 1]
    if len(data) > ROLE_LIMIT:
        raise OfficeError(f"desks.{desk.name}.role", f"larger than {ROLE_LIMIT} bytes")
    return data.decode("utf-8", errors="replace").strip()


def desk_cwd(wt: Path, office: OfficeSpec, repo: Path, desk: DeskSpec) -> Path:
    """Where a desk works: its cwd, inside its worktree and nowhere else."""
    inside = wt / office.root.relative_to(repo) / desk.cwd
    resolved = inside.resolve()
    if not resolved.is_relative_to(wt.resolve()):
        raise OfficeError(f"desks.{desk.name}.cwd", "leads outside the desk's worktree")
    if not resolved.is_dir():
        raise OfficeError(
            f"desks.{desk.name}.cwd",
            "is not a folder in the committed tree; commit the office folder first",
        )
    return resolved


def handoff(wt: Path, earlier: Sequence[tuple[str, str]]) -> list[Path]:
    """Write the earlier desks' answers into ``.metabot/inbox/``; their paths."""
    if not earlier:
        return []
    folder = wt / HANDOFF_DIR
    inbox = folder / "inbox"
    for part in (folder, inbox):
        if part.is_symlink() or (part.exists() and not part.is_dir()):
            raise OfficeError("handoff", f"{part} is not a plain folder; nothing was handed on")
        part.mkdir(exist_ok=True)
    paths = []
    for label, answer in earlier:
        target = inbox / f"{label}.md"
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise OfficeError("handoff", f"{target} is not a plain file; nothing was handed on")
        target.write_text(answer, encoding="utf-8")
        paths.append(target)
    return paths


def _kit_line(desk: DeskSpec) -> str:
    if not desk.kit:
        return ""
    names = ", ".join(dict.fromkeys(desk.kit))
    return (
        f"Your kit is loaded for this run: {names}. These are skills: load one before you "
        "follow it."
    )


def desk_prompt(
    office: OfficeSpec,
    desk: DeskSpec,
    task: str,
    inbox: Sequence[Path],
    branch: str,
    copy: int | None = None,
    picks: Sequence[str] = (),
) -> str:
    parts = [_role_text(office, desk)]
    parts.append(f"Task:\n{task.strip()}")
    if inbox:
        listed = "\n".join(f"- {path}" for path in inbox)
        parts.append(
            "The desks before you, and their advisors, left their answers in these files. "
            f"Read them first:\n{listed}"
        )
    if picks:
        listed = "\n".join(f"- {pick}" for pick in picks)
        parts.append(
            "The desk before you ran as copies, each with its work on its own branch:\n"
            f"{listed}\nCompare their work, for example with: git diff HEAD <branch>. "
            "Build on the best one, and say which you chose."
        )
    if copy is not None:
        parts.append(
            f"You are one of {desk.fan} copies of the {desk.name} desk. Each copy works on "
            "the same task on its own branch, and the next desk or the person picks one."
        )
    parts.append(_kit_line(desk))
    parts.append(
        f"You are the {desk.name} desk of the {office.name} office, working in a git worktree "
        f"on the branch {branch}. Do not switch branches, commit or push: when you finish, "
        "your changes are committed for you. End with a short answer saying what you did."
    )
    return "\n\n".join(part for part in parts if part)


def advisor_prompt(
    office: OfficeSpec,
    desk: DeskSpec,
    task: str,
    inbox: Sequence[Path],
    advises: str,
    since: str,
    picks: Sequence[str] = (),
) -> str:
    """What an advisor is asked: review the work of the desk it advises, or the office's."""
    parts = [_role_text(office, desk), f"Task:\n{task.strip()}"]
    if inbox:
        listed = "\n".join(f"- {path}" for path in inbox)
        parts.append(
            "The desks before you, and their advisors, left their answers in these files. "
            f"Read them first:\n{listed}"
        )
    if picks:
        listed = "\n".join(f"- {pick}" for pick in picks)
        parts.append(
            f"The work to review ran as copies, each on its own branch:\n{listed}\n"
            "See what each changed with: git diff HEAD <branch>"
        )
    else:
        parts.append(
            "The work to review is this worktree's HEAD. "
            f"See what it changed with: git diff {since} HEAD"
        )
    parts.append(_kit_line(desk))
    whose = "the person running the office" if advises == YOU else f"the {advises} desk"
    parts.append(
        f"You are the {desk.name} advisor of the {office.name} office, and you advise {whose}. "
        "Review the work: read it, and run only what leaves it as it is. You file nothing: you "
        "are read-only, and whatever you change here is put back. End with your advice: what "
        "is wrong or missing, and what to do about it."
    )
    return "\n\n".join(part for part in parts if part)


@dataclass
class _Office:
    """What every desk run of one office run shares."""

    office: OfficeSpec
    task: str
    repo: Path
    base: str
    names: list[str]
    approver: Approver
    allow_write: bool
    allows: Sequence[str]
    roster: Mapping[str, Mapping[str, str]] | None
    connections: bool
    on_event: Callable[[Event], None] | None
    on_desk: Callable[[DeskResult], None] | None
    make: Callable[[str], Any]
    cfg: Config
    chosen: list[Any]
    pins: dict[str, Any]
    store: ConnectionStore
    slots: asyncio.Semaphore
    result: OfficeResult
    record: dict[str, Any]
    checks: dict[str, str] = field(default_factory=dict)  # trusted, and runnable headless
    checks_skipped: dict[str, str] = field(default_factory=dict)
    advisors: dict[str, list[str]] = field(default_factory=dict)  # desk or you: its advisors
    project: Path | None = None  # the checkout that takes a copy of each receipt
    kits: dict[str, Path] = field(default_factory=dict)  # desk: its kit, as a plugin folder
    git: asyncio.Lock = field(default_factory=asyncio.Lock)  # one git step at a time


def _advisors(
    office: OfficeSpec, names: Sequence[str], roster: Mapping[str, Mapping[str, str]] | None
) -> dict[str, list[str]]:
    """Whom each advisor reviews in this relay: the chosen desks, then you.

    An advisor that cannot run here refuses the run before anything starts,
    as a desk does; ``--no-advisors`` runs the desks without them.
    """
    found: dict[str, list[str]] = {}
    for who in (*names, YOU):
        mine = [n for n, d in office.desks.items() if who in d.advises]
        if mine:
            found[who] = mine
    running = dict.fromkeys(n for mine in found.values() for n in mine)
    problems = [f"{n}: {p}" for n in running if (p := desk_problem(office.desks[n], roster))]
    if problems:
        raise OfficeError(
            "advisors",
            "cannot run here: " + "; ".join(problems) + "; or run the desks without them: "
            "--no-advisors",
        )
    return found


@dataclass(frozen=True)
class Start:
    """Where an office run that can start begins, and what it runs."""

    repo: Path
    base: str
    names: list[str]
    advisors: dict[str, list[str]]  # desk or you: its advisors
    checks: dict[str, str]
    checks_skipped: dict[str, str]


def check_start(
    office: OfficeSpec,
    task: str,
    *,
    desks: Sequence[str] | None,
    roster: Mapping[str, Mapping[str, str]] | None,
    checks: bool = False,
    advisors: bool = False,
) -> Start:
    """Whether an office run can start, found without starting it; OfficeError says why not.

    Every office run checks this first. ``metabot ui`` checks it too, so a
    form that cannot start is refused before a run is opened.
    """
    if not task.strip():
        raise OfficeError("task", "the task is empty")
    repo = toplevel(office.root)
    if repo is None:
        raise OfficeError("office", "an office run needs the office inside a git repository")
    names = chosen_desks(office, desks, roster)
    advisor_map = _advisors(office, names, roster) if advisors else {}
    run_checks: dict[str, str] = {}
    skipped: dict[str, str] = {}
    if checks:
        try:
            run_checks, skipped = runnable(office)
        except CheckError as exc:
            raise OfficeError("checks", str(exc)) from None
        if not run_checks:
            raise OfficeError(
                "checks",
                "no check can run headless: each needs {evidence}, which a person names "
                "when delivering"
                if skipped
                else "the office has no checks to run",
            )
    try:
        base = resolve_commit(repo, "HEAD")
    except DeskGitError:
        raise OfficeError(
            "office", "the repository has no commit to start the desks from"
        ) from None
    return Start(repo, base, names, advisor_map, run_checks, skipped)


def _open(
    office: OfficeSpec,
    task: str,
    mode: str,
    *,
    approver: Approver,
    desks: Sequence[str] | None,
    allow_write: bool,
    allows: Sequence[str],
    roster: Mapping[str, Mapping[str, str]] | None,
    connections: bool,
    plugins: bool,
    on_event: Callable[[Event], None] | None,
    on_desk: Callable[[DeskResult], None] | None,
    make_engine: Callable[[str], Any] | None,
    checks: bool = False,
    advisors: bool = False,
    office_run_id: str | None = None,
) -> _Office:
    """Check the office run can start, and write its record."""
    from metabot.plugins import enabled_plugins

    start = check_start(office, task, desks=desks, roster=roster, checks=checks, advisors=advisors)
    if office_run_id is None:
        office_run_id = new_run_id()
    office_journal_path(office_run_id)  # an unsafe id is refused before any work
    cfg = load_config()
    chosen = enabled_plugins() if plugins else []
    store = ConnectionStore()
    record: dict[str, Any] = {
        "format": OFFICE_FORMAT,
        "office_run_id": office_run_id,
        "office": office.name,
        "root": str(office.root),
        "repo": str(start.repo),
        "mode": mode,
        "task": scrub(task, store.redaction_map()),
        "base": start.base,
        "checks": sorted(start.checks),
        "checks_skipped": start.checks_skipped,
        "advisors": start.advisors,
        "started_at": utc_now(),
        "finished_at": None,
    }
    result = OfficeResult(office_run_id, mode, checks_skipped=start.checks_skipped)
    _claim(record)
    try:
        advising = (n for mine in start.advisors.values() for n in mine)
        kit_dirs, kits = _load_kits(office, [*start.names, *advising], office_run_id)
        if kits:
            record["kits"] = kits
        _save(record, result.rows)
    except BaseException:  # no run begins: no close removes the kits, and the id is free again
        shutil.rmtree(kits_root(office_run_id), ignore_errors=True)
        office_journal_path(office_run_id).unlink(missing_ok=True)
        raise
    return _Office(
        office=office,
        task=task,
        repo=start.repo,
        base=start.base,
        names=start.names,
        approver=approver,
        allow_write=allow_write,
        allows=allows,
        roster=roster,
        connections=connections,
        on_event=on_event,
        on_desk=on_desk,
        make=make_engine or get_engine,
        cfg=cfg,
        chosen=chosen,
        pins={"plugins": [p.pin for p in chosen]} if chosen else {},
        store=store,
        slots=asyncio.Semaphore(fan_cap_of(cfg.fan_cap)),
        result=result,
        record=record,
        checks=start.checks,
        checks_skipped=start.checks_skipped,
        advisors=start.advisors,
        project=start.repo if project_receipts_on() else None,
        kits=kit_dirs,
    )


def _load_kits(
    office: OfficeSpec, names: Sequence[str], office_run_id: str
) -> tuple[dict[str, Path], dict[str, list[str]]]:
    """Each running desk's kit as a plugin folder, and its skills; one that cannot load refuses."""
    root = kits_root(office_run_id)
    dirs: dict[str, Path] = {}
    listed: dict[str, list[str]] = {}
    for name in dict.fromkeys(names):
        desk = office.desks[name]
        if not desk.kit:
            continue
        try:
            listed[name] = build_kit(office, desk, root / name)
        except (KitError, OSError) as exc:
            shutil.rmtree(root, ignore_errors=True)
            said = str(exc) if isinstance(exc, KitError) else f"cannot copy it: {exc}"
            raise OfficeError(f"desks.{name}.kit", said) from None
        dirs[name] = root / name
    return dirs, listed


async def _off_loop(
    lock: asyncio.Lock | None, fn: Callable[..., Any], /, *args: Any, **kw: Any
) -> Any:
    """``fn`` in a worker thread, so the page's event loop stays free while git works.

    A stop that comes meanwhile waits for ``fn`` to finish, then stops the run,
    so git is never left half done. ``lock`` keeps an office run's git steps
    one at a time, as they were when they ran on the loop.
    """
    async with lock or contextlib.nullcontext():
        job = asyncio.ensure_future(asyncio.to_thread(fn, *args, **kw))
        try:
            return await asyncio.shield(job)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await job
            raise


async def _opened(*args: Any, **kw: Any) -> _Office:
    """``_open`` off the loop; an office run stopped while it opened is closed, then stopped."""
    job = asyncio.ensure_future(asyncio.to_thread(_open, *args, **kw))
    try:
        return await asyncio.shield(job)
    except asyncio.CancelledError:
        with contextlib.suppress(Exception):
            _close(await job)
        raise


def _close(run: _Office) -> None:
    run.record["finished_at"] = utc_now()
    if run.kits:  # the desks are done with their kits
        shutil.rmtree(kits_root(run.result.office_run_id), ignore_errors=True)
    _save(run.record, run.result.rows)


def _save_so_far(run: _Office) -> None:
    """The record as it stands, so a run killed part way keeps the desks it ran.

    Only the record saved at the close must be written; one that cannot be
    written now is written then.
    """
    with contextlib.suppress(OSError):
        _save(run.record, run.result.rows)


def _copies(desk: DeskSpec) -> list[int | None]:
    return [None] if desk.fan <= 1 else list(range(1, desk.fan + 1))


def _new_row(run: _Office, name: str, copy: int | None, advises: str | None = None) -> DeskResult:
    label = desk_label(name, copy) if advises is None else advisor_label(name, advises)
    engine_name, model = desk_engine(run.office.desks[name], run.roster)
    row = DeskResult(
        desk=name,
        copy=copy,
        run_id=f"{run.result.office_run_id}.{label}",
        branch=None if advises is not None else desk_branch(run.result.office_run_id, label),
        engine=engine_name,
        model=model or run.cfg.models.get(engine_name),
        advises=advises,
    )
    run.result.rows.append(row)
    return row


def _copied(run: _Office, done: Any) -> None:
    """Note a run's project copy of its receipt, or why it has none."""
    if done.project_receipt is not None:
        run.result.project_copies.append(done.project_receipt)
    if done.project_error and done.project_error not in run.result.project_errors:
        run.result.project_errors.append(done.project_error)


def _events_for(on_event: Callable[[Event], None] | None, label: str) -> Any:
    """The trace for one of several desks at once: its lines name the desk."""
    for_run = getattr(on_event, "for_run", None)
    return for_run(label) if callable(for_run) else on_event


async def _run_row(
    run: _Office,
    row: DeskResult,
    start: str,
    earlier: Sequence[tuple[str, str]],
    parent: str | None,
    picks: Sequence[str] = (),
    together: bool = False,
    since: str | None = None,
) -> None:
    """One desk run: its worktree from ``start``, its run, its checks, and its commit.

    An advisor's run (``row.advises``) has a worktree with no branch, runs
    read-only with every write asked and nothing allowed ahead, and files
    nothing; ``since`` is where the work it reviews began.
    """
    from metabot.cmdline import resolve_allows

    desk = run.office.desks[row.desk]
    advisor = row.advises is not None
    began = False
    try:
        async with run.slots:
            try:
                wt = await _off_loop(
                    run.git,
                    create_worktree,
                    *(run.repo, run.result.office_run_id, row.label, start),
                    detached=advisor,
                )
                cwd = desk_cwd(wt, run.office, run.repo, desk)
                inbox = handoff(wt, earlier)
                if row.advises is not None:
                    prompt = advisor_prompt(
                        run.office, desk, run.task, inbox, row.advises, since or run.base, picks
                    )
                else:
                    prompt = desk_prompt(
                        run.office, desk, run.task, inbox, str(row.branch), row.copy, picks
                    )
            except (DeskGitError, OfficeError, OSError) as exc:
                row.error = str(exc)
                return
            _save_so_far(run)
            if run.on_desk is not None:
                run.on_desk(row)
            engine = run.make(row.engine)
            policy = Policy(
                allow_write=run.allow_write and not advisor,
                allows=frozenset()
                if advisor
                else resolve_allows(list(run.allows), engine, run.store),
            )
            began = True
            events = _events_for(run.on_event, row.label) if together else run.on_event
            try:
                result = await run_job(
                    engine=engine,
                    prompt=prompt,
                    cwd=cwd,
                    policy=policy,
                    approver=Labelled(run.approver, row.label),
                    model=row.model,
                    plugin_dirs=[p.view for p in run.chosen]
                    + ([run.kits[row.desk]] if row.desk in run.kits else []),
                    switchboard=run.connections and bool(run.store.active()),
                    on_event=events,
                    run_id=row.run_id,
                    meta={
                        "office": run.result.office_run_id,
                        "office_name": run.office.name,
                        "desk": row.desk,
                        "copy": row.copy,
                        "parent": parent,
                        **({"advises": row.advises} if advisor else {}),
                        **run.pins,
                    },
                    limits=Limits(max_tool_calls=desk.turns, max_tokens=desk.budget),
                    read_only=advisor,
                    project=run.project,
                )
            except RunCancelled as exc:
                if advisor:
                    await _off_loop(run.git, _finish_advice, row, exc.result, wt)
                else:
                    await _off_loop(
                        run.git, _finish_desk, row, exc.result, wt, run.task, run.repo, start
                    )
                row.error = "cancelled"
                raise
            except Exception as exc:  # an engine adapter broke: its run sealed what it saw
                row.error = printable(f"{type(exc).__name__}: {exc}", 300)
                sealed = receipts_dir() / f"{row.run_id}.json"
                row.receipt = sealed if sealed.is_file() else None
                raise
            _copied(run, result)
            hint = limit_hint(row.desk, result.limit)
            if hint and events is not None:  # said where the person watches, as it happens
                events(Notice(hint))
            if advisor:
                await _off_loop(run.git, _finish_advice, row, result, wt)
                return
            if not (run.checks and result.ok):
                await _off_loop(run.git, _finish_desk, row, result, wt, run.task, run.repo, start)
                return
            # The checks judge the desk's commit, and what they leave is not its work.
            try:
                row.commit = await _off_loop(
                    run.git, _commit_work, row, wt, run.task, run.repo, start
                )
            except DeskGitError as exc:
                await _off_loop(
                    run.git,
                    _finish_desk,
                    row,
                    result,
                    wt,
                    run.task,
                    run.repo,
                    start,
                    commit_error=str(exc),
                )
                return
            if row.commit is None:  # it changed no file: there is no work of its to judge
                await _off_loop(
                    run.git,
                    _finish_desk,
                    row,
                    result,
                    wt,
                    run.task,
                    run.repo,
                    start,
                    committed=True,
                )
                return
            try:
                gate = await run_gate(
                    run.checks,
                    wt / run.office.root.relative_to(run.repo),
                    desk_run_id=row.run_id,
                    inside=wt,
                    meta={
                        "office": run.result.office_run_id,
                        "office_name": run.office.name,
                        "desk": row.desk,
                        "copy": row.copy,
                        **({"checks_skipped": run.checks_skipped} if run.checks_skipped else {}),
                    },
                    project=run.project,
                )
            except ChecksCancelled as exc:
                row.checks_receipt = exc.result.receipt
                await _off_loop(
                    run.git,
                    _finish_desk,
                    row,
                    result,
                    wt,
                    run.task,
                    run.repo,
                    start,
                    committed=True,
                )
                row.ok, row.error = False, "cancelled"
                raise
            _copied(run, gate)
            row.checks, row.checks_receipt = gate.summary, gate.receipt
            await _off_loop(
                run.git, _finish_desk, row, result, wt, run.task, run.repo, start, committed=True
            )
    except asyncio.CancelledError:
        if not began and row.error is None:
            row.error = "cancelled before it began"
        raise
    finally:
        _save_so_far(run)


async def _together(jobs: Sequence[Coroutine[Any, Any, None]]) -> None:
    """Run desk runs at once; each one ends before any error is raised."""
    if len(jobs) == 1:
        await jobs[0]
        return
    outcomes = await asyncio.gather(*jobs, return_exceptions=True)
    errors = [o for o in outcomes if isinstance(o, BaseException)]
    for error in errors:
        if not isinstance(error, RunCancelled):
            raise error
    if errors:
        raise errors[0]


async def _advise(
    run: _Office,
    who: str,
    start: str,
    since: str,
    earlier: Sequence[tuple[str, str]],
    parent: str | None,
    picks: Sequence[str],
) -> list[tuple[str, str]]:
    """The advisors of ``who`` review its work, at once; the advice of each that gave it.

    ``since`` is where that work began.
    """
    names = run.advisors.get(who, [])
    if not names:
        return []
    since = resolve_commit(run.repo, since)
    rows = [_new_row(run, name, None, advises=who) for name in names]
    together = len(rows) > 1
    await _together(
        [_run_row(run, row, start, earlier, parent, picks, together, since) for row in rows]
    )
    return [(row.label, row.answer) for row in rows if row.ok]


async def run_relay(
    office: OfficeSpec,
    task: str,
    *,
    approver: Approver,
    desks: Sequence[str] | None = None,
    allow_write: bool = False,
    allows: Sequence[str] = (),
    roster: Mapping[str, Mapping[str, str]] | None = None,
    connections: bool = True,
    plugins: bool = True,
    on_event: Callable[[Event], None] | None = None,
    on_desk: Callable[[DeskResult], None] | None = None,
    make_engine: Callable[[str], Any] | None = None,
    checks: bool = False,
    advisors: bool = True,
    office_run_id: str | None = None,
) -> OfficeResult:
    """Run desks one after another; a desk with no good run stops the relay.

    After each desk, its advisors review its work; once every desk is done,
    the advisors of you review the office's work. ``advisors=False`` runs
    the desks alone.
    """
    run = await _opened(
        office, task, "relay", approver=approver, desks=desks, allow_write=allow_write,
        allows=allows, roster=roster, connections=connections, plugins=plugins,
        on_event=on_event, on_desk=on_desk, make_engine=make_engine, checks=checks,
        advisors=advisors, office_run_id=office_run_id,
    )  # fmt: skip
    earlier: list[tuple[str, str]] = []
    start, parent = run.base, None
    picks: tuple[str, ...] = ()
    try:
        for name in run.names:
            began_at = start
            rows = [_new_row(run, name, copy) for copy in _copies(office.desks[name])]
            together = len(rows) > 1
            await _together(
                [_run_row(run, row, start, earlier, parent, picks, together) for row in rows]
            )
            good = [row for row in rows if row.ok]
            if not good:
                break
            earlier.extend((row.label, row.answer) for row in good)
            parent = ",".join(row.run_id for row in good)
            if together:  # the next desk starts where the copies started, and picks
                picks = tuple(str(row.branch) for row in good if row.commit is not None)
            else:
                start, picks = f"refs/heads/{good[0].branch}", ()
            earlier.extend(await _advise(run, name, start, began_at, earlier, parent, picks))
        else:
            await _advise(run, YOU, start, run.base, earlier, parent, picks)
    finally:
        _close(run)
    run.result.offer = _offer(run)
    _keep_offer(run)
    return run.result


async def run_fan(
    office: OfficeSpec,
    task: str,
    *,
    approver: Approver,
    desks: Sequence[str] | None = None,
    allow_write: bool = False,
    allows: Sequence[str] = (),
    roster: Mapping[str, Mapping[str, str]] | None = None,
    connections: bool = True,
    plugins: bool = True,
    on_event: Callable[[Event], None] | None = None,
    on_desk: Callable[[DeskResult], None] | None = None,
    make_engine: Callable[[str], Any] | None = None,
    checks: bool = False,
    office_run_id: str | None = None,
) -> OfficeResult:
    """Run every chosen desk at once from one base; nothing is handed between them.

    Advisors do not run in a fan.
    """
    run = await _opened(
        office, task, "fan", approver=approver, desks=desks, allow_write=allow_write,
        allows=allows, roster=roster, connections=connections, plugins=plugins,
        on_event=on_event, on_desk=on_desk, make_engine=make_engine, checks=checks,
        office_run_id=office_run_id,
    )  # fmt: skip
    try:
        rows = [
            _new_row(run, name, copy) for name in run.names for copy in _copies(office.desks[name])
        ]
        together = len(rows) > 1
        await _together([_run_row(run, row, run.base, (), None, (), together) for row in rows])
    finally:
        _close(run)
    run.result.offer = _offer(run)
    _keep_offer(run)
    return run.result


def _keep_offer(run: _Office) -> None:
    """The offer joins the saved record, so ``metabot ui`` can show what to pick.

    The record saved at the close stands without it.
    """
    run.record["offer"] = list(run.result.offer)
    with contextlib.suppress(OSError):
        _save(run.record, run.result.rows)


def _offer(run: _Office) -> list[str]:
    """The labels of the desk runs whose branches hold the office's work, in run order.

    Of runs whose branches are at one commit, the latest desk's are kept: in a
    relay each desk starts from the branch before it, so the later one holds
    what came before. A run whose commit is in another kept branch's history
    is left out, and so is a run that ended in error or changed nothing.
    """
    at: dict[str, list[DeskResult]] = {}
    for row in run.result.rows:
        if row.branch is None or not row.ok:  # an advisor files nothing
            continue
        try:
            tip = resolve_commit(run.repo, f"refs/heads/{row.branch}")
        except (DeskGitError, GitError):
            continue  # its branch is gone, so there is nothing of it to pick
        if tip != run.base:
            at.setdefault(tip, []).append(row)
    latest = {tip: [r for r in rows if r.desk == rows[-1].desk] for tip, rows in at.items()}
    heads = [
        tip
        for tip in latest
        if not any(other != tip and is_ancestor(run.repo, tip, other) for other in latest)
    ]
    offered = {row.label for tip in heads for row in latest[tip]}
    return [row.label for row in run.result.rows if row.label in offered]


def _join(*errors: str | None) -> str | None:
    return "; ".join(e for e in errors if e) or None


def limit_hint(desk: str, limit: str | None) -> str | None:
    """What to change in the office when a desk's run passed one of its limits."""
    if limit is None:
        return None
    if limit.endswith(" tool calls"):
        return (
            f"raise turns: for the {desk} desk in office.yaml, or give it a model that needs "
            "fewer tool calls"
        )
    if limit.endswith(" tokens"):
        return f"raise budget: for the {desk} desk in office.yaml"
    return None


def _commit_work(row: DeskResult, wt: Path, task: str, repo: Path, start: str) -> str | None:
    """The commit that holds a desk's work, or None when it changed no file.

    A desk may commit its work itself: its branch's tip, once it has moved
    from where the desk began, is its work all the same, and is judged.
    """
    made = commit_desk(wt, row.desk, task, row.run_id, branch=row.branch)
    tip = made or resolve_commit(repo, f"refs/heads/{row.branch}")
    return None if tip == resolve_commit(repo, start) else tip


def _finish_desk(
    row: DeskResult,
    run: Any,
    wt: Path,
    task: str,
    repo: Path,
    start: str,
    *,
    committed: bool = False,
    commit_error: str | None = None,
) -> None:
    """Record a desk's run, keep its answer, and commit what it changed.

    With ``committed``, its work was committed before its checks ran, and
    the worktree goes back to that commit. A desk whose work could not be
    committed, or whose checks failed, is not ok: the relay stops there, and
    a fan does not offer it to pick.
    """
    assert row.branch is not None  # an advisor's row has none, and _finish_advice takes it
    row.receipt, row.ok, row.answer = run.receipt, run.ok, run.final_text
    row.error = _join(run.error, limit_hint(row.desk, run.limit), commit_error)
    row.artefact = run_path(row.run_id) / ARTEFACT
    write_private(row.artefact, run.final_text)
    try:
        if committed:
            restore_worktree(wt)
        elif commit_error is None:
            row.commit = _commit_work(row, wt, task, repo, start)
        row.stat = diff_stat(repo, resolve_commit(repo, start), resolve_commit(repo, row.branch))
    except DeskGitError as exc:
        row.ok = False
        row.error = _join(row.error, str(exc))
    if commit_error is not None:
        row.ok = False
    if row.checks is not None and row.checks != "pass":
        row.ok = False
        row.error = _join(row.error, "the gate's checks failed")


def _finish_advice(row: DeskResult, run: Any, wt: Path) -> None:
    """Record an advisor's run and keep its advice; whatever it changed is put back."""
    row.receipt, row.ok, row.answer = run.receipt, run.ok, run.final_text
    row.error = _join(run.error, limit_hint(row.desk, run.limit))
    row.artefact = run_path(row.run_id) / ARTEFACT
    write_private(row.artefact, run.final_text)
    try:
        restore_worktree(wt)
    except DeskGitError as exc:
        row.ok = False
        row.error = _join(row.error, str(exc))


def load_record(office_run_id: str) -> dict[str, Any] | None:
    try:
        path = office_journal_path(office_run_id)
    except ValueError:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("format") == OFFICE_FORMAT else None


def receipt_state(run_id: str, receipt: str | None) -> str:
    if not receipt:
        return "no receipt"
    if not verify_file(receipt).valid:
        return "INVALID"
    try:
        return "valid" if journal_matches(run_id) else "JOURNAL MISMATCH"
    except ValueError:
        return "INVALID"
