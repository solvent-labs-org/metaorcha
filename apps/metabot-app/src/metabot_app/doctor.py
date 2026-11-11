"""B5: doctor. What this machine has for a first run, as ten items the setup page
keys on: ``{"id", "ok", "blocker", "what", "fix"}``, in plain words, never an
exception's text. ``ok`` on the report means no blocker. Items:

  python  core  key  tmux  engine.claude  engine.codex  engine.cursor
  engine.local  sandbox  folder

A blocker is what stops the first run: no core, no signing key, no usable tmux
(off the phone), no folder to work in, or no engine of any kind. A missing
sign-in or sandbox is a note with its fix. The local model server is probed on
loopback only; a hosted address is never called by doctor.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from . import paths, tmuxcmd

IDS = ("python", "core", "key", "tmux", "engine.claude", "engine.codex", "engine.cursor",
       "engine.local", "sandbox", "folder")
PY_FLOOR = (3, 11)
TMUX_FLOOR = "3.2"
INSTALL = "uv tool install metaorcha"
CLI = {"engine.claude": ("claude", "Claude Code"), "engine.codex": ("codex", "Codex"),
       "engine.cursor": ("cursor-agent", "Cursor")}
DEFAULT_URL = "http://127.0.0.1:11434/v1"
PROBE_S = 1.5
LOOPBACK = ("127.0.0.1", "localhost", "::1", "[::1]")


def _item(id_, ok, what, fix="", blocker=False, configured=False):
    """configured: set up but not provable from here (a hosted model server the doctor
    never probes); the page's set-up step counts it, the finding still reads not ok."""
    return {"id": id_, "ok": bool(ok), "blocker": bool(blocker) and not ok, "what": what,
            "fix": fix if not ok else "", "configured": bool(configured)}


def key_path() -> Path:
    raw = os.environ.get("ORCHA_KEY_PATH")
    return Path(raw).expanduser() if raw else Path.home() / ".orcha" / "key"


def _core(ctx):
    from . import mb
    v = mb.versions(ctx)["metabot"]
    if v["ok"]:
        return _item("core", True, f"MetaBot {v['version']}")
    return _item("core", False, "MetaBot is not installed", f"run: {INSTALL}", blocker=True)


def _key():
    p = key_path()
    try:
        st = p.stat()
    except OSError:
        return _item("key", False, "no signing key yet", "run: metabot init (it makes your key)", blocker=True)
    if st.st_mode & 0o077:
        return _item("key", False, "the signing key is readable by others", "set its mode to 0600", blocker=True)
    return _item("key", True, "signing key present")


def _tmux(on_device: bool):
    if on_device:
        return _item("tmux", True, "not used on the phone")
    if not tmuxcmd.found():
        return _item("tmux", False, "tmux is not installed",
                     f"install tmux {TMUX_FLOOR} or newer (desks and runs need it)", blocker=True)
    v = tmuxcmd.version()
    if v is None:
        return _item("tmux", False, "tmux did not say its version",
                     f"install tmux {TMUX_FLOOR} or newer", blocker=True)
    text = f"{v[0]}.{v[1]}"
    if not tmuxcmd.new_enough(v):
        return _item("tmux", False, f"tmux {text} is too old", f"install tmux {TMUX_FLOOR} or newer", blocker=True)
    return _item("tmux", True, f"tmux {text}")


def _cli(id_):
    binary, label = CLI[id_]
    found = paths.find_bin(binary)
    if found:
        return _item(id_, True, f"{label} is installed")
    return _item(id_, False, f"{label} is not installed",
                 f"install {binary} and sign in, or use another engine")


def local_url(metabot_home) -> str:
    try:
        cfg = json.loads((Path(metabot_home) / "config.json").read_text(encoding="utf-8"))
        url = cfg.get("openai_base_url")
    except (OSError, ValueError, AttributeError):
        url = None
    return url.rstrip("/") if isinstance(url, str) and url else DEFAULT_URL


def _local(metabot_home):
    url = local_url(metabot_home)
    host = (urlsplit(url).hostname or "").lower()
    if host not in LOOPBACK:
        return _item("engine.local", False, "a model server off this machine is configured (not probed)",
                     "doctor only probes a local model server; the first run will show whether it answers",
                     configured=True)
    try:
        req = urllib.request.Request(url + "/models", headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=PROBE_S) as r:
            data = json.loads(r.read(200_000) or b"{}")
        models = data.get("data") if isinstance(data, dict) else None
        n = len(models) if isinstance(models, list) else 0
        return _item("engine.local", True, f"a local model server answers with {n} model{'s' if n != 1 else ''}")
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return _item("engine.local", False, "no local model server is answering",
                     "start a local model server, add an API key, or sign in to a CLI engine")


def _sandbox(on_device: bool):
    if on_device:
        return _item("sandbox", True, "not used on the phone")
    if paths.find_bin("bwrap"):
        return _item("sandbox", True, "bubblewrap is installed")
    return _item("sandbox", False, "bubblewrap is not installed",
                 "install bubblewrap to run the cursor engine in a sandbox")


def _folder(cfg):
    roots = [str(r) for r in (cfg.get("project_roots") or []) if isinstance(r, str)]
    for p in cfg.get("projects") or []:
        if isinstance(p, dict):
            roots += [str(x) for x in (p.get("paths") or []) if isinstance(x, str)]
    live = [r for r in roots if os.path.isdir(os.path.expanduser(r))]
    if live:
        return _item("folder", True, f"{len(live)} folder{'s' if len(live) != 1 else ''} to work in")
    return _item("folder", False, "no folder to work in", "add a folder in Setup", blocker=True)


def report(ctx, on_device: bool | None = None) -> dict:
    if on_device is None:
        on_device = os.environ.get("METABOT_ON_DEVICE") == "1" or hasattr(sys, "getandroidapilevel")
    py = sys.version_info[:2]
    items = [_item("python", py >= PY_FLOOR, f"Python {py[0]}.{py[1]}",
                   f"MetaBot needs Python {PY_FLOOR[0]}.{PY_FLOOR[1]} or newer", blocker=True),
             _core(ctx), _key(), _tmux(on_device)]
    engines = [_cli(i) for i in ("engine.claude", "engine.codex", "engine.cursor")]
    local = _local(getattr(ctx, "metabot_home", ""))
    if not local["ok"] and not local["configured"] and not any(e["ok"] for e in engines):   # a hosted server, unprobed, is still an engine
        local = _item("engine.local", False, local["what"],
                      "sign in to Claude Code, Codex or Cursor, add an API key, or start a local model server",
                      blocker=True)
    items += engines + [local, _sandbox(on_device), _folder(ctx.cfg)]
    return {"ok": not any(i["blocker"] for i in items), "items": items}
