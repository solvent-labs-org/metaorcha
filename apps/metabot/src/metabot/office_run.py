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

A desk with ``read_only: true`` takes its turn as any desk does, but the way
an advisor runs: read-only, in a worktree with no branch, with every write
asked. Its answer goes into the next desks' inboxes, and the next desk is
its child; it files nothing, so the next desk starts where it started.

With ``checks``, the office's gate checks run on each desk's work once the
desk ends with an answer: its work is committed first, the checks judge that
commit, and the worktree goes back to it afterwards, so nothing a check
leaves becomes the desk's work. They are sealed as the desk's checks run,
``<desk run>.checks`` (see ``metabot.checks``). A desk whose checks fail is
not ok, so a relay stops there.

With ``rounds`` above one (``--until-checks``), a desk whose checks fail
goes again, up to that many rounds: in the same worktree, on the same branch,
from the commit its last round made, with what the failed checks said in its
inbox as ``<desk>.checks.r<n>.md``. Round n's run id is ``<desk run>.r<n>``
and its parent is the round before. When the run's one desk that may write,
run as one copy, changes no file in round 1, the checks judge the work as it
stands, so a desk that claims work it did not do goes again. The loop stops
when the checks pass, when the rounds run out, when a later round changes no
file, or when the office's money cap is spent; a run that ends in an error,
at a limit or in a stop, or that had a call declined, is never tried again.
Only a desk's last round counts: the next desk starts from it, and it alone
may be offered.

With ``max_cost_musd``, every desk run of the office run, each round and
each advisor, runs under what is left of that one cap, one run at a time,
and a desk whose engine MetaBot cannot price refuses the run before it
starts. A run whose tokens have no price, or whose engine reported no usage
MetaBot could read, has an unknown cost, so the cap cannot be kept: nothing
more runs under it. A run that never reached its model cost nothing.

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
import contextvars
import functools
import json
import os
import shutil
from collections.abc import Callable, Coroutine, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from emerge.journal import utc_now

from metabot.approvals import Approver, Labelled, printable
from metabot.checks import (
    CHECKS_SUFFIX,
    CheckError,
    ChecksCancelled,
    feedback_text,
    run_gate,
    runnable,
)
from metabot.config import Config, load_config, project_receipts_on
from metabot.connections import ConnectionStore, record_secrets
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
    worktree_path,
)
from metabot.engines import get_engine
from metabot.events import ErrorEvent, Event, Notice
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
    kept_home,
    metabot_home,
    new_run_id,
    private_dir,
    receipts_dir,
    run_path,
    safe_id,
    write_private,
)
from metabot.receipts import (
    budget_verdict,
    declined_and_failed,
    journal_matches,
    project_copy,
    provenance_verdicts,
    scrub,
    signed_cost,
    verdicts_of,
    verify_file,
)
from metabot.runner import Limits, RunCancelled, run_job, unpriced
from metabot.seals import Member, seal_runs
from metabot.spend import dollars, uncounted
from metabot.steps import ascii_text

OFFICE_FORMAT = "metabot.office/1"
ROLE_LIMIT = 64 * 1024
ARTEFACT = "artefact.md"
# how a desk's answer is written down: a name in it whose bytes are not UTF-8 is kept
# escaped, as `ask` prints it, and never stops the office
SHOWN = "backslashreplace"
ROUNDS_DEFAULT = 3  # what --until-checks gives a desk when --rounds is not said
ROUNDS_MAX = 10


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
    read_only: bool = False  # a read-only desk: it files nothing, as an advisor does
    round: int | None = None  # its round, once its desk ran more than one; else None
    next_round: str | None = None  # the run id of the round after it
    stopped: str | None = None  # why its desk's loop ended here, on a loop's last round
    cost_musd: int | None = None  # what its run cost; None when it has no price
    declined: int = 0  # the calls of its run the gate declined
    answer: str = ""
    feedback: str = ""  # what its failed checks said, for the next round

    @property
    def label(self) -> str:
        if self.advises is not None:
            return advisor_label(self.desk, self.advises)
        return desk_label(self.desk, self.copy)

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("answer")
        data.pop("feedback")
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
    seal: Path | None = None  # the receipt over every run of it; None if it could not be signed
    cost_unknown: list[str] = field(default_factory=list)  # runs under the cap it cannot count
    record_error: str | None = None  # why the office run's record could not be written

    @property
    def ok(self) -> bool:
        last = [row for row in self.rows if row.next_round is None]  # a loop's last rounds
        return bool(last) and all(row.ok for row in last)


