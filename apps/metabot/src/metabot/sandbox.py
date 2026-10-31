"""Each engine CLI runs with the instructions outside its project hidden.

An engine CLI reads instruction files besides the task: AGENTS.md, CLAUDE.md,
``.cursor/rules`` and skill folders, in its project and in the folders above
it, and its own user rules, skills, plugins and memory. Outside the project
they are not the project's: on a shared machine they may belong to another
project, or another client. So when there is one to hide, MetaBot starts
the engine CLI inside bubblewrap (``bwrap``), with each such file found above
the project shown blank and each such folder shown empty and read-only, and
the same for the engine's own user rules, skills, plugins, hooks and memory.
Read-only, since an engine may fill a writable one again as it starts:
cursor-agent downloads the plugins of the person's account into its plugin
folder, so that folder is made, empty, when it is not there yet.

The project is the nearest folder at or above the run's folder that holds a
git repository, as git finds one; else the run's folder. A linked worktree
inside its own repository has the main checkout as its project. The home
folder, or a folder that holds it, is never a project: a run in it has none,
and what that folder holds is hidden too. The project's own files stay as
they are. So does a folder that holds the project, and of it everything else
is hidden. A file the engine's command names is never hidden.

Inside the sandbox nothing can gain privileges: ``sudo`` and other setuid
programs fail, and files that belong to other users show as owned by
``nobody``. Otherwise the engine reads and writes what it did, and reaches the
network as it did.

Without a working ``bwrap``, or with ``"sandbox": false`` in config.json,
nothing is hidden: the run says how many sources it left in view, and its
receipt signs that. A cursor run without ``bwrap`` refuses unless the person
turned the sandbox off, since cursor reads every rule folder above its
project, and its account's plugins come down as it starts.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from metabot.paths import metabot_home, write_private

# What an engine CLI may read as instructions in a folder above its project.
ABOVE = (
    "AGENTS.md",
    "AGENTS.override.md",
    "CLAUDE.md",
    "CLAUDE.local.md",
    "GEMINI.md",
    ".cursorrules",
    ".windsurfrules",
    ".cursor/rules",
    ".cursor/skills",
    ".claude/CLAUDE.md",
    ".claude/rules",
    ".claude/skills",
    ".agents/skills",
    ".github/copilot-instructions.md",
)
CHECK_TIMEOUT = 10.0
_WORKING: set[str] = set()  # each bwrap that has started a process; a failure is never kept


def user_home() -> Path:
    return Path.home()


def find_bwrap() -> str | None:
    """This machine's bwrap, once it has started a process; None when it cannot.

    A bwrap that worked is not checked again. One that did not is checked at
    the next run, so one installed while ``metabot ui`` or ``serve`` runs is
    used from then on.
    """
    path = shutil.which("bwrap")
    if path is None or path in _WORKING:
        return path
    try:
        ran = subprocess.run(
            [path, "--dev-bind", "/", "/", "--", "true"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=CHECK_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if ran.returncode != 0:
        return None
    _WORKING.add(path)
    return path


def blank_file() -> Path:
    """An empty file of MetaBot's own, shown in place of each hidden file."""
    path = (metabot_home() / "sandbox" / "blank").absolute()  # bwrap reads it from the run's folder
    if not path.is_file() or path.stat().st_size:
        write_private(path, "")
    return path


def empty_dir() -> Path:
    """An empty folder of MetaBot's own, shown read-only in place of each hidden folder.

    Something put in one is never shown: the next one is taken, and kept.
    """
    base = (metabot_home() / "sandbox").absolute()
    n = 0
    while True:
        path = base / ("empty" if n == 0 else f"empty-{n}")
        path.mkdir(parents=True, exist_ok=True)
        if not any(path.iterdir()):
            return path
        n += 1


def present(paths: Iterable[Path]) -> list[Path]:
    """Those of ``paths`` that are there; a path that cannot be looked at is not."""
    return [p for p in paths if os.path.exists(p)]


def named_file(word: str) -> Path | None:
    """The file a command word names: a path, or a program on PATH."""
    found = shutil.which(word) if "/" not in word else word
    path = Path(found) if found else None
    return path if path is not None and _is(path.is_file) else None


def _is(check: Callable[[], bool]) -> bool:
    try:
        return check()
    except OSError:
        return False


