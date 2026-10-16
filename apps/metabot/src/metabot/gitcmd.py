"""git, run the one way MetaBot runs it.

Always an argument list, never a shell. Variables that point git at some
other repository (``GIT_DIR`` and the like) are dropped, so a value left over
from a hook or another tool never redirects MetaBot's git. Hooks are off: a
desk's worktree and commits are MetaBot's bookkeeping, and the person's hooks
run once, when they commit the work they picked.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

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
    repo: Path, *args: str, check: bool = True, timeout: float = TIMEOUT_S
) -> subprocess.CompletedProcess[str]:
    argv = ["git", "-C", str(repo), *NO_HOOKS, *args]
    try:
        proc = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="surrogateescape",
            env=git_env(),
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
    way, so both tools agree on which office a folder belongs to.
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
        return path
    lines = proc.stdout.splitlines()
    if proc.returncode != 0 or len(lines) != 2:
        return path
    top, common = Path(lines[0]).resolve(), Path(lines[1]).resolve()
    main = common.parent
    if main == top or common.name != ".git":
        return path
    try:
        return main / path.resolve().relative_to(top)
    except ValueError:
        return path
