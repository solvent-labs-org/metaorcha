"""Proposals: a workflow draft from a task, chosen by rules.

Models propose, rules decide, and this proposer is rules alone. ``metabot propose`` reads
the task, the folder and this machine's config, and rules choose the shape: one ask, a
ladder, or an office run. It writes a ``metabot.workflow/1`` draft, which ``metabot
workflow check`` reads as it reads a file written by hand, and names each rule that chose
a part of it. The rules are the ones MetaBot already runs on: ``metabot.suggest`` picks the
role, ``metabot.roster`` the engine, login and model, ``metabot.ladder`` the rungs, and
``metabot.office`` reads the desks.

A proposal never runs, approves, fires or keeps anything. It starts no engine and no
process, and contacts nothing: what is here is the engine CLIs on PATH, and the local
model server when its last probe (``metabot doctor --models``) answered. Where a folder
sits in git, the files git keeps say so, and no git is run. It writes nothing but the
draft, to a file ``--out`` names that is not there yet, and never a draft that ``metabot
workflow check`` would not read as a workflow.

A money cap is the person's to set: each step's ``max_cost`` is left empty, and a step that
may run on a paid engine says the cap is missing, in the draft and in what is printed.

A task that reads as private is always a ladder on the role ``private``: as it runs, the
ladder skips every rung that sends the work off this machine, so the draft names no engine
at all, and an office in the folder, whose desks may leave, is not proposed.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shlex
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from metabot.approvals import printable, terminal_safe
from metabot.config import Config
from metabot.workflows import (
    ABOUT_LIMIT,
    FORMAT,
    HEXISH,
    PROMPT_LIMIT,
    WorkflowError,
    parse_workflow,
    workflow_bytes,
)

ASK, LADDER, OFFICE = "ask", "ladder", "office"
LOOKS = {"review": "a review", "fast": "a quick look"}  # tasks that read and change nothing
BUILDS = ("build", "default")  # the rules whose work an office or a ladder takes
NAME_WORDS = 5
NAME_CHARS = 40
PRIVATE_NAME = "private-task"  # a private task's own words never name its workflow
FALLBACK_NAME = "proposed"
CAP_HOW = 'set "max_cost" in the step to the most one run may spend, in dollars, such as "2.00"'
LABEL = 8
SAID = 80  # the characters of a name a cap note gives, so the note always fits the about
GIT_RECORD = 4096


@dataclass(frozen=True)
class Proposal:
    shape: str  # ask, ladder or office
    name: str
    rules: tuple[str, ...]  # each rule that fired, in plain words
    runs_on: tuple[str, ...]  # what each step runs on here
    missing: tuple[str, ...]  # what to set before it runs
    data: dict[str, Any]  # the draft

    @property
    def raw(self) -> bytes:
        return workflow_bytes(self.data)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.raw).hexdigest()


def draft_name(task: str, private: bool) -> str:
    """A workflow name from the task's leading words; a private task's never names it."""
    if private:
        return PRIVATE_NAME
    words = [w for w in re.findall(r"[^\W_]+", task.lower()) if w.isascii()]
    name = ""
    for word in words[:NAME_WORDS]:
        longer = f"{name}-{word}" if name else word[:NAME_CHARS]
        if len(longer) > NAME_CHARS:
            break
        name = longer
    if not name:
        return FALLBACK_NAME
    return f"task-{name}" if HEXISH.match(name) else name


def _local_here(cfg: Config) -> bool:
    """Whether the local model server counts as here: its address is on this machine and its
    last probe answered. Nothing is contacted."""
    from metabot.ladder import is_local_url
    from metabot.models import cached_mode

    model = cfg.openai_model
    try:
        local = bool(model) and is_local_url(cfg.openai_base_url)
    except ValueError:  # an address that does not parse is not here
        return False
    return local and cached_mode(cfg.openai_base_url, model) is not None


class _Draft:
    """A proposal as it is put together: the rule lines, and what each step runs on."""

    def __init__(self) -> None:
        self.rules: list[str] = []
        self.runs_on: list[str] = []
        self.missing: list[str] = []
        self.cap: str | None = None  # why a cap is needed, when one is

    def paid(self, step: str, why: str) -> None:
        if self.cap is None:
            self.cap = f"max_cost in step {step}: {why}; {CAP_HOW}"


def _git_tree(path: Path) -> tuple[Path, Path] | None:
    """The top of the git working tree that holds ``path``, and the top of its main working
    tree, as ``git rev-parse`` gives them, read from the files git keeps; no git is run.
    None outside git, or where git could not say."""
    from metabot.paths import is_dir, is_file

    for folder in (path, *path.parents):
        dot_git = folder / ".git"
        try:
            if is_dir(dot_git):
                if is_file(dot_git / "HEAD"):
                    return folder, folder  # a repository's own working tree
                continue  # not a repository: git looks further up
            if not is_file(dot_git):
                continue
            with dot_git.open("rb") as fh:  # git writes the path's bytes as they are
                first = os.fsdecode(fh.read(GIT_RECORD)).split("\n", 1)[0].strip()
            if not first.startswith("gitdir:"):
                return None
            record = Path(os.path.normpath(folder / first[len("gitdir:") :].strip()))
            if not is_dir(record):
                return None  # pruned, or its main tree moved: git no longer knows it
            common = record
            if is_file(record / "commondir"):
                with (record / "commondir").open("rb") as fh:
                    said = os.fsdecode(fh.read(GIT_RECORD)).strip()
                common = Path(os.path.normpath(record / said))
            common = common.resolve()
        except (OSError, ValueError, RuntimeError):
            return None
        # a linked worktree's main tree holds its .git; a submodule's record is elsewhere
        return folder, common.parent if common.name == ".git" else folder
    return None


def _office_root(folder: Path) -> tuple[Path, Path]:
    """The folder an office run reads ``folder``'s office from, and the project it sits in,
    as ``office_root`` and ``load_office`` find them: a linked worktree's folder is the main
    tree's, when the main tree holds the office too."""
    from metabot.office import OFFICE_FILE
    from metabot.paths import is_file

    tree = _git_tree(folder)
    if tree is None:
        return folder, folder
    top, main = tree
    if main == top:
        return folder, top
    mapped = main / folder.relative_to(top)
    try:
        there = is_file(mapped / OFFICE_FILE)
    except OSError:  # an office it cannot look at, never another: reading it says why
        there = True
    if not there:
        return folder, top
    mapped_tree = _git_tree(mapped)
    return mapped, mapped if mapped_tree is None else mapped_tree[0]