def office_journal_path(office_run_id: str) -> Path:
    """An office run's record, for a lookup: nothing is created; a writer makes the folder."""
    return kept_home() / "offices" / f"{safe_id(office_run_id, 'office run id')}.json"


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
        target.write_text(answer, encoding="utf-8", errors=SHOWN)
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
    read_only: bool = False,
    round_of: tuple[int, int, Path] | None = None,
    changed_before: bool = True,
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
    if read_only:
        parts.append(
            f"You are the {desk.name} desk of the {office.name} office, working read-only in "
            "a git worktree: read what you need, and change nothing, for nothing you change is "
            "kept. End with a short answer: it goes to the desks after you."
        )
        return "\n\n".join(part for part in parts if part)
    if round_of is not None:
        n, of, said = round_of
        before = (
            "Your work from the round before is on this branch, and the gate's checks failed on it"
            if changed_before
            else "Your round before changed no file, and the gate's checks fail on the work "
            "as it stands"
        )
        parts.append(
            f"This is round {n} of {of}. {before}; what they said is in {said}. Read it, and "
            "fix the work, not the checks: do not change the checks, the office file, or "
            "the tests they run."
        )
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
    rounds: int = 1  # the most rounds a desk whose checks fail runs
    cap_musd: int | None = None  # the office run's money cap, over every desk run
    spent_musd: int = 0
    uncounted: str | None = None  # why the first run under the cap has no cost


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


def _priced(
    office: OfficeSpec, names: Sequence[str], roster: Mapping[str, Mapping[str, str]] | None
) -> None:
    """Under a money cap, each desk that runs needs a price; OfficeError names one without."""
    from metabot.prices import price_for, read_prices

    cfg = load_config()
    mine = read_prices(cfg.prices)
    for name in dict.fromkeys(names):
        engine, model = desk_engine(office.desks[name], roster)
        model = model or cfg.models.get(engine)
        base_url = cfg.openai_base_url if engine == "openai" else None
        if unpriced(price_for(engine, model, base_url, mine), engine, model):
            raise OfficeError(
                "max_cost",
                f"desk {name} runs on {engine}/{model or 'default'}, which has no price: "
                "add its rates under prices in config.json, or run without a money cap",
            )


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
    rounds: int = 1,
    max_cost_musd: int | None = None,
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
    if not 1 <= rounds <= ROUNDS_MAX:
        raise OfficeError("rounds", f"between 1 and {ROUNDS_MAX}")
    if rounds > 1 and not checks:
        raise OfficeError("rounds", "a desk goes again only when its checks fail: run the checks")
    names = chosen_desks(office, desks, roster)
    advisor_map = _advisors(office, names, roster) if advisors else {}
    if max_cost_musd is not None:
        _priced(office, [*names, *(n for mine in advisor_map.values() for n in mine)], roster)
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
    rounds: int = 1,
    max_cost_musd: int | None = None,
) -> _Office:
    """Check the office run can start, and write its record."""
    from metabot.plugins import enabled_plugins

    start = check_start(
        office, task, desks=desks, roster=roster, checks=checks, advisors=advisors,
        rounds=rounds, max_cost_musd=max_cost_musd,
    )  # fmt: skip
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
        "task": scrub(task, record_secrets()),
        "base": start.base,
        "checks": sorted(start.checks),
        "checks_skipped": start.checks_skipped,
        "advisors": start.advisors,
        "started_at": utc_now(),
        "finished_at": None,
        **({"rounds": rounds} if rounds > 1 else {}),
        **({"max_cost_musd": max_cost_musd, "spent_musd": 0} if max_cost_musd is not None else {}),
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
        # under a cap, one run at a time, so each starts under what is truly left
        slots=asyncio.Semaphore(1 if max_cost_musd is not None else fan_cap_of(cfg.fan_cap)),
        result=result,
        record=record,
        checks=start.checks,
        checks_skipped=start.checks_skipped,
        advisors=start.advisors,
        project=start.repo if project_receipts_on() else None,
        kits=kit_dirs,
        rounds=rounds,
        cap_musd=max_cost_musd,
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
    """``_open`` off the loop; an office run stopped while it opened is closed, then stopped,
    however often it is stopped before the open ends."""
    # A future, not a task: a loop that shuts down cancels its tasks, and the open would go
    # on in its thread with nobody left to close it.
    call = functools.partial(contextvars.copy_context().run, _open, *args, **kw)
    job = asyncio.get_running_loop().run_in_executor(None, call)
    try:
        return await asyncio.shield(job)
    except asyncio.CancelledError:
        while not job.done():  # a stop again waits too: wait() never cancels what it waits on
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait([job])
        if not job.cancelled() and job.exception() is None:
            with contextlib.suppress(Exception):
                _close(job.result())
        raise


