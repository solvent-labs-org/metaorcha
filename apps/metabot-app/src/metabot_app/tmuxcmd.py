"""Every tmux command line is built here (A5).

A fresh install talks to its own tmux server, socket ``metabot`` (``tmux -L
metabot``), so the app never lists, attaches to or kills a session the user
started by hand. Config ``tmux_socket`` names it; "" means the user's default
server (what an existing install had). ``version()`` tells doctor whether tmux
is 3.2 or newer, which ``new-session -e`` needs.
"""
from __future__ import annotations

import re
import shutil
import subprocess

from . import paths

BIN = "tmux"
DEFAULT_SOCKET = "metabot"
MIN_VERSION = (3, 2)
_SOCKET_RX = re.compile(r"[A-Za-z0-9_.-]{1,40}")
_state = {"socket": DEFAULT_SOCKET}


def configure(socket) -> str:
    """Set the socket name from config. "" = the default server; anything that is
    not a plain name (letters, digits, ``_ . -``, up to 40) becomes ``metabot``."""
    if socket == "":
        _state["socket"] = ""
    elif isinstance(socket, str) and _SOCKET_RX.fullmatch(socket) and ".." not in socket:
        _state["socket"] = socket
    else:
        _state["socket"] = DEFAULT_SOCKET
    return _state["socket"]


def socket() -> str:
    return _state["socket"]


def argv(*args) -> list[str]:
    """``["tmux", "-L", <socket>, *args]``, or without ``-L`` for the default server."""
    sock = _state["socket"]
    return [BIN, *(["-L", sock] if sock else []), *[str(a) for a in args]]


def found() -> str | None:
    """Where tmux is (login PATH first), or None."""
    return shutil.which(BIN, path=paths.search_path())


def _run(cmd: list[str], timeout: float) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return 127, ""
    return r.returncode, r.stdout or ""


def parse_version(text: str) -> tuple[int, int] | None:
    """``tmux 3.4`` / ``tmux next-3.5`` / ``tmux 3.2a`` -> (major, minor); None otherwise."""
    m = re.search(r"(\d+)\.(\d+)", text or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def version(timeout: float = 3.0) -> tuple[int, int] | None:
    rc, out = _run(argv("-V"), timeout)
    return parse_version(out) if rc == 0 else None


def new_enough(v) -> bool:
    return bool(v) and tuple(v) >= MIN_VERSION