def _resolved(path: Path) -> Path:
    try:
        return path.resolve()
    except (OSError, RuntimeError):  # a loop of links
        return Path(os.path.abspath(path))


def _holds(outer: Path, inner: Path) -> bool:
    return inner == outer or inner.is_relative_to(outer)


def _gitdir(dot: Path) -> Path | None:
    """The folder a ``.git`` file names, as a linked worktree's or a submodule's does."""
    try:
        with dot.open("rb") as fh:  # git writes the path's bytes as they are
            first = os.fsdecode(fh.readline(4096)).strip()
    except OSError:
        return None
    if not first.startswith("gitdir:"):
        return None
    named = Path(first.removeprefix("gitdir:").strip())
    return named if named.is_absolute() else dot.parent / named


def _common(named: Path) -> Path:
    """The repository folder a worktree's record at ``named`` shares, by its ``commondir``;
    OSError when that cannot be read."""
    return _resolved(named / os.fsdecode((named / "commondir").read_bytes()).strip())


def _repository(folder: Path) -> bool:
    """Whether ``folder`` holds a git repository as git takes one: a ``.git`` folder
    with ``HEAD`` and ``objects``, or a ``.git`` file naming a folder with ``HEAD``."""
    dot = folder / ".git"
    if _is(dot.is_dir):
        return _is((dot / "HEAD").is_file) and _is((dot / "objects").is_dir)
    if _is(dot.is_file):
        named = _gitdir(dot)
        return named is not None and _is((named / "HEAD").is_file)
    return False


def git_root(cwd: Path) -> Path | None:
    """The nearest folder at or above ``cwd`` that holds a git repository."""
    here = _resolved(cwd)
    return next((f for f in (here, *here.parents) if _repository(f)), None)


def main_checkout(root: Path) -> Path | None:
    """The main checkout of the linked worktree at ``root``; None for any other folder."""
    dot = root / ".git"
    named = _gitdir(dot) if _is(dot.is_file) else None
    if named is None:
        return None
    try:
        common = _common(named)
    except OSError:
        return None
    if common.name != ".git" or not _repository(common.parent):
        return None  # a bare repository's worktree has no main checkout
    return common.parent


def copies_main(project: Path) -> Path | None:
    """The main checkout a copy of the git project at ``project`` names, as ``main_checkout``
    reads it in the copy: a linked worktree of the project's repository. None outside git,
    and for a repository with no main checkout, such as a submodule's or a bare one."""
    root = git_root(project)
    if root is None:
        return None
    dot = root / ".git"
    if _is(dot.is_dir):
        common = _resolved(dot)
    else:
        named = _gitdir(dot)
        if named is None:
            return None
        try:
            common = _common(named)
        except OSError:
            common = _resolved(named)  # a repository of its own, such as a submodule's
    if common.name != ".git" or not _repository(common.parent):
        return None
    return common.parent


def project_root(cwd: Path) -> Path:
    """The project of a run in ``cwd``: the nearest folder that holds a git repository,
    else ``cwd``; never the home folder or a folder that holds it."""
    here = _resolved(cwd)
    root = git_root(here) or here
    return here if _holds(root, _resolved(user_home())) else root


def _places(cwd: Path, copy_of: Path | None = None) -> tuple[list[Path], tuple[Path, ...]]:
    """The folders whose instructions a run in ``cwd`` hides, and the project's own.

    ``copy_of``: ``cwd`` is where MetaBot makes a copy of the git project there, a linked
    worktree of its repository: a git root of its own, which names the main checkout.
    """
    here = _resolved(cwd)
    home = _resolved(user_home())
    if copy_of is None:
        root = project_root(here)
        main = main_checkout(root)
    else:
        root, main = here, copies_main(copy_of)
    top = main if main is not None and _holds(main, root) else root  # a worktree in its repository
    kept = tuple(dict.fromkeys(p for p in (top, root, here) if not _holds(p, home)))
    walk = [top] if _holds(top, home) else []  # a run with no project: its folder too
    walk += top.parents
    # An engine walks up the path it was given, which may pass a link the disk's does not.
    given = Path(os.path.abspath(cwd))
    along = next((f for f in (given, *given.parents) if _resolved(f) == top), None)
    if along is not None:
        walk += along.parents
    return list(dict.fromkeys(walk)), kept


