"""The chat inbox seam.

Out of the box the app reaches a desk by typing into the CLI its terminal runs (the
pane) and shows the CLI's answers in the desk's lane. An inbox adapter adds a second
road: a chat service of the user's own that a desk's agent can hold ("arm") and that
carries messages both ways. The app ships no adapter; ``NullInbox`` is the default.

An adapter is one ``.py`` file outside the package, named by the config key
``inbox_module`` (an absolute path). It defines ``make_inbox(ctx) -> Inbox``. A
missing or broken file logs one line and the app runs with ``NullInbox``.

The protocol (every method is called from server threads; none may block for long):

  meta() -> dict                 shown to the page as META.inbox: ``name`` (what the
                                 page calls the inbox), ``arm_hint`` (one line on what
                                 arming does), ``arm_command`` (what a person types in a
                                 session to arm it, ``{desk}`` filled in); {} = none
  start(ctx) -> None             start any background readers (called once, lazily,
                                 when the first page subscribes to the event stream)
  arm(pane) -> dict              arm the desk ``pane``: {"ok": bool, "error": str}
  deliver(desk, text) -> dict | None
                                 deliver one message; None = "not mine, use the
                                 normal path" (the pane, or the lane)
  publish(desk, text) -> dict    send one message on the inbox: {"ok", "error"}
  tree_bucket(rows) -> dict | None
                                 the side-tree group for desks known only to the
                                 inbox: {"label": str, "desks": [rows]}; None = none
  names_dirs() -> list[Path]     extra session-name folders (files named by a Claude
                                 session id or a desk name, holding the desk name)

Optional (missing = the NullInbox answer):

  armed() -> dict                desk name -> {"armed_at": int, "n": int}: desks that
                                 hold the inbox now
  streams() -> dict              stream name -> connected (bool), for the status line
  history() -> list[dict]        recent inbox events, oldest first, already tagged:
                                 {"via", "time", "title", "message", "target", "from"}
                                 (``claim: true`` marks a receipt that the desk took
                                 an earlier message)
  close() -> None                stop background readers (the app reconfigured)

The ``ctx`` an adapter gets (``InboxCtx``) carries the config, the state folder and
the few server calls an adapter needs: ``emit`` (push an event to every open page),
``sessions`` (the desk names known now), ``paste`` (type a line into a desk's CLI,
with the server's safety rules), ``desk_slug`` and ``log``.
"""
from __future__ import annotations

import hashlib
import importlib.util
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol, runtime_checkable

REQUIRED = ("meta", "start", "arm", "deliver", "publish", "tree_bucket", "names_dirs")
NO_INBOX = "no chat inbox is set up on this PC"


@runtime_checkable
class Inbox(Protocol):
    def meta(self) -> dict: ...

    def start(self, ctx: "InboxCtx") -> None: ...

    def arm(self, pane: str) -> dict: ...

    def deliver(self, desk: str, text: str) -> dict | None: ...

    def publish(self, desk: str, text: str) -> dict: ...

    def tree_bucket(self, rows: list) -> dict | None: ...

    def names_dirs(self) -> list[Path]: ...


@dataclass
class InboxCtx:
    """What an adapter may use. ``config`` is the whole config file as read (an
    adapter keeps its own keys there); ``cfg`` is the app's checked config."""
    cfg: dict
    config: dict
    state: Path
    home: Path
    emit: Callable[[dict], None] = lambda ev: None
    sessions: Callable[[], list] = lambda: []
    paste: Callable[[str, str], tuple] = lambda desk, text: (False, NO_INBOX)
    desk_slug: Callable[[str], str] = lambda name: name
    log: Callable[[str], None] = lambda msg: None
    default_desk: str = ""
    extra: dict = field(default_factory=dict)


class NullInbox:
    """No inbox: nothing is armed, nothing is delivered, nothing is shown."""

    def meta(self) -> dict:
        return {}

    def start(self, ctx) -> None:
        return None

    def arm(self, pane) -> dict:
        return {"ok": False, "error": NO_INBOX}

    def deliver(self, desk, text):
        return None

    def publish(self, desk, text) -> dict:
        return {"ok": False, "error": NO_INBOX}

    def tree_bucket(self, rows):
        return None

    def names_dirs(self) -> list:
        return []

    def armed(self) -> dict:
        return {}

    def streams(self) -> dict:
        return {}

    def history(self) -> list:
        return []

    def close(self) -> None:
        return None


_NULL = NullInbox()


def _result(v) -> dict:
    """An adapter's {"ok", "error"} reply, normalised."""
    if not isinstance(v, dict):
        return {"ok": False, "error": "the inbox gave no answer"}
    return {"ok": v.get("ok") is True, "error": str(v.get("error") or "")}


