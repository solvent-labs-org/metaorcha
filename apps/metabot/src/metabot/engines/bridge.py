"""Bridges: an engine a developer adds without changing MetaBot.

A bridge is a program, named in ``config.json`` under ``bridges``::

    "bridges": {"echo": {"command": ["python3", "/path/to/echo.py"],
                         "models": ["m-1"], "tools": "own", "read_only": "no"}}

It runs as its own process, never inside MetaBot, and is handed nothing but
its job; it runs as the person, so it can read what they can. For each run
MetaBot writes a job file, ``bridge-job.json``, private to the person, and
runs ``command`` with the file's path as its last word. The job says
``metabot.bridge/1``: the prompt, the model, the folder, whether the job is
read-only, the switchboard command when the bridge takes MetaBot's
switchboard (``tools: switchboard``), and a session to resume.

The bridge writes one JSON object per line on stdout, each with a ``type``:

- ``started``: ``session_id``, ``model``
- ``text``: ``text``
- ``tool_call``: ``call_id``, ``tool``, ``args`` (an object)
- ``tool_result``: ``call_id``, ``output``, ``ok``
- ``usage``: ``data`` (token counts), ``total``
- ``final``: ``text``, ``session_id``
- ``error``: ``message``
- ``notice``: ``text``

A ``usage`` line holds one request's token counts in ``data``:
``input_tokens``, ``output_tokens``, ``cache_read_input_tokens`` and
``cache_creation_input_tokens``, or ``prompt_tokens``, ``completion_tokens``
and ``prompt_tokens_details.cached_tokens``. Send one as each request ends,
so a cap acts as the run goes. With ``total`` true it holds the run's whole
so far instead: what it holds past the lines before it is added, priced as
one request, and what they counted stands. Send ``started`` with the model
before any ``usage``. A model it names with no price of its own is priced as
the model asked for. Tokens reported before a model is named are priced by
the model asked for, and when the one then named prices them otherwise, or
neither has a price, the run's cost is unknown, whatever model is named
after it.

A line that is not one of these is kept as text. MetaBot's gate decides
nothing a bridge does with its own tools: what it reports is recorded as it
reports it, and a call it reports with no result is recorded as one that may
have run. Calls through MetaBot's switchboard are gated, as for any engine.
A bridge reports a call it made through the switchboard with ``tool``
``metabot/`` and the tool's name: the run counts it toward its limits, and the
switchboard alone records it, so it is in the record once. Each call has an id
no other call in the run has: when two share one, which result was whose
cannot be told, and the call is recorded as one that may have run.
A bridge that does not say ``read_only: honored`` never gets a read-only job:
the run fails before the program starts, so an advisor, a read-only desk or a
routine never runs on it.

The receipt names the bridge, its command, and a digest of the command's
words and of each file a word names, so a receipt says which program ran.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from functools import cached_property
from pathlib import Path
from typing import Any

from metabot.engines.base import CliEngine, EngineJob
from metabot.events import (
    ErrorEvent,
    Event,
    Final,
    Notice,
    Started,
    TextEvent,
    ToolCall,
    ToolResult,
    Usage,
)
from metabot.paths import write_private
from metabot.sandbox import named_file

FORMAT = "metabot.bridge/1"
SWITCHBOARD_CALL = "metabot/"  # how a bridge names a call it made through the switchboard
JOB_FILE = "bridge-job.json"
NOT_GATED = (
    "{name} is a bridge: its own tools are not gated by MetaBot, and what it reports "
    "is recorded as it reports it"
)


def _text(obj: dict[str, Any], key: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{key} is not text")
    return value


def _maybe(obj: dict[str, Any], key: str) -> str | None:
    value = obj.get(key)
    return value if isinstance(value, str) else None


def digest_of(command: list[str]) -> str:
    """sha256 of the command's words, and of the bytes of each file a word names."""
    h = hashlib.sha256()
    for word in command:
        h.update(b"w\0" + word.encode("utf-8", "surrogateescape") + b"\0")  # a name's bytes
        path = named_file(word)
        if path is not None:
            try:
                h.update(b"f\0" + hashlib.sha256(path.read_bytes()).digest())
            except OSError:
                h.update(b"f\0unreadable")
    return h.hexdigest()