def _office_in(folder: Path, cfg: Config, d: _Draft) -> Any:
    """The office in ``folder``, read as its run reads it; None when there is none, or it
    does not read, which a rule line says."""
    from metabot.office import OFFICE_FILE, OfficeError, load_office
    from metabot.paths import is_file

    root, top = _office_root(folder)
    try:
        there = is_file(root / OFFICE_FILE)
    except OSError:  # one it cannot look at: reading it says why
        there = True
    if not there:
        return None
    try:
        return load_office(root, cfg.fan_cap, top=top)
    except OfficeError as exc:
        d.rules.append(
            f"the folder's {OFFICE_FILE} does not read ({printable(str(exc), 300)}), "
            "so no office run is proposed"
        )
        return None


def _ask(
    task: str, s: Any, roster: Mapping[str, Mapping[str, str]], cfg: Config, d: _Draft
) -> dict[str, Any]:
    from metabot.ladder import tier_of

    look = LOOKS[s.rule]
    d.rules.append(f"{look} is one ask, on the {s.role} role")
    entry = roster.get(s.role) or {}
    login = entry.get("login") if s.engine and entry.get("engine") == s.engine else None
    step: dict[str, Any] = {"name": ASK, "kind": ASK, "prompt": task, "role": s.role}
    if s.engine:
        step["engine"] = s.engine
    if s.model:
        step["model"] = s.model
    if login:
        step["login"] = login
    if s.note and s.engine:
        d.rules.append(s.note)
    elif s.note:
        d.missing.append(s.note)
    d.rules.append(f"{look} reads and changes nothing, so the step is read_only")
    step["read_only"] = True
    on = f"{s.engine}/{s.model}" if s.engine and s.model else s.engine or "-"
    d.runs_on.append(f"{ASK}: {on}, role {s.role}, {f'login {login}' if login else 'no login'}")
    if s.engine and tier_of(s.engine, cfg) == "paid":
        d.paid(ASK, f"it runs on a paid engine ({s.engine}, billed by use)")
    return step


