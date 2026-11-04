"""git, run the one way MetaBot runs it.

Always an argument list, never a shell. Variables that point git at some
other repository (``GIT_DIR`` and the like) are dropped, so a value left over
from a hook or another tool never redirects MetaBot's git. Hooks are off: a
desk's worktree and commits are MetaBot's bookkeeping, and the person's hooks
run once, when they commit the work they picked.
"""

from __future__ import annotations

import errno
import os
import stat
import subprocess
from pathlib import Path

from metabot.paths import NOT_THERE, is_dir, is_file

DROPPED = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_NAMESPACE",
    "GIT_PREFIX",
)
TIMEOUT_S = 120.0
NO_HOOKS = ("-c", "core.hooksPath=/dev/null")


class GitError(RuntimeError):
    def __init__(self, args: tuple[str, ...], returncode: int | None, stderr: str) -> None:
        said = stderr.strip().splitlines()
        detail = said[-1] if said else f"exit {returncode}"
        super().__init__(f"git {args[0] if args else ''}: {detail}")
        self.args_run = args
        self.returncode = returncode
        self.stderr = stderr


def git_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in DROPPED}
    env["GIT_TERMINAL_PROMPT"] = "0"  # never wait on a credential prompt nobody sees
    return env


def git(
    repo: Path,
    *args: str,
    check: bool = True,
    timeout: float = TIMEOUT_S,
    index: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run git in ``repo``. ``index`` is the one way an index other than the
    repository's own is used: a leftover ``GIT_INDEX_FILE`` never is."""
    argv = ["git", "-C", str(repo), *NO_HOOKS, *args]
    env = git_env()
    if index is not None:
        env["GIT_INDEX_FILE"] = str(index)
    try:
        proc = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="surrogateescape",
            env=env,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitError(args, None, f"timed out after {exc.timeout:.0f}s") from None
    except OSError as exc:
        raise GitError(args, None, f"cannot run git: {exc.strerror or exc}") from None
    if check and proc.returncode != 0:
        raise GitError(args, proc.returncode, proc.stderr)
    return proc


def toplevel(path: Path) -> Path | None:
    """The top of the working tree holding ``path``, or None outside git."""
    try:
        proc = git(path, "rev-parse", "--show-toplevel", check=False, timeout=10)
    except GitError:
        return None
    line = proc.stdout.strip()
    return Path(line).resolve() if proc.returncode == 0 and line else None


def main_worktree(path: Path) -> Path:
    """``path`` mapped from a linked worktree onto the main working tree.

    Unchanged when it is not in a linked worktree. Code Desks maps the same
    way, so both tools agree on which office a folder belongs to. When git
    cannot answer because the main tree's record of the worktree cannot be
    looked at, the worktree's own ``.git`` file still says where that tree is;
    Code Desks then leaves the folder as it is. When git no longer knows the
    worktree, or declines on purpose, the folder stays as it is.
    """
    try:
        proc = git(
            path,
            "rev-parse",
            "--path-format=absolute",
            "--show-toplevel",
            "--git-common-dir",
            check=False,
            timeout=10,
        )
    except GitError:
        return _by_git_file(path) or path
    lines = proc.stdout.splitlines()
    if proc.returncode != 0 or len(lines) != 2:
        return _by_git_file(path) or path
    top, common = Path(lines[0]).resolve(), Path(lines[1]).resolve()
    main = common.parent
    if main == top or common.name != ".git":
        return path
    try:
        return main / path.resolve().relative_to(top)
    except ValueError:
        return path


def _by_git_file(path: Path) -> Path | None:
    """``path`` (resolved) on the main tree, read from the ``.git`` file of the linked worktree
    that holds it, ``gitdir: <main>/.git/worktrees/<name>``, when that record of it cannot be
    looked at or entered, as git could not: no access, or a mount that stopped answering.
    None when no such file says so, or the record is gone or open, so git's answer stands."""
    for folder in (path, *path.parents):
        dot_git = folder / ".git"
        try:
            if is_dir(dot_git):
                return None  # a repository's own working tree
            if not is_file(dot_git):
                continue
            with dot_git.open("rb") as fh:  # git writes the path's bytes as they are
                first = os.fsdecode(fh.read(4096)).split("\n", 1)[0].strip()
        except OSError:
            return None
        if not first.startswith("gitdir:"):
            return None
        record = Path(os.path.normpath(folder / first[len("gitdir:") :].strip()))
        worktrees = record.parent
        if worktrees.name != "worktrees" or worktrees.parent.name != ".git":
            return None  # a submodule's, or a bare repository's worktree
        try:
            held = os.stat(record)
        except ValueError:  # a name no file can have: git knows no such worktree
            return None
        except OSError as exc:
            if exc.errno in NOT_THERE or exc.errno == errno.ENAMETOOLONG:
                return None  # pruned, or its main tree moved: git no longer knows it
            return worktrees.parent.parent / path.relative_to(folder)
        if stat.S_ISDIR(held.st_mode) and not os.access(record, os.X_OK):  # shut to git too
            return worktrees.parent.parent / path.relative_to(folder)
        return None  # there to look at: git failed for another reason, or declined
    return None
