"""What the commands share: their exit codes, the prompt they were given, and
the tools ``--allow`` names. It imports no other command module, so any of them
can use it.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from metabot.connections import ConnectionStore

EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_CANCELLED = 0, 1, 2, 130


def resolve_allows(raw: list[str], engine: Any, store: ConnectionStore) -> frozenset[str]:
    """``gh__create_issue`` -> the connection's DID form; ``Edit`` -> ``<prefix>/Edit``."""
    out: set[str] = set()
    for item in raw:
        if "#" in item or "/" in item:
            out.add(item)
            continue
        conn_name, sep, capability = item.partition("__")
        if sep:
            try:
                out.add(f"{store.get(conn_name).id}#{capability}")
                continue
            except KeyError:
                # no connection by that name, so it is one of the engine's own tools
                pass
        out.add(f"{engine.tool_prefix}/{item}")
    return frozenset(out)


def given_folder(raw: str | None) -> tuple[Path, bool]:
    """The folder a command was given, else the current one, resolved, and whether it
    is a folder to work in: a loop of links, or one past a folder that cannot be
    entered, is not."""
    try:
        path = Path(raw or os.getcwd())
    except OSError:  # the current folder was removed
        return Path(raw or "."), False
    try:
        path = path.resolve()
        return path, path.is_dir()
    except (OSError, RuntimeError):  # RuntimeError: a loop of links, before 3.13
        try:
            return Path(os.path.abspath(path)), False
        except OSError:  # a relative name, from a folder that was removed
            return path, False


def read_prompt(words: list[str]) -> str:
    if words == ["-"]:
        return sys.stdin.read()
    return " ".join(words)
