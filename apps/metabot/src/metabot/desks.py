"""Desks work in git worktrees that MetaBot keeps outside the project.

Each desk of an office run gets a worktree at
``<METABOT_HOME>/desks/<office run>/<label>``, on the branch
``metabot/<office run>/<label>``. The label is the desk's name, or
``<desk>.<n>`` for one of a desk's copies. The person's own checkout is never
touched while desks work, and gains no untracked folder.

After a desk's run, MetaBot commits what the desk changed on its branch,
under the person's own git identity, with the run id as the message's last
line. ``.metabot/`` in a worktree holds what desks hand each other and is
never committed.

``pick`` stages one desk branch's work as a squash in the person's checkout
and never commits. ``clean`` removes a run's worktrees and branches after the
person agrees, and keeps any desk whose work is not committed. Nothing here
pushes.

A worktree's ``.git`` file is git's link from the worktree to its records in
the repository, and work run in the worktree can change it like any file: a
search and replace did, on 10-22. Pointed at the project's own ``.git``, it
would turn MetaBot's commit in the worktree into a commit on the person's
branch. So ``create_worktree`` keeps the file as git wrote it, and
``relink`` puts it back before MetaBot's own git runs there.
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from metabot.gitcmd import GitError, git
from metabot.paths import metabot_home, private_dir, safe_id
from metabot.text import ascii_digits

BRANCH_ROOT = "metabot"
TRAILER = "MetaBot-Run"
SUBJECT_LIMIT = 72
HANDOFF_DIR = ".metabot"
EXCLUDE = f":(exclude){HANDOFF_DIR}"
_NAME = r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}"
# a desk, a copy of one (dev.2), or an advisor's review of one (qa.on-dev, qa.on-you)
LABEL = re.compile(rf"^{_NAME}(\.[0-9]{{1,2}}|\.on-{_NAME})?$")
ADVICE = ".on-"
SHA = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
IN_PROGRESS = {
    "MERGE_HEAD": "a merge",
    "CHERRY_PICK_HEAD": "a cherry-pick",
    "REVERT_HEAD": "a revert",
    "rebase-merge": "a rebase",
    "rebase-apply": "a rebase",
}
UNDO = "git reset --merge"


class DeskGitError(RuntimeError):
    pass


def desk_label(desk: str, copy: int | None = None) -> str:
    """The name a desk's worktree, branch and run id carry: ``dev``, or ``dev.2``."""
    return _label(desk if copy is None else f"{desk}.{copy}")


def advisor_label(advisor: str, advises: str) -> str:
    """An advisor's run on the desk it advises, or on you: ``qa.on-dev``, ``qa.on-you``."""
    return _label(f"{advisor}{ADVICE}{advises}")


def _label(label: str) -> str:
    if not isinstance(label, str) or not LABEL.match(label):
        raise ValueError(f"unsafe desk label: {label!r}")
    return label


def desk_branch(run_id: str, label: str) -> str:
    return f"{BRANCH_ROOT}/{safe_id(run_id)}/{_label(label)}"


def desks_root(run_id: str) -> Path:
    return metabot_home() / "desks" / safe_id(run_id)


def worktree_path(run_id: str, label: str) -> Path:
    return desks_root(run_id) / _label(label)


def resolve_commit(repo: Path, rev: str) -> str:
    """The full sha of a commit, or DeskGitError: never an option or a path."""
    proc = git(
        repo,
        "rev-parse",
        "--verify",
        "--quiet",
        "--end-of-options",
        f"{rev}^{{commit}}",
        check=False,
    )
    sha = proc.stdout.strip()
    if proc.returncode != 0 or not SHA.match(sha):
        raise DeskGitError(f"not a commit here: {rev}")
    return sha


def is_ancestor(repo: Path, older: str, newer: str) -> bool:
    """Whether commit ``older`` is in the history of commit ``newer``; False when git cannot say."""
    try:
        proc = git(
            repo, "merge-base", "--is-ancestor", "--end-of-options", older, newer, check=False
        )
    except GitError:
        return False
    return proc.returncode == 0


_LINKS: dict[Path, bytes] = {}  # each worktree this process made: its .git file as git wrote it


def relink(wt: Path) -> bool:
    """Put back the ``.git`` file of a worktree this process made, when the work
    changed it; whether it had to. A worktree it did not make is left as it is."""
    link = _LINKS.get(wt.resolve())
    if link is None:
        return False
    dot = wt / ".git"
    try:
        if not dot.is_symlink() and dot.is_file() and dot.read_bytes() == link:
            return False
        if dot.is_dir() and not dot.is_symlink():
            shutil.rmtree(dot)
        elif dot.is_symlink() or dot.exists():
            dot.unlink()
        dot.write_bytes(link)
    except OSError as exc:
        raise DeskGitError(
            f"cannot put back the worktree's .git file: {exc.strerror or exc}"
        ) from None
    return True