def _hideable(path: Path, kept: Sequence[Path], seen: Sequence[Path]) -> list[Path]:
    """What hiding ``path`` hides, as the disk has it.

    Nothing for the project's own, or a link into it, or what is not a file or a
    folder, such as a link to ``/dev/null``. For a folder that holds the project
    or a path in ``seen``, everything else in it. Else the path itself.
    """
    try:
        if not os.path.lexists(path):
            return []
        real = path.resolve()
    except (OSError, RuntimeError):  # a loop of links: there is nothing to read
        return []
    if any(_holds(k, real) for k in kept) or real in seen:
        return []
    if not (_is(real.is_dir) or _is(real.is_file)):
        return []
    if any(_holds(real, k) for k in (*kept, *seen)):
        try:
            entries = sorted(real.iterdir())
        except OSError:
            return []
        return [hid for entry in entries for hid in _hideable(entry, kept, seen)]
    return [real]


def _outermost(paths: Iterable[Path]) -> tuple[Path, ...]:
    """The paths, less any inside another one's folder: bwrap cannot bind inside a bind."""
    found = tuple(dict.fromkeys(paths))
    folders = [p for p in found if _is(p.is_dir)]
    return tuple(p for p in found if not any(p != f and _holds(f, p) for f in folders))


def _sources(engine: Any, cwd: Path, copy_of: Path | None = None) -> tuple[Path, ...]:
    walk, kept = _places(cwd, copy_of)
    commands: list[Path] = getattr(engine, "command_files", list)()
    seen = (_resolved(cwd), *(_resolved(Path(f)) for f in commands))
    found = [hid for f in walk for name in ABOVE for hid in _hideable(f / name, kept, seen)]
    own = []
    if hasattr(engine, "user_instructions"):
        own = engine.user_instructions(cwd, copy_of=copy_of)
    found += [hid for path in own for hid in _hideable(path, kept, seen)]
    return _outermost(found)


def found_above(cwd: Path) -> list[Path]:
    """Every instruction file and folder outside the project of a run in ``cwd``."""
    return list(_sources(None, cwd))


@dataclass
class SandboxPlan:
    """How one engine run starts: what it hides, with what, or why it does not start.

    ``started`` is set once the engine's process is there. A run that never
    started its engine signs no sandbox verdict: nothing was in its view.
    """

    hidden: tuple[Path, ...] = ()
    tool: str | None = None  # bwrap, when it hides them
    blank: Path | None = None  # the empty file shown in place of each hidden file
    empty: Path | None = None  # the empty folder shown in place of each hidden folder
    refused: str | None = None
    off: bool = False  # the person turned the sandbox off
    started: bool = False

    def wrap(self, argv: Sequence[str]) -> list[str]:
        if self.tool is None:
            return list(argv)
        mounts: list[str] = []
        for path in self.hidden:
            if path.is_dir():
                mounts += ["--ro-bind", str(self.empty or empty_dir()), str(path)]
            else:
                # a bound /dev/null cannot be opened: a hidden file reads as an empty one
                mounts += ["--ro-bind", str(self.blank or blank_file()), str(path)]
        # No --die-with-parent: a stop reaches bwrap and the engine alike, and bwrap,
        # which ends at once, would take the engine with it before it could stop.
        return [self.tool, "--dev-bind", "/", "/", *mounts, "--", *argv]

    def verdict(self) -> dict[str, str] | None:
        if self.refused is not None:
            detail = f"refused;unhidden={len(self.hidden)}"
            return {"check": "sandbox", "result": "fail", "detail": detail}
        if not self.started:
            return None
        if self.tool is not None:
            return {
                "check": "sandbox",
                "result": "pass",
                "detail": f"bwrap;hidden={len(self.hidden)}",
            }
        how = "off" if self.off else "none"
        if not self.hidden:
            return {"check": "sandbox", "result": "pass", "detail": f"{how};hidden=0"}
        return {
            "check": "sandbox",
            "result": "warn",
            "detail": f"{how};unhidden={len(self.hidden)}",
        }

    def note(self) -> str | None:
        """What the run says when it leaves instructions in view."""
        if self.tool is not None or not self.hidden:
            return None
        why = "the sandbox is off in config.json" if self.off else "bwrap is not available here"
        return (
            f"metabot: {len(self.hidden)} instruction file(s) or folder(s) outside the project "
            f"are in this engine's view, since {why}"
        )