class BridgeEngine(CliEngine):
    supports_hooks = False
    loads_plugins = False

    def __init__(self, name: str, spec: dict[str, Any], source: str = "config") -> None:
        self.name = name
        self.command = [str(word) for word in spec["command"]]
        self.binary = self.command[0]
        self.tool_prefix = name
        self.models = [str(m) for m in spec.get("models", [])]
        self.tools = spec.get("tools", "own")
        # the switchboard records the calls made through it, which the bridge reports so
        self.skip_tools = (SWITCHBOARD_CALL,) if self.tools == "switchboard" else ()
        self.keeps_read_only = spec.get("read_only", "no") == "honored"
        self.read_only_mode = (
            "its own, as it says (read_only: honored)"
            if self.keeps_read_only
            else "none: it never takes a read-only turn"
        )
        self.also_sees = "whatever its own program reads"
        self.source = source

    @cached_property
    def digest(self) -> str:
        return digest_of(self.command)

    def command_files(self) -> list[Path]:
        """Each file a word of the command names, which the sandbox never hides."""
        return [p for p in map(named_file, self.command) if p is not None]

    @property
    def model(self) -> str | None:
        return self.models[0] if self.models else None

    @property
    def receipt_meta(self) -> dict[str, Any]:
        return {
            "bridge": {
                "name": self.name,
                "source": self.source,
                "command": self.command,
                "digest": self.digest,
            }
        }

    def prepare(self, job: EngineJob) -> EngineJob:
        if job.read_only and not self.keeps_read_only:
            raise ValueError(
                f"bridge {self.name} does not keep to read-only (read_only: honored), "
                "so it cannot run a read-only job"
            )
        assert job.work_dir is not None  # run_job gives every run its directory
        sent = {
            "format": FORMAT,
            "prompt": job.prompt,
            "model": job.model or self.model,
            "cwd": str(job.cwd),
            "read_only": job.read_only,
            "switchboard": job.switchboard if self.tools == "switchboard" else None,
            "resume": job.resume,
        }
        write_private(job.work_dir / JOB_FILE, json.dumps(sent, indent=2) + "\n")
        return replace(job)

    def argv(self, job: EngineJob) -> list[str]:
        assert job.work_dir is not None
        return [*self.command, str(job.work_dir / JOB_FILE)]

    def parse_event(self, obj: dict[str, Any], state: dict[str, Any]) -> list[Event]:
        kind = obj.get("type")
        if kind == "started":
            return [
                Started(_maybe(obj, "session_id"), _maybe(obj, "model")),
                Notice(NOT_GATED.format(name=self.name)),
            ]
        if kind == "text":
            return [TextEvent(_text(obj, "text"))]
        if kind == "tool_call":
            args = obj.get("args", {})
            if not isinstance(args, dict):
                raise ValueError("args is not an object")
            return [ToolCall(_text(obj, "call_id"), _text(obj, "tool"), args)]
        if kind == "tool_result":
            return [ToolResult(_text(obj, "call_id"), obj.get("output"), obj.get("ok") is True)]
        if kind == "usage":
            data = obj.get("data", {})
            if not isinstance(data, dict):
                raise ValueError("data is not an object")
            return [Usage(data, obj.get("total") is True)]
        if kind == "final":
            return [Final(_text(obj, "text"), _maybe(obj, "session_id"))]
        if kind == "error":
            return [ErrorEvent(f"{self.name}: {_text(obj, 'message')}")]
        if kind == "notice":
            return [Notice(f"{self.name}: {_text(obj, 'text')}")]
        raise ValueError(f"unknown event type {kind!r}")