class Guarded:
    """An adapter behind a guard: every call that raises or answers in the wrong
    shape is the NullInbox answer, and logs one line per method (the first time),
    so a bad adapter never takes a route down."""

    def __init__(self, inner: Any, label: str, log: Callable[[str], None]):
        self.inner = inner
        self.label = label
        self._log = log
        self._warned: set = set()

    def _call(self, name: str, *args):
        fn = getattr(self.inner, name, None)
        if not callable(fn):
            return getattr(_NULL, name)(*args)
        try:
            return fn(*args)
        except Exception as exc:
            if name not in self._warned:
                self._warned.add(name)
                self._log(f"inbox adapter {self.label}: {name} failed ({type(exc).__name__}); "
                          "its NullInbox answer is used")
            return getattr(_NULL, name)(*args)

    def meta(self) -> dict:
        m = self._call("meta")
        if not isinstance(m, dict):
            return {}
        out = {k: v for k, v in m.items()
               if isinstance(k, str) and isinstance(v, (str, int, float, bool))}
        return out if isinstance(out.get("name"), str) and out["name"] else {}

    def start(self, ctx) -> None:
        self._call("start", ctx)

    def arm(self, pane) -> dict:
        return _result(self._call("arm", pane))

    def deliver(self, desk, text):
        v = self._call("deliver", desk, text)
        return None if v is None else _result(v)

    def publish(self, desk, text) -> dict:
        return _result(self._call("publish", desk, text))

    def tree_bucket(self, rows):
        b = self._call("tree_bucket", rows)
        if not isinstance(b, dict) or not isinstance(b.get("desks"), list):
            return None
        label = b.get("label")
        return {"label": label if isinstance(label, str) and label else "Inbox",
                "desks": [d for d in b["desks"] if isinstance(d, dict)]}

    def names_dirs(self) -> list:
        v = self._call("names_dirs")
        if not isinstance(v, (list, tuple)):
            return []
        return [Path(p) for p in v if isinstance(p, (str, Path)) and str(p)]

    def armed(self) -> dict:
        v = self._call("armed")
        if not isinstance(v, dict):
            return {}
        out = {}
        for name, w in v.items():
            if not isinstance(name, str) or not name:
                continue
            w = w if isinstance(w, dict) else {}
            at, n = w.get("armed_at", 0), w.get("n", 1)
            out[name] = {"armed_at": at if isinstance(at, int) and not isinstance(at, bool) else 0,
                         "n": n if isinstance(n, int) and not isinstance(n, bool) and n > 0 else 1}
        return out

    def streams(self) -> dict:
        v = self._call("streams")
        if not isinstance(v, dict):
            return {}
        return {str(k): bool(c) for k, c in v.items()}

    def history(self) -> list:
        v = self._call("history")
        if not isinstance(v, list):
            return []
        return [dict(e) for e in v if isinstance(e, dict) and isinstance(e.get("message"), str)]

    def close(self) -> None:
        self._call("close")


def load(path, ctx: InboxCtx, log: Callable[[str], None]):
    """The inbox the config names, else NullInbox. ``path`` is the ``inbox_module``
    value; any failure (not absolute, missing, will not import, no
    ``make_inbox``, ``make_inbox`` raises, a required method missing) logs exactly
    one line and returns NullInbox."""
    if not path:
        return NullInbox()
    p = Path(str(path)).expanduser()

    def refuse(why):
        log(f"inbox adapter {p} not loaded ({why}); chat uses the terminal only")
        return NullInbox()

    if not p.is_absolute():
        return refuse("inbox_module must be an absolute path")
    if not p.is_file():
        return refuse("no such file")
    name = "metabot_inbox_" + hashlib.sha256(str(p).encode()).hexdigest()[:12]
    try:
        spec = importlib.util.spec_from_file_location(name, p)
        if spec is None or spec.loader is None:
            return refuse("not a Python file")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod   # registered as an import is (dataclasses and pickling look it up)
        spec.loader.exec_module(mod)
    except Exception as exc:
        sys.modules.pop(name, None)
        return refuse(f"import failed: {type(exc).__name__}")
    make = getattr(mod, "make_inbox", None)
    if not callable(make):
        return refuse("no make_inbox(ctx)")
    try:
        obj = make(ctx)
    except Exception as exc:
        return refuse(f"make_inbox failed: {type(exc).__name__}")
    missing = [m for m in REQUIRED if not callable(getattr(obj, m, None))]
    if missing:
        return refuse("missing " + ", ".join(missing))
    return Guarded(obj, p.name, log)