def _ladder(
    task: str, private: bool, found: tuple[Any, bool], cfg: Config, d: _Draft
) -> dict[str, Any]:
    from metabot.ladder import LadderError, attempts, effective_ladder
    from metabot.roster import OFF_MACHINE, stays_here

    if private:
        d.rules.append(
            "a private task runs as a ladder on the role private: as it runs, it skips every "
            "rung that sends the work off this machine, so the draft names no engine"
        )
    else:
        d.rules.append(
            "build work runs as a ladder, as the Home page runs a task: the cheapest rung "
            "runs, and the ladder climbs on a failure the record shows"
        )
    step: dict[str, Any] = {"name": LADDER, "kind": LADDER, "prompt": task}
    if private:
        step["role"] = "private"
    step["checks"] = []
    d.rules.append(
        "no check is proposed: a command you add to the step's checks judges each rung's work"
    )
    try:
        rungs, _whose, note = effective_ladder(cfg, found=found)
        most = attempts(cfg)
    except LadderError as exc:
        d.missing.append(str(exc))
        return step
    if note:
        d.rules.append(note)
    ran = 0
    for n, rung in enumerate(rungs, 1):
        line = f"{LADDER}: rung {n} {rung.tier} {rung.label()}"
        line += f", login {rung.login}" if rung.login else ""
        if private and not stays_here(rung.entry(), cfg):
            line += f", skipped: {OFF_MACHINE}"
        elif ran >= most:
            line += f", past the {most} rung(s) one task runs"
        else:
            ran += 1
            if rung.tier == "paid":
                label = printable(rung.label(), SAID)
                d.paid(LADDER, f"it may run on a paid rung ({label}), billed by use")
        d.runs_on.append(line)
    if ran == 0 and private:
        d.missing.append(
            "no rung here keeps the work on this machine: start the local model server and "
            f"probe it (metabot doctor --models {printable(cfg.openai_model, 80) or 'MODEL'}), "
            "or add a rung on it to ladder in config.json"
        )
    elif ran == 0:
        d.missing.append(
            "no rung here: install an engine CLI, or start and probe the local model server"
        )
    return step


def _office(
    task: str, office: Any, roster: Mapping[str, Mapping[str, str]], cfg: Config, d: _Draft
) -> dict[str, Any]:
    from metabot.ladder import tier_of
    from metabot.office import (
        OfficeError,
        cwd_link_problem,
        desk_engine,
        desk_login,
        desk_problem,
        desk_runs_on,
    )

    d.rules.append(
        f"the folder holds an office.yaml (office {printable(office.name, 80)}), so build work "
        "runs as an office run of its desks"
    )
    step: dict[str, Any] = {
        "name": OFFICE,
        "kind": OFFICE,
        "prompt": task,
        "dir": str(office.root),
        "office_sha256": office.sha256,
    }
    count = len(office.checks())
    if count:
        step["checks"] = True
        d.rules.append(
            f"the office's gates name {count} check{'' if count == 1 else 's'}, "
            "so the step runs them"
        )
    else:
        d.rules.append("the office's gates name no checks")
    for name, desk in office.desks.items():
        login = desk_login(desk, roster)
        d.runs_on.append(
            f"{OFFICE}: desk {name} on {desk_runs_on(desk, roster)}, "
            + (f"login {login}" if login else "no login")
            + (", advisor" if desk.is_advisor else "")
        )
        problem = desk_problem(desk, roster) or cwd_link_problem(office, desk)
        if problem:
            d.missing.append(f"desk {name}: {printable(problem, 300)}")
        try:
            engine, _model = desk_engine(desk, roster)
        except OfficeError:
            continue
        if tier_of(engine, cfg) == "paid":
            desk_name = printable(name, SAID)
            d.paid(OFFICE, f"desk {desk_name} runs on a paid engine ({engine}, billed by use)")
    return step