def _close(run: _Office) -> None:
    run.record["finished_at"] = utc_now()
    if run.cap_musd is not None:
        run.record["spent_musd"] = run.spent_musd
    if run.kits:  # the desks are done with their kits
        shutil.rmtree(kits_root(run.result.office_run_id), ignore_errors=True)
    try:
        run.result.seal = _seal(run)
        run.record["seal"] = str(run.result.seal)
    except (OSError, ValueError) as exc:  # the record says so, and every desk receipt stands
        run.record["seal"], run.record["seal_error"] = None, str(exc)
        _said(run, f"the office run could not be sealed: {exc}")
    else:  # beside the desks' copies, so the project's folder checks alone
        copied, why = project_copy(run.result.seal, run.project)
        if copied is not None:
            run.result.project_copies.append(copied)
        if why is not None and why not in run.result.project_errors:  # said once, as the desks'
            run.result.project_errors.append(why)
    try:
        _save(run.record, run.result.rows)
    except OSError as exc:  # a full disk: the rows and what they spent are still said
        run.result.record_error = str(exc)
        _said(run, f"the office's record could not be written: {exc}")


def _said(run: _Office, error: str) -> None:
    """An error at the close, said however the office run ends: also when it was stopped."""
    if run.on_event is not None:
        with contextlib.suppress(Exception):  # a trace that cannot print never stops it
            run.on_event(ErrorEvent(error))