def create_worktree(
    repo: Path, run_id: str, label: str, base: str, *, detached: bool = False
) -> Path:
    """A new worktree for one desk, on a new branch from ``base``.

    ``detached`` makes it at ``base`` with no branch, for an advisor: it files
    nothing, so it has nothing for ``desks pick`` to bring in.
    """
    path = worktree_path(run_id, label)
    if path.exists():
        raise DeskGitError(f"{path} already exists")
    sha = resolve_commit(repo, base)
    private_dir(path.parent)
    how = ["--detach"] if detached else ["-b", desk_branch(run_id, label)]
    try:
        git(repo, "worktree", "add", "--quiet", *how, str(path), sha)
        _LINKS[path.resolve()] = (path / ".git").read_bytes()
    except GitError as exc:
        raise DeskGitError(f"cannot make the worktree for {label}: {exc}") from None
    except OSError as exc:
        raise DeskGitError(f"cannot read the new worktree's .git file: {exc}") from None
    return path


def _plain(text: str) -> str:
    return "".join(ch if ch.isprintable() else " " for ch in text)


def commit_message(desk: str, task: str, run_id: str) -> tuple[str, str]:
    """The subject from the task's first line, and the run id line.

    A commit is a record, and a branch may be pushed: every secret a record must not keep
    is taken out of the line before it is cut to length.
    """
    from metabot.connections import record_secrets
    from metabot.receipts import scrub

    first = next((line.strip() for line in task.splitlines() if line.strip()), "")
    first = scrub(first, record_secrets())
    subject = _plain(f"{desk}: {first}" if first else desk)[:SUBJECT_LIMIT].rstrip()
    return subject, f"{TRAILER}: {safe_id(run_id)}"


def commit_desk(
    wt: Path, desk: str, task: str, run_id: str, branch: str | None = None
) -> str | None:
    """Commit what a desk changed; the new commit's sha, or None when nothing changed."""
    relink(wt)
    if branch is not None:
        on = git(wt, "symbolic-ref", "--quiet", "HEAD", check=False).stdout.strip()
        if on != f"refs/heads/{branch}":
            raise DeskGitError(f"desk {desk} left its branch {branch}; its work was not committed")
    try:
        git(wt, "add", "--all", "--", ".", EXCLUDE)
        if git(wt, "diff", "--cached", "--quiet", check=False).returncode == 0:
            return None
        subject, trailer = commit_message(desk, task, run_id)
        git(wt, "commit", "--quiet", "--no-verify", "--no-gpg-sign", "-m", subject, "-m", trailer)
        return git(wt, "rev-parse", "HEAD").stdout.strip()
    except GitError as exc:
        raise DeskGitError(f"cannot commit desk {desk}'s work: {exc}") from None


def restore_worktree(wt: Path) -> None:
    """Put a desk's worktree back to its last commit, once the desk's work is committed.

    What ran after the desk, such as a gate's checks, is not the desk's work,
    and an advisor files nothing: such files never reach a branch, and the
    worktree can be removed later. Ignored files and the desk's inbox stay.
    """
    relink(wt)
    try:
        git(wt, "reset", "--quiet", "--hard", "HEAD")
        git(wt, "clean", "-d", "--force", "--quiet", f"--exclude=/{HANDOFF_DIR}")
    except GitError as exc:
        raise DeskGitError(f"cannot put the worktree back to its commit: {exc}") from None


def diff_stat(repo: Path, base: str, head: str) -> tuple[int, int, int]:
    """Files changed, lines added and lines removed from ``base`` to ``head``."""
    files = added = removed = 0
    out = git(repo, "diff", "--numstat", "--no-renames", "--end-of-options", base, head).stdout
    for line in out.splitlines():
        parts = line.split("\t", 2)
        if len(parts) < 3:
            continue
        files += 1
        added += int(parts[0]) if ascii_digits(parts[0]) else 0
        removed += int(parts[1]) if ascii_digits(parts[1]) else 0
    return files, added, removed


@dataclass(frozen=True)
class DeskBranch:
    branch: str
    run_id: str
    label: str
    sha: str
    subject: str
    stat: tuple[int, int, int] | None  # against where it left the person's HEAD


def list_desks(repo: Path) -> list[DeskBranch]:
    """Every desk branch in the repository, with what a pick would bring in."""
    fmt = "%(refname)%00%(objectname)%00%(subject)"
    out = git(repo, "for-each-ref", f"--format={fmt}", f"refs/heads/{BRANCH_ROOT}/").stdout
    head = git(repo, "rev-parse", "--verify", "--quiet", "HEAD", check=False).stdout.strip()
    rows: list[DeskBranch] = []
    for line in out.splitlines():
        ref, sha, subject = ([*line.split("\0"), "", ""])[:3]
        parts = ref[len("refs/heads/") :].split("/")
        if len(parts) != 3 or not SHA.match(sha):
            continue
        stat = None
        if SHA.match(head):
            base = git(repo, "merge-base", head, sha, check=False).stdout.strip()
            if SHA.match(base):
                stat = diff_stat(repo, base, sha)
        rows.append(DeskBranch("/".join(parts), parts[1], parts[2], sha, subject, stat))
    return rows