def propose(task: str, folder: Path, cfg: Config) -> Proposal:
    """The draft rules choose for ``task`` in ``folder``, with each rule that fired.

    ``folder`` is absolute. Nothing is started, contacted or written here.
    """
    from metabot import roster as rosters
    from metabot.suggest import reason, suggest

    d = _Draft()
    found = (rosters.available(), _local_here(cfg))
    if found[1]:
        d.rules.append(
            f"the local model {printable(cfg.openai_model, 80)} counts as here: its last probe "
            "answered, and propose contacts nothing"
        )
    roster = {r: e for r, (e, _) in rosters.effective_roster(cfg, *found).items()}
    s = suggest(task, roster, local=lambda entry: rosters.stays_here(entry, cfg))
    private = s.rule == "private"
    d.rules.insert(0, reason(s))
    office = _office_in(folder, cfg, d) if private or s.rule in BUILDS else None
    if private and office is not None:
        d.rules.append(
            "the folder holds an office.yaml, and its desks may send the work off this "
            "machine, so the private task is not proposed onto it"
        )
        office = None
    if s.rule in LOOKS:
        shape, step = ASK, _ask(task, s, roster, cfg, d)
    elif office is not None:
        shape, step = OFFICE, _office(task, office, roster, cfg, d)
    else:
        shape, step = LADDER, _ladder(task, private, found, cfg, d)
    step["allow_write"] = False
    step["allow"] = []
    if not step.get("read_only"):
        d.rules.append("each write asks: allow and allow_write are left for you to set")
    step["max_cost"] = None
    if d.cap is not None:
        d.missing.append(d.cap)
    else:
        d.rules.append("max_cost is left empty for you to set: no step here runs on a paid engine")
    name = draft_name(task, private)
    about = (
        f"A draft from metabot propose: rules chose {'an' if shape != LADDER else 'a'} {shape}. "
        "Read it, then approve it yourself."
    )
    # the cap note leads, so an about cut to its limit still says the cap is missing
    notes = ([d.cap] if d.cap else []) + [m for m in d.missing if m != d.cap]
    if notes:
        about += f" Before it runs: {'; '.join(notes)}."
    if len(about) > ABOUT_LIMIT:
        about = about[: ABOUT_LIMIT - 1] + "…"
    data = {
        "format": FORMAT,
        "name": name,
        "about": about,
        "cwd": str(folder),
        "steps": [step],
    }
    return Proposal(shape, name, tuple(d.rules), tuple(d.runs_on), tuple(d.missing), data)


# ── the command ─────────────────────────────────────────────────────────────


class _Refused(ValueError):
    pass


