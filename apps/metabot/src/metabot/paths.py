"""Where MetaBot keeps its state, and how it writes private files.

Everything lives under one home directory: ``$METABOT_HOME``, else
``~/.metabot``. Directories are created 0700; secrets, journals and the
config are written 0600, atomically.
"""

from __future__ import annotations

import contextlib
import os
import re
import secrets
import tempfile
import time
from pathlib import Path


def private_dir(path: Path) -> Path:
    """Create ``path`` (and parents) and make it owner-only.

    A parent it has to make is owner-only too, whatever the umask; a parent
    that is there already keeps its mode.
    """
    if not path.parent.exists():
        private_dir(path.parent)
    path.mkdir(mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def _home_path() -> Path:
    raw = os.environ.get("METABOT_HOME")
    return Path(raw).expanduser() if raw else Path.home() / ".metabot"


def metabot_home() -> Path:
    return private_dir(_home_path())


def runs_dir() -> Path:
    return private_dir(metabot_home() / "runs")


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def safe_id(value: str, what: str = "run id") -> str:
    """A name that is safe as one path component: no separators, no dot-files."""
    if not isinstance(value, str) or not _SAFE_ID.match(value) or ".." in value:
        raise ValueError(f"unsafe {what}: {value!r}")
    return value


def run_dir(run_id: str) -> Path:
    """A run's folder, created: only for the run that writes it."""
    return private_dir(runs_dir() / safe_id(run_id))


def claim_run_dir(run_id: str) -> Path:
    """A new run's folder, created now: ValueError when the id is already in use."""
    safe_id(run_id)
    if (metabot_home() / "receipts" / f"{run_id}.json").exists():
        raise ValueError(f"run id in use: {run_id}")
    path = runs_dir() / run_id
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        raise ValueError(f"run id in use: {run_id}") from None
    os.chmod(path, 0o700)
    return path


def run_path(run_id: str) -> Path:
    """A run's folder for a lookup: nothing is created, so a typo leaves no trace."""
    return _home_path() / "runs" / safe_id(run_id)


def receipts_dir() -> Path:
    return private_dir(metabot_home() / "receipts")


def secrets_dir() -> Path:
    return private_dir(metabot_home() / "secrets")


def new_run_id() -> str:
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return f"mb-{stamp}-{secrets.token_hex(3)}"


def write_private(path: Path, data: str) -> None:
    """Write ``data`` to ``path`` atomically with mode 0600."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise
