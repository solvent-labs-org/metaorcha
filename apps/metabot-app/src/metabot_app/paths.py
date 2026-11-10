"""Where MetaBot's binaries are (A2).

The app is installed next to the core: ``uv tool install metaorcha-app`` puts
``metabot-app``, ``metabot`` and ``orcha`` in one folder, and the desktop app
installs both into its own folder. So a binary is looked for beside the app's
own interpreter first, then on PATH. Nothing here names a folder on the
developer's machine.
"""
from __future__ import annotations

import functools
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path


def _runnable(p: Path) -> bool:
    return p.is_file() and os.access(p, os.X_OK)


@functools.lru_cache(maxsize=None)
def login_path(timeout: float = 3.0) -> str:
    """The PATH the user's login shell has (A3). An app opened from a desktop
    launcher, Finder above all, gets a bare PATH, which would make ``tmux`` or
    ``claude`` look missing. Asked once per process: ``$SHELL -lc 'printf %s "$PATH"'``.
    A missing shell, a failure, an empty answer or a timeout gives the process PATH."""
    fallback = os.environ.get("PATH") or os.defpath
    shell = os.environ.get("SHELL") or ""
    if not shell or not _runnable(Path(shell)):
        return fallback
    try:
        proc = subprocess.Popen([shell, "-lc", 'printf %s "$PATH"'], stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                                start_new_session=True)
    except OSError:
        return fallback
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        proc.communicate()
        return fallback
    out = (out or "").strip()
    return out if proc.returncode == 0 and out else fallback


def search_path() -> str:
    """The login shell's PATH first, then any process PATH entries it lacks."""
    seen, parts = set(), []
    for entry in login_path().split(os.pathsep) + (os.environ.get("PATH") or "").split(os.pathsep):
        if entry and entry not in seen:
            seen.add(entry)
            parts.append(entry)
    return os.pathsep.join(parts)


def find_bin(name: str) -> str | None:
    """The binary beside ``sys.executable`` when it is there and runnable, else the
    first runnable one on the search path (login shell PATH, then the process
    PATH), else None."""
    beside = Path(sys.executable).parent / name
    if _runnable(beside):
        return str(beside)
    return shutil.which(name, path=search_path())


def resolve(raw, name: str | None, fallback: str | None = None) -> str:
    """A config value for a binary. Empty (or not a string) means find it:
    ``find_bin(name)``, else ``fallback``, else the bare ``name`` (so the error
    that follows still says what was tried). A path starting with ``~`` is
    expanded, because a subprocess never expands one. Anything else is kept."""
    if isinstance(raw, str) and raw:
        return os.path.expanduser(raw) if raw.startswith("~") else raw
    found = find_bin(name) if name else None
    if found:
        return found
    if fallback:
        return fallback
    return name or ""