def _utf8(text: str, what: str) -> None:
    """A draft holds UTF-8 text: a name from argv that is not refuses here, not as it is
    written."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        raise _Refused(f"{what} is not UTF-8 text") from None


def _target(raw: str) -> Path:
    """Where ``--out`` writes: never in MetaBot's home, where approvals and the store live,
    and never the home itself or a folder that holds it, which a file there would stop
    MetaBot from making."""
    from metabot.paths import kept_home

    _utf8(raw, f"the --out name {printable(raw)}")
    path = Path(raw).expanduser().absolute()  # the name itself is never followed if a link
    try:
        parent = path.parent.resolve()
        home = kept_home().resolve()
    except (OSError, RuntimeError) as exc:
        raise _Refused(f"cannot write {printable(raw)}: {exc}") from None
    target = parent / path.name
    if parent.is_relative_to(home) or home.is_relative_to(target):
        raise _Refused(
            f"{printable(str(path))} is MetaBot's home, in it or holds it; write the draft "
            "outside it, with your own files"
        )
    return target


def _write(path: Path, raw: bytes) -> None:
    """``raw`` to a new file at ``path``, mode 0600; never over a file or a link there."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            os.fchmod(fh.fileno(), 0o600)
            fh.write(raw)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(path)
        raise


def _next(p: Proposal, out: Path | None) -> list[str]:
    where = shlex.quote(str(out)) if out is not None else "FILE"
    return [
        f"metabot workflow check {where}",
        f"metabot workflow add {where}",
        f"metabot workflow approve {p.name}",
    ]


def cmd_propose(args: argparse.Namespace) -> int:
    from metabot.cmdline import EXIT_OK, EXIT_USAGE, given_folder, read_prompt
    from metabot.config import load_config

    def refuse(text: str) -> int:
        print(f"metabot: {text}", file=sys.stderr)
        return EXIT_USAGE

    try:
        task = read_prompt(args.task)
    except UnicodeDecodeError:  # stdin that is not UTF-8
        return refuse("the task is not UTF-8 text")
    if not task.strip():
        return refuse("the task is empty")
    if len(task) > PROMPT_LIMIT:
        return refuse(f"the task is longer than {PROMPT_LIMIT} characters")
    folder, usable = given_folder(args.folder)
    if not usable:
        return refuse(f"{printable(str(folder))} is not a directory")
    try:
        _utf8(task, "the task")
        _utf8(str(folder), f"the folder name {printable(str(folder))}")
        out = _target(args.out) if args.out else None
    except _Refused as exc:
        return refuse(str(exc))
    p = propose(task, folder, load_config())
    try:
        parse_workflow(p.raw)  # the check a hand-written file passes, before it is written
    except WorkflowError as exc:
        return refuse(f"the draft does not read as a workflow ({exc}); give a shorter task")
    if out is not None:
        try:
            _write(out, p.raw)
        except FileExistsError:
            return refuse(f"{printable(str(out))} is there already; propose never writes over it")
        except OSError as exc:
            return refuse(f"cannot write {printable(str(out))}: {exc.strerror or exc}")
    if args.json:
        print(
            json.dumps(
                {
                    "shape": p.shape,
                    "name": p.name,
                    "rules": list(p.rules),
                    "runs_on": list(p.runs_on),
                    "missing": list(p.missing),
                    "draft": p.data,
                    "sha256": p.sha256,
                    "path": None if out is None else str(out),
                    "next": _next(p, out),
                },
                indent=2,
            )
        )
        return EXIT_OK
    rows = [("shape", p.shape)]
    rows += [("rule", r) for r in p.rules]
    rows += [("runs on", r) for r in p.runs_on]
    rows += [("missing", m) for m in p.missing]
    for label, value in rows:
        print(f"{label:<{LABEL}} {printable(value, 600)}")
    if out is not None:
        said = f"{printable(str(out), 600)}, sha256 {p.sha256}, not kept and not approved"
        print(f"{'draft':<{LABEL}} {said}")
    else:
        text = p.raw.decode("utf-8")
        print(f"{'draft':<{LABEL}} not written; --out FILE writes it:")
        sys.stdout.write(terminal_safe(text) if sys.stdout.isatty() else text)
    for line in _next(p, out):
        print(f"{'next':<{LABEL}} {line}")
    return EXIT_OK
