"""The process table (A4).

Linux has /proc; macOS does not. Everything that walks parents or reads a
process's command line asks here: /proc when it exists, else one ``ps`` call,
parsed into ``{pid: (ppid, args)}``. Nothing else about a process is read.
"""
from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

PROC = Path("/proc")
PS_ARGV = ["ps", "-eo", "pid=,ppid=,args="]


def parse(text: str) -> dict[int, tuple[int, str]]:
    """``ps -eo pid=,ppid=,args=`` output -> {pid: (ppid, args)}. Header lines and
    anything that is not ``<int> <int> ...`` are skipped."""
    out: dict[int, tuple[int, str]] = {}
    for line in text.splitlines():
        f = line.split(None, 2)
        if len(f) >= 2 and f[0].isdigit() and f[1].isdigit():
            out[int(f[0])] = (int(f[1]), f[2].strip() if len(f) == 3 else "")
    return out


def table() -> dict[int, tuple[int, str]]:
    """The live table from ``ps``; {} when ps is missing or slow."""
    try:
        r = subprocess.run(PS_ARGV, capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return {}
    return parse(r.stdout)


def children(tbl: dict[int, tuple[int, str]]) -> dict[int, list[int]]:
    """{ppid: [pid, ...]} in pid order."""
    out: dict[int, list[int]] = {}
    for pid in sorted(tbl):
        out.setdefault(tbl[pid][0], []).append(pid)
    return out


def ppid(pid: int, tbl: dict | None = None) -> int | None:
    """The parent pid: /proc/<pid>/stat when /proc exists, else the table. None when gone."""
    try:
        stat = (PROC / str(pid) / "stat").read_bytes().decode("ascii", "replace")
    except OSError:
        stat = None
    if stat is not None:
        tail = stat.rsplit(")", 1)[-1].split()
        return int(tail[1]) if len(tail) > 1 and tail[1].isdigit() else None
    if PROC.is_dir():
        return None          # /proc exists and the process does not
    row = (tbl if tbl is not None else table()).get(pid)
    return row[0] if row else None


def argv(pid: int, tbl: dict | None = None) -> list[str] | None:
    """The command line: /proc/<pid>/cmdline, else ``ps`` args split like a shell
    (a path with a space is lost there; macOS has no better source). None when gone."""
    try:
        raw = (PROC / str(pid) / "cmdline").read_bytes()
    except OSError:
        raw = None
    if raw is not None:
        return [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a]
    if PROC.is_dir():
        return None
    row = (tbl if tbl is not None else table()).get(pid)
    if not row:
        return None
    try:
        return shlex.split(row[1])
    except ValueError:
        return row[1].split()
