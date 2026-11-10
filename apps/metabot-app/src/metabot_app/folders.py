"""B6: the folders a desk or a run may work in.

``GET /api/dirs?under=<abs>[&all=1]`` lists a folder's subfolders for the page's
picker (dot folders only when asked; browsing starts at the home folder and may
not enter ~/.ssh, ~/.orcha, ~/.metabot, the accounts folder or any ~/.claude*).
``POST /api/folders {"add": path}`` / ``{"remove": path}`` edits ``project_roots``:
config.json is rewritten atomically with every other key kept, and the running
server's CFG is updated in place, so the folder works at once.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import catalog

LIST_LIMIT = 500


def _resolved(raw):
    if not isinstance(raw, str) or not raw or not os.path.isabs(raw) or "\x00" in raw:
        return None
    try:
        return Path(raw).resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return None


def _denied(p: Path, state_dir: str) -> bool:
    """A folder the page may never enter or choose (the home itself is handled by the caller)."""
    home = Path.home().resolve()
    for d in catalog.home_denylist(state_dir):
        try:
            d = d.resolve()
        except (OSError, RuntimeError):
            continue
        if p == d or d in p.parents:
            return True
    for par in [p, *p.parents]:
        if par.parent == home and par.name.startswith(".claude"):
            return True
    return False


def folder_ok(raw, state_dir: str):
    """The resolved folder a project root may be, or (None, why)."""
    p = _resolved(raw)
    if p is None or not p.is_dir():
        return None, "not a folder on this computer"
    if p == Path.home().resolve():
        return None, "the home folder itself cannot be a project; pick a folder in it"
    if _denied(p, state_dir):
        return None, "that folder holds keys or logins and cannot be a project"
    return p, ""


def list_dirs(raw, state_dir: str, show_all: bool = False):
    """(status, reply) for GET /api/dirs."""
    home = Path.home().resolve()
    if raw in (None, ""):
        p = home
    else:
        p = _resolved(raw)
        if p is None or not p.is_dir():
            return 400, {"ok": False, "error": "not a folder on this computer"}
    if p != home and _denied(p, state_dir):
        return 400, {"ok": False, "error": "that folder holds keys or logins"}
    dirs = []
    try:
        names = sorted(os.listdir(p))
    except OSError:
        return 400, {"ok": False, "error": "that folder cannot be read"}
    for name in names:
        if name.startswith(".") and not show_all:
            continue
        child = p / name
        try:
            if not child.is_dir() or child.is_symlink():
                continue
        except OSError:
            continue
        if _denied(child.resolve(), state_dir):
            continue
        dirs.append({"name": name, "path": str(child)})
        if len(dirs) >= LIST_LIMIT:
            break
    parent = None if p.parent == p else str(p.parent)
    return 200, {"ok": True, "under": str(p), "parent": parent, "dirs": dirs}


def write_roots(config_path: Path, raw_config: dict, roots: list[str]) -> None:
    """config.json with ``project_roots`` replaced and every other key as it was; atomic."""
    data = dict(raw_config)
    data["project_roots"] = list(roots)
    tmp = config_path.with_name(config_path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, config_path)


def edit(body: dict, cfg: dict, config_path: Path, raw_config: dict, state_dir: str, log=None):
    """(status, reply) for POST /api/folders; ``cfg`` is updated in place on success."""
    add, remove = body.get("add"), body.get("remove")
    if (add is None) == (remove is None):
        return 400, {"ok": False, "error": "say add or remove"}
    roots = [r for r in (cfg.get("project_roots") or []) if isinstance(r, str)]
    if add is not None:
        p, why = folder_ok(add, state_dir)
        if p is None:
            return 400, {"ok": False, "error": why}
        if str(p) not in roots:
            roots.append(str(p))
            kind = "folder_added"
        else:
            kind = None
        chosen = str(p)
    else:
        p = _resolved(remove)
        chosen = str(p) if p is not None else (remove if isinstance(remove, str) else "")
        if chosen not in roots:
            return 404, {"ok": False, "error": "not a project folder"}
        roots.remove(chosen)
        kind = "folder_removed"
    try:
        write_roots(config_path, raw_config, roots)
    except OSError:
        return 500, {"ok": False, "error": "config.json could not be written"}
    cfg["project_roots"] = roots
    if kind and log is not None:
        log(kind, folder=chosen)
    return 200, {"ok": True, "project_roots": roots}