def _seal(run: _Office) -> Path:
    """One receipt over every desk run and checks run of the office run, in order."""
    members: list[Member] = []
    for row in run.result.rows:
        role = "desk" if row.advises is None else "advisor"
        # what each run left, also when a stop came before the row was told of it
        receipt = Path(row.receipt) if row.receipt else receipts_dir() / f"{row.run_id}.json"
        if row.receipt is None and receipt.is_file():
            row.receipt = receipt  # and the record names what the seal holds
        members.append(Member(row.run_id, role, receipt, row.ok))
        checked = row.run_id + CHECKS_SUFFIX
        kept = receipts_dir() / f"{checked}.json"
        if row.checks_receipt is None and kept.is_file():
            row.checks_receipt = kept
        if row.checks_receipt is not None:
            members.append(
                Member(checked, "checks", Path(row.checks_receipt), row.checks == "pass")
            )
    task = str(run.record.get("task", ""))
    workflow = (
        f"office_sha256={run.office.sha256 or 'unknown'};"
        f"checks={'on' if run.checks else 'off'};rounds={run.rounds};"
        f"allow_write={'true' if run.allow_write else 'false'}"
    )
    verdicts = [
        {"check": "workflow", "result": "pass", "detail": ascii_text(workflow, limit=1000)},
        *([budget_verdict(run.cap_musd, _counted(run))] if run.cap_musd is not None else []),
        *provenance_verdicts(run.pins),
    ]
    name, mode = ascii_text(run.office.name).replace(";", "_"), run.result.mode
    return seal_runs(
        run.result.office_run_id,
        members,
        delivered=run.result.ok,
        kind_detail=f"kind=office;name={name};mode={mode}",
        verdicts=verdicts,
        started_at=str(run.record["started_at"]),
        meta={"sealed": "office", "office_name": run.office.name, "task": task},
    )


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
    desk = run.office.desks[name]
    engine_name, model = desk_engine(desk, run.roster)
    read_only = desk.read_only and advises is None
    files = advises is None and not read_only
    row = DeskResult(
        desk=name,
        copy=copy,
        run_id=f"{run.result.office_run_id}.{label}",
        branch=desk_branch(run.result.office_run_id, label) if files else None,
        engine=engine_name,
        model=model or run.cfg.models.get(engine_name),
        advises=advises,
        read_only=read_only,
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
    round_of: tuple[int, int] | None = None,
    last_commit: str | None = None,
) -> None:
    """One desk run: its worktree from ``start``, its run, its checks, and its commit.

    A later round (``round_of``) works in the worktree its desk's first round
    made, from ``last_commit``, the commit the round before made; its work is
    what it changed since then, and its diff stat counts from ``start``.

    An advisor's run (``row.advises``) has a worktree with no branch, runs
    read-only with every write asked and nothing allowed ahead, and files
    nothing; ``since`` is where the work it reviews began. A read-only desk
    (``row.read_only``) runs the same way, on a desk's prompt.
    """
    from metabot.cmdline import resolve_allows

    desk = run.office.desks[row.desk]
    advisor = row.advises is not None
    files = not (advisor or row.read_only)  # else it runs read-only and files nothing
    began = False
    try:
        async with run.slots:
            left = None if run.cap_musd is None else run.cap_musd - run.spent_musd
            unkept = _unkept(run)
            if unkept is not None:
                row.error = row.stopped = unkept
                return
            try:
                if round_of is None:
                    wt = await _off_loop(
                        run.git,
                        create_worktree,
                        *(run.repo, run.result.office_run_id, row.label, start),
                        detached=not files,
                    )
                else:
                    wt = worktree_path(run.result.office_run_id, row.label)
                cwd = desk_cwd(wt, run.office, run.repo, desk)
                inbox = handoff(wt, earlier)
                if row.advises is not None:
                    prompt = advisor_prompt(
                        run.office, desk, run.task, inbox, row.advises, since or run.base, picks
                    )
                else:
                    prompt = desk_prompt(
                        run.office,
                        desk,
                        run.task,
                        inbox,
                        str(row.branch),
                        row.copy,
                        picks,
                        read_only=row.read_only,
                        round_of=(*round_of, inbox[-1]) if round_of else None,
                        changed_before=last_commit is not None,
                    )
            except (DeskGitError, OfficeError, OSError) as exc:
                row.error = str(exc)
                return
            _save_so_far(run)
            if run.on_desk is not None:
                run.on_desk(row)
            engine = run.make(row.engine)
            policy = Policy(
                allow_write=run.allow_write and files,
                allows=resolve_allows(list(run.allows), engine, run.store)
                if files
                else frozenset(),
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
                        **({"read_only": True} if row.read_only else {}),
                        **({"round": row.round} if round_of else {}),
                        **run.pins,
                    },
                    limits=Limits(
                        max_tool_calls=desk.turns, max_tokens=desk.budget, max_cost_musd=left
                    ),
                    read_only=not files,
                    project=run.project,
                )
            except RunCancelled as exc:
                _spend(
                    run,
                    row,
                    exc.result.cost_musd,
                    exc.result.tokens,
                    exc.result.lost,
                    exc.result.named_late,
                )
                row.declined = exc.result.declined
                if not files:
                    await _off_loop(run.git, _finish_advice, row, exc.result, wt)
                else:
                    await _off_loop(
                        run.git, _finish_desk, row, exc.result, wt, run.task, run.repo, start
                    )
                row.error = "cancelled"
                raise
            except Exception as exc:  # an engine adapter broke, or a file could not be written
                row.error = printable(f"{type(exc).__name__}: {exc}", 300)
                sealed = receipts_dir() / f"{row.run_id}.json"
                row.receipt = sealed if sealed.is_file() else None
                if row.receipt is not None:  # what it spent still counts
                    _spend(run, row, signed_cost(row.receipt), None)
                    row.declined = declined_and_failed((), verdicts_of(row.receipt))[0]
                elif run_path(row.run_id).is_dir():  # it began, and no seal says what it spent
                    _spend(run, row, None, None)
                else:  # it stopped before its run began: nothing ran
                    _spend(run, row, 0, 0)
                if isinstance(exc, OSError):  # a full disk: the desk failed, and the office says so
                    return
                raise
            _spend(run, row, result.cost_musd, result.tokens, result.lost, result.named_late)
            row.declined = result.declined
            _copied(run, result)
            hint = limit_hint(row.desk, result.limit)
            if hint and events is not None:  # said where the person watches, as it happens
                events(Notice(hint))
            if not files:
                await _off_loop(run.git, _finish_advice, row, result, wt)
                return
            if not (run.checks and result.ok):
                await _off_loop(run.git, _finish_desk, row, result, wt, run.task, run.repo, start)
                return
            # The checks judge the desk's commit, and what they leave is not its work.
            try:
                row.commit = await _off_loop(
                    run.git, _commit_work, row, wt, run.task, run.repo, last_commit or start
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
            # A desk that changed no file has no work of its to judge, save in a loop's
            # round 1 for the run's one writer: the checks say whether the task is done,
            # and if not it goes again, unless a call of it was declined.
            judged = run.rounds > 1 and round_of is None and _sole_writer(run) == row.desk
            if row.commit is None and not judged:
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
            if run.rounds > 1 and gate.summary not in (None, "pass"):
                row.feedback = feedback_text(gate, row.round or 1, changed=row.commit is not None)
            await _off_loop(
                run.git, _finish_desk, row, result, wt, run.task, run.repo, start, committed=True
            )
    except asyncio.CancelledError:
        if not began and row.error is None:
            row.error = "cancelled before it began"
        raise
    finally:
        _save_so_far(run)


def _sole_writer(run: _Office) -> str | None:
    """The run's one desk that may write, run as one copy; None when there are more.

    A desk that may write but is not there to, a planner, or one after copies,
    changes no file and is right to: only the one writer is held to the checks.
    """
    writers = [
        name
        for name in run.names
        if not (run.office.desks[name].read_only or run.office.desks[name].is_advisor)
    ]
    if len(writers) != 1 or run.office.desks[writers[0]].fan > 1:
        return None
    return writers[0]


def _spend(
    run: _Office,
    row: DeskResult,
    cost_musd: int | None,
    tokens: int | None,
    lost: bool = False,
    late: bool = False,
) -> None:
    """Count what a desk run cost toward the office's cap; ``tokens`` is what it counted,
    ``lost`` that a request it sent never said what it spent, ``late`` that its engine
    reported tokens before it named a model that prices them otherwise."""
    row.cost_musd = cost_musd
    run.spent_musd += cost_musd or 0
    if run.cap_musd is not None:
        run.record["spent_musd"] = run.spent_musd
        if cost_musd is None:  # under a cap: tokens with no price, or none counted
            later = row.round is not None and row.round > 1
            label = row.label + (f" r{row.round}" if later else "")
            run.result.cost_unknown.append(label)
            run.record["cost_unknown"] = list(run.result.cost_unknown)
            run.uncounted = run.uncounted or uncounted(label, tokens, lost, late)


def _spent(run: _Office) -> str:
    return f"the office's {dollars(run.cap_musd or 0)} is spent"


def _unkept(run: _Office) -> str | None:
    """Why nothing more runs under the office's cap; None while it holds."""
    if run.cap_musd is None:
        return None
    if run.uncounted is not None:
        return f"{run.uncounted}, so the office's {dollars(run.cap_musd)} cannot be kept"
    return _spent(run) if run.spent_musd >= run.cap_musd else None


def _counted(run: _Office) -> int | None:
    """What the office run spent, for its seal; None when a run's cost is unknown."""
    return None if run.result.cost_unknown else run.spent_musd


def _new_round(run: _Office, row: DeskResult, n: int) -> DeskResult:
    """The desk's next round: its run id is the first round's, then ``.r<n>``."""
    first = row.run_id.rsplit(".r", 1)[0] if row.round and row.round > 1 else row.run_id
    nxt = DeskResult(
        desk=row.desk,
        copy=row.copy,
        run_id=f"{first}.r{n}",
        branch=row.branch,
        engine=row.engine,
        model=row.model,
        round=n,
    )
    row.next_round = nxt.run_id
    run.result.rows.append(nxt)
    return nxt


async def _run_rounds(
    run: _Office,
    row: DeskResult,
    start: str,
    earlier: Sequence[tuple[str, str]],
    parent: str | None,
    picks: Sequence[str] = (),
    together: bool = False,
) -> None:
    """A desk's run, and while its checks fail, its next rounds; see the module doc."""
    await _run_row(run, row, start, earlier, parent, picks, together)
    while run.rounds > 1 and row.checks not in (None, "pass"):
        n = (row.round or 1) + 1
        if n > run.rounds:
            row.stopped = f"{run.rounds} rounds used; the checks still fail"
            return
        if row.declined:  # it would be asked the same again
            calls = "a declined call" if row.declined == 1 else f"{row.declined} declined calls"
            row.stopped = f"round {row.round or 1} had {calls}; the checks still fail"
            return
        unkept = _unkept(run)
        if unkept is not None:
            row.stopped = unkept
            return
        row.round = row.round or 1
        said = (f"{row.label}.checks.r{row.round}", row.feedback)
        last = row.commit
        row = _new_round(run, row, n)
        await _run_row(
            run, row, start, [*earlier, said], row_parent(run, row), picks, together,
            round_of=(n, run.rounds), last_commit=last,
        )  # fmt: skip
        if row.ok and row.checks is None and row.error is None:  # it changed no file
            row.ok = False
            row.stopped = f"round {n} changed no file; the checks still fail"
            row.error = "the gate's checks still fail"
            return
    if row.round and row.checks == "pass":
        row.stopped = f"the checks passed in round {row.round}"


def row_parent(run: _Office, row: DeskResult) -> str:
    """A later round's parent: the round before it."""
    return next(r.run_id for r in run.result.rows if r.next_round == row.run_id)


def _last_round(run: _Office, row: DeskResult) -> DeskResult:
    """The desk run its loop ended on: ``row`` itself when it ran once."""
    by_id = {r.run_id: r for r in run.result.rows}
    while row.next_round is not None:
        row = by_id[row.next_round]
    return row


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
    rounds: int = 1,
    max_cost_musd: int | None = None,
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
        advisors=advisors, office_run_id=office_run_id, rounds=rounds,
        max_cost_musd=max_cost_musd,
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
                [_run_rounds(run, row, start, earlier, parent, picks, together) for row in rows]
            )
            rows = [_last_round(run, row) for row in rows]
            good = [row for row in rows if row.ok]
            if not good:
                break
            earlier.extend((row.label, row.answer) for row in good)
            parent = ",".join(row.run_id for row in good)
            if together:  # the next desk starts where the copies started, and picks
                picks = tuple(str(row.branch) for row in good if row.commit is not None)
            elif good[0].branch is not None:  # a read-only desk files nothing: start stays
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
    rounds: int = 1,
    max_cost_musd: int | None = None,
) -> OfficeResult:
    """Run every chosen desk at once from one base; nothing is handed between them.

    Advisors do not run in a fan.
    """
    run = await _opened(
        office, task, "fan", approver=approver, desks=desks, allow_write=allow_write,
        allows=allows, roster=roster, connections=connections, plugins=plugins,
        on_event=on_event, on_desk=on_desk, make_engine=make_engine, checks=checks,
        office_run_id=office_run_id, rounds=rounds, max_cost_musd=max_cost_musd,
    )  # fmt: skip
    try:
        rows = [
            _new_row(run, name, copy) for name in run.names for copy in _copies(office.desks[name])
        ]
        together = len(rows) > 1
        await _together([_run_rounds(run, row, run.base, (), None, (), together) for row in rows])
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
    # a loop's rounds share one label and one branch: each label is offered once
    return list(dict.fromkeys(row.label for row in run.result.rows if row.label in offered))


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
    write_private(row.artefact, run.final_text, errors=SHOWN)
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
    write_private(row.artefact, run.final_text, errors=SHOWN)
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
    except (OSError, ValueError, RecursionError):
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