def _git_path(repo: Path, name: str) -> Path:
    return Path(git(repo, "rev-parse", "--path-format=absolute", "--git-path", name).stdout.strip())


def checkout_problem(repo: Path) -> str | None:
    """Why the person's checkout cannot take a pick now, or None."""
    for name, what in IN_PROGRESS.items():
        if _git_path(repo, name).exists():
            return f"{what} is in progress"
    if git(repo, "status", "--porcelain=v1", "--untracked-files=no").stdout.strip():
        return "the checkout has changes; commit or stash them first"
    return None


def pick(repo: Path, branch: str) -> list[str]:
    """Stage a desk branch's work as one squash in the person's checkout; never commit."""
    if not branch.startswith(f"{BRANCH_ROOT}/"):
        raise DeskGitError(f"only {BRANCH_ROOT}/ branches are picked, not {branch}")
    sha = resolve_commit(repo, f"refs/heads/{branch}")
    problem = checkout_problem(repo)
    if problem:
        raise DeskGitError(f"cannot pick {branch}: {problem}")
    proc = git(repo, "merge", "--squash", sha, check=False)
    if proc.returncode != 0:
        conflicts = git(repo, "diff", "--name-only", "--diff-filter=U").stdout.split()
        if conflicts:
            raise DeskGitError(
                f"{branch} conflicts with your checkout in: {', '.join(conflicts)}. "
                f"Nothing was committed. Resolve them, or undo with: {UNDO}"
            )
        said = (proc.stderr.strip().splitlines() or ["git merge failed"])[-1]
        raise DeskGitError(f"cannot pick {branch}: {said}")
    out = git(repo, "diff", "--cached", "--name-only", "-z").stdout
    return [name for name in out.split("\0") if name]


@dataclass
class Cleaned:
    removed: list[str] = field(default_factory=list)  # worktree paths and branch names
    kept: dict[str, str] = field(default_factory=dict)  # what stayed, and why


def _worktrees(repo: Path) -> list[tuple[Path, str | None]]:
    """Every worktree of the repository: its path and its branch ref, if any."""
    out = git(repo, "worktree", "list", "--porcelain", "-z").stdout
    found: list[tuple[Path, str | None]] = []
    path: Path | None = None
    branch: str | None = None
    for line in out.split("\0"):
        if line.startswith("worktree "):
            if path is not None:
                found.append((path, branch))
            path, branch = Path(line[len("worktree ") :]), None
        elif line.startswith("branch "):
            branch = line[len("branch ") :]
    if path is not None:
        found.append((path, branch))
    return found


def run_items(repo: Path, run_id: str) -> tuple[list[tuple[Path, str | None]], list[str]]:
    """A run's worktrees (path, branch) and its branch names.

    An advisor's worktree has no branch: it is the run's because MetaBot made
    it in the run's own folder.
    """
    prefix = f"refs/heads/{BRANCH_ROOT}/{safe_id(run_id)}/"
    refs = git(repo, "for-each-ref", "--format=%(refname)", prefix).stdout.splitlines()
    branches = [ref[len("refs/heads/") :] for ref in refs if ref.startswith(prefix)]
    home = desks_root(run_id).resolve()
    trees: list[tuple[Path, str | None]] = []
    for path, ref in _worktrees(repo):
        if ref and ref.startswith(prefix):
            trees.append((path, ref[len("refs/heads/") :]))
        elif ref is None and path.resolve().parent == home:
            trees.append((path, None))
    return trees, branches


def clean(repo: Path, run_id: str, confirm: Callable[[list[str]], bool]) -> Cleaned:
    """Remove a run's desk worktrees and branches, after ``confirm`` agrees to the list."""
    trees, branches = run_items(repo, run_id)
    home = desks_root(run_id).resolve()
    items = [str(path) for path, _ in trees] + branches
    result = Cleaned()
    if not items or not confirm(items):
        return result
    busy: set[str] = set()
    for path, branch in trees:
        if branch is not None and not path.resolve().is_relative_to(home):
            result.kept[branch] = f"checked out at {path}, which MetaBot did not make"
            busy.add(branch)
            continue
        handoff = path / HANDOFF_DIR
        if handoff.is_symlink():
            handoff.unlink()
        elif handoff.is_dir():
            shutil.rmtree(handoff)
        proc = git(repo, "worktree", "remove", str(path), check=False)
        if proc.returncode == 0:
            result.removed.append(str(path))
        else:
            result.kept[str(path)] = "it has work that is not committed"
            if branch is not None:
                result.kept[branch] = "its worktree was kept"
                busy.add(branch)
    git(repo, "worktree", "prune", check=False)
    for branch in branches:
        if branch in busy:
            continue
        proc = git(repo, "branch", "--delete", "--force", branch, check=False)
        if proc.returncode == 0:
            result.removed.append(branch)
        else:
            said = (proc.stderr.strip().splitlines() or ["git branch failed"])[-1]
            result.kept[branch] = said
    if home.is_dir() and not any(home.iterdir()):
        home.rmdir()
    return result
