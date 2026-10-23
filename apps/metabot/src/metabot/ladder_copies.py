"""A copy of the project for each rung of a ladder, so a failed rung leaves nothing behind.

When the person's folder is in git, the ladder starts by taking a snapshot
of the project as it stands: HEAD, with the changes not yet committed and
the files git does not track but does not ignore. The snapshot is built
through an index of MetaBot's own, so the person's index, working tree and
branches are never touched. Ignored files, such as ``.venv`` or
``node_modules``, are not in it.

Each rung works in its own worktree of that snapshot, with no branch, at
``<METABOT_HOME>/desks/<ladder>/rung-<n>``, in the same subfolder the person
asked from. After the rung, what it changed is committed there, so the
checks judge exactly that, and the worktree goes back to that commit.

The rung that answers has its work brought back: the change from the
snapshot to its commit, applied to the person's working tree with
``git apply``, once ``git apply --check`` says it fits. The patch is kept
beside the ladder's record. When it does not fit, because the person changed
those files meanwhile, nothing is applied, and that rung's worktree stays
where it is, for the person to take the work by hand. Every other rung's
worktree is removed when the ladder ends. Nothing here makes a branch,
commits to the person's branches, or pushes.

A rung's work can change its copy's ``.git`` file, git's link from the copy
to the project's records. MetaBot puts the file back before each of its own
git commands in the copy (``desks.relink``), so its commit stays in the copy.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from metabot.desks import (
    EXCLUDE,
    SHA,
    DeskGitError,
    commit_desk,
    create_worktree,
    desks_root,
    relink,
    restore_worktree,
)
from metabot.gitcmd import GitError, git, toplevel
from metabot.paths import metabot_home, private_dir, safe_id

LABEL = "rung-{n}"


class CopyError(RuntimeError):
    """The ladder cannot work in copies here; it says why and works in place."""


def snapshot(repo: Path, ladder_id: str) -> str:
    """A commit of the project as it stands, made without touching the person's index or refs."""
    head = git(repo, "rev-parse", "--verify", "--quiet", "HEAD^{commit}", check=False)
    parent = head.stdout.strip() if head.returncode == 0 else ""
    with tempfile.TemporaryDirectory(prefix="metabot-snapshot-") as tmp:
        index = Path(tmp) / "index"
        try:
            if SHA.match(parent):
                git(repo, "read-tree", parent, index=index)
            git(repo, "add", "--all", "--", ".", EXCLUDE, index=index)
            tree = git(repo, "write-tree", index=index).stdout.strip()
            message = f"metabot ladder {safe_id(ladder_id)}: the project as the ladder began"
            how = ["-p", parent] if SHA.match(parent) else []
            sha = git(repo, "commit-tree", tree, *how, "-m", message).stdout.strip()
        except GitError as exc:
            raise CopyError(f"cannot take a snapshot of the project: {exc}") from None
    if not SHA.match(sha):
        raise CopyError("cannot take a snapshot of the project")
    return sha


@dataclass
class Copies:
    repo: Path  # the top of the person's working tree
    sub: Path  # the person's folder, relative to it
    ladder_id: str
    base: str  # the snapshot every rung starts from
    trees: dict[int, Path] = field(default_factory=dict)

    @classmethod
    def start(cls, cwd: Path, ladder_id: str) -> Copies | None:
        """Copies for a ladder asked from ``cwd``; None outside git. CopyError when git fails."""
        repo = toplevel(cwd)
        if repo is None:
            return None
        sub = cwd.resolve().relative_to(repo)
        return cls(repo, sub, ladder_id, snapshot(repo, ladder_id))

    def make(self, n: int) -> Path:
        """Rung ``n``'s worktree, at the snapshot; the folder it works in."""
        try:
            wt = create_worktree(
                self.repo, self.ladder_id, LABEL.format(n=n), self.base, detached=True
            )
        except DeskGitError as exc:
            raise CopyError(str(exc)) from None
        self.trees[n] = wt
        return wt / self.sub

    def keep(self, n: int, task: str, run_id: str) -> tuple[str | None, bool]:
        """Commit what rung ``n`` changed: its commit, or None when it changed nothing, and
        whether its work had changed the copy's ``.git`` file. CopyError when git fails."""
        wt = self.trees[n]
        try:
            relinked = relink(wt)
            return commit_desk(wt, LABEL.format(n=n), task, run_id), relinked
        except (DeskGitError, GitError) as exc:
            raise CopyError(str(exc)) from None

    def restore(self, n: int) -> None:
        """Put rung ``n``'s copy back to its commit. CopyError when git fails."""
        try:
            restore_worktree(self.trees[n])
        except (DeskGitError, GitError) as exc:
            raise CopyError(str(exc)) from None

    def patch_path(self) -> Path:
        return metabot_home() / "ladders" / f"{safe_id(self.ladder_id)}.patch"

    def bring_back(self, n: int, commit: str) -> tuple[bool, str]:
        """Apply rung ``n``'s work to the person's working tree: whether it was, and a line."""
        patch = self.patch_path()
        private_dir(patch.parent)
        git(self.repo, "diff", "--binary", "--no-renames", "--full-index",
            f"--output={patch}", self.base, commit)  # fmt: skip
        os.chmod(patch, 0o600)
        files = git(self.repo, "diff", "--name-only", "--no-renames", self.base, commit).stdout
        count = len(files.split())
        fits = git(self.repo, "apply", "--check", "--binary", str(patch), check=False)
        if fits.returncode == 0:
            done = git(self.repo, "apply", "--binary", str(patch), check=False)
            if done.returncode == 0:
                return True, f"rung {n}'s work is in your project: {count} file(s); patch {patch}"
            fits = done
        said = (fits.stderr.strip().splitlines() or ["git apply refused it"])[-1]
        return False, (
            f"rung {n}'s work did not apply to your project ({said}); nothing was changed. "
            f"It is in {self.trees[n]} at {commit[:12]}, and the patch is {patch}; "
            f"metabot desks clean {safe_id(self.ladder_id)} removes the copy"
        )

    def close(self, keep: int | None = None) -> list[Path]:
        """Remove every rung's worktree but ``keep``'s; the ones that could not go."""
        left: list[Path] = []
        for n, wt in self.trees.items():
            try:
                relink(wt)
            except DeskGitError:
                left.append(wt)
                continue
            if n == keep:
                continue
            if git(self.repo, "worktree", "remove", str(wt), check=False).returncode != 0:
                left.append(wt)
        git(self.repo, "worktree", "prune", check=False)
        home = desks_root(self.ladder_id)
        if home.is_dir() and not any(home.iterdir()):
            home.rmdir()
        return left