def _refusal(name: str, hidden: Sequence[Path]) -> str:
    found = f" ({len(hidden)} of them on this machine)" if hidden else ""
    reads = (
        f"{name} reads every rule folder above its project and your own rules, and its "
        f"account's plugins come down as it starts{found}. MetaBot hides them with bwrap"
    )
    if sys.platform.startswith("linux"):
        return (
            f"{reads}, which does not work here: install bubblewrap, or set "
            f'"sandbox": false in config.json to run {name} with them in view'
        )
    return f'{reads}, on Linux: set "sandbox": false in config.json to run {name} with them in view'


def plan_for(engine: Any, cwd: Path, *, enabled: bool) -> SandboxPlan:
    """The sandbox for one run of ``engine`` in ``cwd``."""
    tool = find_bwrap() if enabled else None
    if tool is not None:  # a folder the engine fills as it starts is hidden from the start
        for folder in getattr(engine, "fills_as_it_starts", list)():
            with contextlib.suppress(OSError):
                folder.mkdir(parents=True, exist_ok=True)
    hidden = _sources(engine, cwd)
    if not enabled:
        return SandboxPlan(hidden=hidden, off=True)
    if tool is not None:
        if not hidden:
            return SandboxPlan()
        return SandboxPlan(hidden=hidden, tool=tool, blank=blank_file(), empty=empty_dir())
    if getattr(engine, "reads_rules_above", False):
        return SandboxPlan(hidden=hidden, refused=_refusal(engine.name, hidden))
    return SandboxPlan(hidden=hidden)


def _makes(folder: Path) -> bool:
    """Whether ``mkdir -p`` makes ``folder``, missing now: nothing on its way that is
    not a folder, and the nearest folder there is can be written in."""
    there = folder
    while not os.path.lexists(there):
        if there.parent == there:
            return False
        there = there.parent
    return _is(there.is_dir) and os.access(there, os.W_OK | os.X_OK)


def _made_and_hidden(
    engine: Any, cwd: Path, found: Sequence[Path], copy_of: Path | None = None
) -> list[Path]:
    """The folders the engine fills as it starts that a run in ``cwd`` makes, then hides:
    each is missing now, a run can make it, and it is not the project's own."""
    _walk, kept = _places(cwd, copy_of)
    commands: list[Path] = getattr(engine, "command_files", list)()
    seen = (_resolved(cwd), *(_resolved(Path(f)) for f in commands))
    made: list[Path] = []
    for folder in getattr(engine, "fills_as_it_starts", list)():
        if os.path.lexists(folder) or not _makes(folder):
            continue  # there already, so counted as it is; or a run cannot make it
        real = _resolved(folder)
        if any(_holds(k, real) for k in kept) or real in seen:
            continue
        if not any(_holds(h, real) for h in (*found, *made)):
            made.append(real)
    return made


def foresee(
    engine: Any, cwd: Path, *, enabled: bool, where: str = "here", copy_of: Path | None = None
) -> str:
    """What the sandbox does for a run of ``engine`` in ``cwd``, in a line; nothing is made.

    It decides as ``plan_for`` does. With bwrap, a folder the engine fills as it
    starts counts as hidden when the run can make it outside the project: the
    run makes it first. ``where`` names the folder in the line. ``copy_of``: ``cwd``
    is where MetaBot makes a copy of the git project there, which is not made yet.
    """
    found = _sources(engine, cwd, copy_of)
    left = f"{len(found)} left in view {where}" if found else f"nothing to hide {where}"
    if not enabled:
        return f"off in config.json: {left}"
    if find_bwrap() is not None:
        count = len(found) + len(_made_and_hidden(engine, cwd, found, copy_of))
        return f"hides {count} {where}, with bwrap" if count else f"nothing to hide {where}"
    if getattr(engine, "reads_rules_above", False):
        return f"refuses {where}: bwrap does not work on this machine" + (
            f" ({len(found)} to hide)" if found else ""
        )
    return f"{left}: bwrap does not work on this machine" if found else left


def inner_argv(argv: Sequence[str]) -> list[str]:
    """The command a wrapped command line runs; any other command line as it is."""
    if argv and Path(argv[0]).name == "bwrap":
        with contextlib.suppress(ValueError):
            return list(argv[list(argv).index("--") + 1 :])
    return list(argv)
