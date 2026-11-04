"""Routines: a goal that runs on a schedule, with nobody there to ask.

A routine is saved only once it passes ``check_routine``, the platform's
routine rules adapted to one machine: each connection must be active, each
allow must name a capability one of the routine's connections lists, a
destructive capability can never be allowed, and each criterion must be one
every step can be judged on.

A firing is a row before it is a run. ``firings.jsonl`` gets a ``running``
row first, then the run starts, then its outcome is appended; the latest row
for a firing is its state. The run has no human to ask: a write the routine
does not allow is declined, and a destructive call always is, each with a
``declined: no human present`` verdict in the receipt. An engine whose own
tools the gate cannot decide, because it runs no hook, runs them read-only.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import os
import re
import secrets
import tempfile
from collections.abc import Callable, Iterator, Mapping
from dataclasses import asdict, dataclass, fields, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from emerge.criteria import SUPPORTED_CRITERIA
from emerge.journal import utc_now
from emerge.preimage import redact_credentials
from mcp_types import Tool

from metabot.approvals import DenyAll
from metabot.config import MODEL_RE, Config, ConfigError, configured_keys, load_config
from metabot.connections import (
    Connection,
    ConnectionFileError,
    ConnectionStore,
    merged_secrets,
)
from metabot.cron import Cron, CronError, zone
from metabot.engines import bridge_keeps_read_only, engine_names, get_engine, known_engine
from metabot.gate import Policy
from metabot.paths import metabot_home, new_run_id, receipts_dir, run_path, safe_id, write_private
from metabot.receipts import acceptance_of
from metabot.scope_classes import ScopeClass, is_recognised, is_valid_capability_id
from metabot.switchboard import classify
from metabot.upstream import misplaced_copy

ROUTINES = "routines.json"
FIRINGS = "firings.jsonl"
STATES = ("running", "sealed", "refused", "skipped", "error")
MAX_CRITERIA = 8
DEFAULT_TURNS = 50
MAX_TURNS = 10_000
GOAL_LIMIT = 50_000
REASON_LIMIT = 500
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
ID_SHAPE = re.compile(r"^rt-[0-9a-f]{12}$")  # a routine's id; a name never has its shape

ListTools = Callable[[Connection], list[Tool]]
MakeEngine = Callable[[str], Any]


class RoutineError(ValueError):
    """A routine that must not be saved; ``field`` names what is wrong."""

    def __init__(self, field: str, reason: str) -> None:
        super().__init__(f"{field}: {reason}")
        self.field = field
        self.reason = reason


@dataclass
class RoutineSpec:
    name: str
    goal: str
    engine: str
    model: str | None
    cwd: str
    connections: list[str]
    scope_allow: list[str]
    criteria: dict[str, bool]
    cron: str
    tz: str
    enabled: bool = True
    turns: int = DEFAULT_TURNS  # the most tool calls a firing may make
    budget: int | None = None  # the most tokens a firing may use
    plugins: bool = True  # the enabled plugins come along, pinned in the receipt


@dataclass(frozen=True)
class Firing:
    firing_id: str
    routine_id: str
    due: str | None  # the scheduled time in UTC; None for a firing a person asked for
    state: str  # one of STATES
    run_id: str | None
    reason: str | None
    at: str  # when this row was written
    receipt: str | None = None
    acceptance: str | None = None  # the declared_acceptance result, when criteria were declared


# ── the save check ──────────────────────────────────────────────────────────


def _whole(value: Any, low: int, high: int) -> bool:
    return type(value) is int and low <= value <= high


def _check_shape(spec: RoutineSpec) -> None:
    if not isinstance(spec.name, str) or not NAME_RE.match(spec.name):
        raise RoutineError(
            "name", "1-64 of A-Z, a-z, 0-9, '.', '_' and '-', starting with a letter or digit"
        )
    if ID_SHAPE.match(spec.name):  # a command given it would find the routine with that id
        raise RoutineError("name", f"{spec.name} has the shape of a routine id; choose another")
    if not isinstance(spec.goal, str) or not spec.goal.strip():
        raise RoutineError("goal", "a routine needs a goal")
    if len(spec.goal) > GOAL_LIMIT:
        raise RoutineError("goal", f"at most {GOAL_LIMIT} characters")
    if not known_engine(spec.engine):
        choices = ", ".join(engine_names())
        raise RoutineError("engine", f"unknown engine {spec.engine!r}; choose one of: {choices}")
    if not bridge_keeps_read_only(spec.engine):  # a routine's engine runs read-only
        raise RoutineError(
            "engine",
            f"bridge {spec.engine} does not keep to read-only (read_only: honored), "
            "and a routine runs read-only",
        )
    if spec.model is not None and not (isinstance(spec.model, str) and MODEL_RE.match(spec.model)):
        raise RoutineError("model", f"{spec.model!r} is not a model name")
    cwd = Path(str(spec.cwd))
    if not cwd.is_absolute() or not cwd.is_dir():
        raise RoutineError("cwd", f"{spec.cwd} is not a folder (give its full path)")
    try:
        tz = zone(spec.tz)
    except CronError as exc:
        raise RoutineError("tz", str(exc)) from None
    try:
        Cron.parse(spec.cron).next_after(datetime.now(UTC), tz)
    except CronError as exc:
        raise RoutineError("cron", str(exc)) from None
    if not _whole(spec.turns, 1, MAX_TURNS):
        raise RoutineError("turns", f"a whole number from 1 to {MAX_TURNS}")
    if spec.budget is not None and not _whole(spec.budget, 1, 10**12):
        raise RoutineError("budget", "a whole number of tokens above 0")


def _check_criteria(criteria: Any) -> None:
    if not isinstance(criteria, dict):
        raise RoutineError("criteria", "must be an object of name to true or false")
    if len(criteria) > MAX_CRITERIA:
        raise RoutineError("criteria", f"at most {MAX_CRITERIA} criteria")
    for key, value in criteria.items():
        if key not in SUPPORTED_CRITERIA:
            raise RoutineError("criteria", f"unsupported criterion: {key}")
        if not isinstance(value, bool):
            raise RoutineError("criteria", f"{key} must be true or false")


def _connections(refs: Any, store: ConnectionStore) -> dict[str, Connection]:
    if not isinstance(refs, list):
        raise RoutineError("connections", "must be a list of connection names")
    found: dict[str, Connection] = {}
    for ref in refs:
        try:
            conn = store.get(str(ref))
        except KeyError:
            raise RoutineError("connections", f"no connection named {ref!r}") from None
        if not conn.active:
            raise RoutineError("connections", f"connection {conn.name} is {conn.state}")
        refused = _misplaced(conn)
        if refused:
            raise RoutineError("connections", f"connection {conn.name}: {refused}")
        found[conn.id] = conn
    return found


def _misplaced(conn: Connection) -> str | None:
    """Why a connection's server never starts, as a plugin's copy outside this home."""
    return misplaced_copy(conn.command) if conn.transport == "stdio" and conn.command else None


def _allows(entries: Any, conns: Mapping[str, Connection], list_tools: ListTools) -> dict[str, str]:
    if not isinstance(entries, list):
        raise RoutineError("scope_allow", "must be a list of CONNECTION__CAPABILITY")
    by_name = {c.name: c for c in conns.values()}
    listed: dict[str, dict[str, Tool]] = {}
    classes: dict[str, str] = {}
    for entry in entries:
        text = str(entry)
        if "#" in text:
            did, _, capability = text.partition("#")
            conn = conns.get(did)
        else:
            name, sep, capability = text.partition("__")
            conn = by_name.get(name) if sep else None
        if conn is None:
            raise RoutineError(
                "scope_allow", f"{text} does not name one of this routine's connections"
            )
        if not is_valid_capability_id(capability):
            raise RoutineError("scope_allow", f"{text} is not a valid capability id")
        if conn.origins is not None:
            raise RoutineError(
                "scope_allow",
                f"{capability} acts in a browser, where each call's class is set by the origin "
                "it acts on, so a routine cannot allow it ahead",
            )
        if conn.id not in listed:
            try:
                listed[conn.id] = {t.name: t for t in list_tools(conn)}
            except Exception as exc:  # a server that does not answer cannot be checked
                raise RoutineError(
                    "connections", f"{conn.name} did not answer: {type(exc).__name__}"
                ) from None
        tool = listed[conn.id].get(capability)
        if tool is None:
            raise RoutineError("scope_allow", f"{capability} is not a capability of {conn.name}")
        cls = classify(conn, tool)
        if cls is ScopeClass.DESTRUCTIVE:
            why = (
                "is destructive"
                if is_recognised(capability)
                else "is not in the scope-class table, so it resolves to destructive,"
            )
            raise RoutineError(
                "scope_allow",
                f"{capability} {why} and can never be allowed on a routine "
                "(a destructive call always waits for a person)",
            )
        classes[f"{conn.id}#{capability}"] = cls.label
    return classes


def _checked(
    spec: RoutineSpec, store: ConnectionStore, list_tools: ListTools
) -> tuple[list[str], dict[str, str]]:
    _check_shape(spec)
    _check_criteria(spec.criteria)
    conns = _connections(spec.connections, store)
    return list(conns), _allows(spec.scope_allow, conns, list_tools)


def check_routine(
    spec: RoutineSpec, store: ConnectionStore, list_tools: ListTools
) -> dict[str, str]:
    """``{"<connection DID>#<capability>": class}`` for the allows, or RoutineError."""
    return _checked(spec, store, list_tools)[1]


def listing(store: ConnectionStore) -> ListTools:
    """Ask a connection's server for its tools, as ``metabot connect test`` does."""

    def list_tools(conn: Connection) -> list[Tool]:
        from metabot.upstream import Upstream

        async def ask() -> list[Tool]:
            with tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace") as errlog:
                async with Upstream(conn, store.secret(conn), errlog=errlog) as upstream:
                    return await upstream.list_tools()

        return asyncio.run(ask())

    return list_tools


# ── the routines file ───────────────────────────────────────────────────────


def _path() -> Path:
    return metabot_home() / ROUTINES


@contextlib.contextmanager
def _locked() -> Iterator[None]:
    """One change to the routines file at a time; readers see whole files."""
    fd = os.open(metabot_home() / f".{ROUTINES}.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _load() -> dict[str, dict[str, Any]]:
    path = _path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except RecursionError:  # what every reader of the routines catches
        raise ValueError(f"{path} is nested too deep to read") from None
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a routines file")
    return {k: v for k, v in data.items() if isinstance(v, dict)}


def _store(table: Mapping[str, Any]) -> None:
    write_private(_path(), json.dumps(table, indent=2, sort_keys=True) + "\n")


def routines() -> list[dict[str, Any]]:
    """Every saved routine, by name."""
    return sorted(_load().values(), key=lambda r: str(r.get("name")))


def _find(table: Mapping[str, dict[str, Any]], ref: str) -> dict[str, Any]:
    if ref in table:
        return table[ref]
    for record in table.values():
        if record.get("name") == ref:
            return record
    raise KeyError(f"no routine named {ref!r}")


def get_routine(ref: str) -> dict[str, Any]:
    """A routine by its id or its name; KeyError when there is none."""
    return _find(_load(), ref)


def save_routine(
    spec: RoutineSpec,
    *,
    store: ConnectionStore | None = None,
    list_tools: ListTools | None = None,
    routine_id: str | None = None,
) -> str:
    """Check, then save; returns the routine's id. With ``routine_id``, replaces that routine."""
    store = store or ConnectionStore()
    connection_ids, allows = _checked(spec, store, list_tools or listing(store))
    with _locked():
        table = _load()
        if routine_id is not None and routine_id not in table:
            raise KeyError(f"no routine named {routine_id!r}")
        rid = routine_id or f"rt-{secrets.token_hex(6)}"
        if any(r.get("name") == spec.name and k != rid for k, r in table.items()):
            raise RoutineError("name", f"a routine named {spec.name} already exists")
        table[rid] = {
            **asdict(spec),
            "id": rid,
            "connection_ids": connection_ids,
            "allows": allows,
            "saved_at": utc_now(),
        }
        _store(table)
    return rid


def set_enabled(ref: str, enabled: bool) -> dict[str, Any]:
    with _locked():
        table = _load()
        record = _find(table, ref)
        record["enabled"] = enabled
        _store(table)
    return record


def remove_routine(ref: str) -> dict[str, Any]:
    """Forget a routine; its firings and their receipts stay."""
    with _locked():
        table = _load()
        record = _find(table, ref)
        del table[record["id"]]
        _store(table)
    return record


def next_due(record: Mapping[str, Any], after: datetime) -> datetime:
    """The routine's first due time strictly after ``after``, in its own zone."""
    return Cron.parse(record["cron"]).next_after(after, zone(record["tz"]))


# ── firings ─────────────────────────────────────────────────────────────────

_FIRING_FIELDS = tuple(f.name for f in fields(Firing))


def _firings_path() -> Path:
    return metabot_home() / FIRINGS


def _append(firing: Firing) -> Firing:
    line = json.dumps(asdict(firing), sort_keys=True, separators=(",", ":")) + "\n"
    fd = os.open(_firings_path(), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)
    return firing


def _row(raw: Any) -> Firing | None:
    if not isinstance(raw, dict):
        return None
    values: dict[str, Any] = {k: raw.get(k) for k in _FIRING_FIELDS}
    try:
        firing = Firing(**values)
    except TypeError:
        return None
    ok = isinstance(firing.firing_id, str) and isinstance(firing.routine_id, str)
    return firing if ok and firing.state in STATES else None


def firings(routine_id: str | None = None) -> list[Firing]:
    """Each firing at its latest row, in the order the firings began."""
    path = _firings_path()
    if not path.exists():
        return []
    latest: dict[str, Firing] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            firing = _row(json.loads(line))
        except (ValueError, RecursionError):
            continue
        if firing is None or (routine_id is not None and firing.routine_id != routine_id):
            continue
        latest[firing.firing_id] = firing  # a later row replaces it in place
    return list(latest.values())


def utc_stamp(when: datetime) -> str:
    """A due time as the firings file keeps it: UTC, to the second."""
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def note_firing(routine_id: str, due: datetime | None, state: str, reason: str) -> Firing:
    """A firing that never ran: ``skipped``, or an ``error`` before any run."""
    return _append(
        Firing(
            f"fr-{secrets.token_hex(6)}",
            routine_id,
            None if due is None else utc_stamp(due),
            state,
            None,
            reason[:REASON_LIMIT],
            utc_now(),
        )  # fmt: skip
    )


def _sealed_receipt(run_id: str | None) -> Path | None:
    try:
        receipt = receipts_dir() / f"{safe_id(str(run_id))}.json"
    except ValueError:
        return None
    return receipt if receipt.is_file() else None


def _run_error(run_id: str) -> str | None:
    try:
        journal = json.loads((run_path(run_id) / "journal.json").read_text(encoding="utf-8"))
        error = journal["meta"].get("error")
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError):
        return None
    return str(error)[:REASON_LIMIT] if error else None


def sweep_restart() -> list[Firing]:
    """Settle the firings a stopped serve left ``running``.

    A firing whose run sealed keeps its receipt, as ``sealed``. One with
    nothing sealed is an ``error`` with reason ``restart``: it is not run again.
    """
    settled = []
    for firing in firings():
        if firing.state != "running":
            continue
        receipt = _sealed_receipt(firing.run_id)
        if receipt is None:
            row = replace(firing, state="error", reason="restart", at=utc_now())
        else:
            row = replace(
                firing, state="sealed", reason=_run_error(str(firing.run_id)), at=utc_now(),
                receipt=str(receipt), acceptance=acceptance_of(receipt),
            )  # fmt: skip
        settled.append(_append(row))
    return settled


def _cannot_fire(record: Mapping[str, Any], store: ConnectionStore) -> str | None:
    """Why the routine cannot fire now, or None. Its connections are read fresh."""
    for did in record.get("connection_ids") or []:
        try:
            conn = store.get(did)
        except KeyError:
            return f"connection {did} was removed"
        if not conn.active:
            return f"connection {conn.name} is {conn.state}"
        refused = _misplaced(conn)
        if refused:
            return f"connection {conn.name}: {refused}"
    if not Path(str(record.get("cwd"))).is_dir():
        return f"{record.get('cwd')} is not a folder"
    if not known_engine(record.get("engine")):
        return f"unknown engine {record.get('engine')!r}"
    return None


async def fire(
    routine_id: str,
    *,
    due: datetime | None = None,
    make_engine: MakeEngine | None = None,
    on_event: Callable[[Any], None] | None = None,
) -> Firing:
    """Fire a routine once: a ``running`` row, the run, then the run's outcome.

    ``due`` is the scheduled time; a firing a person asked for has none. The
    run's approver is ``DenyAll``: nobody is there, so the decider declines
    whatever would ask, and records that it did.
    """
    from metabot.plugins import enabled_plugins
    from metabot.runner import Limits, RunCancelled, run_job

    record = get_routine(routine_id)
    rid = str(record["id"])
    firing_id = f"fr-{secrets.token_hex(6)}"
    due_text = utc_stamp(due) if due is not None else None
    store = ConnectionStore()
    try:
        tokens, unreadable = store.redaction_map(), None
    except ConnectionFileError as exc:  # the run would need it: the firing is refused, on a row
        tokens, unreadable = {}, str(exc)
    try:
        cfg = load_config()
    except ConfigError as exc:  # so would the settings
        cfg, unreadable = Config(), unreadable or str(exc)
    tokens = merged_secrets(tokens, configured_keys(cfg))  # and each key they name

    def row(state: str, run_id: str | None = None, reason: str | None = None,
            receipt: Path | str | None = None) -> Firing:  # fmt: skip
        if reason is not None and tokens:
            reason = redact_credentials(reason, tokens)
        return _append(
            Firing(
                firing_id,
                rid,
                due_text,
                state,
                run_id,
                None if reason is None else reason[:REASON_LIMIT],
                utc_now(),
                None if receipt is None else str(receipt),
                None if receipt is None else acceptance_of(receipt),
            )  # fmt: skip
        )

    refused = unreadable or _cannot_fire(record, store)
    if refused is not None:
        return row("refused", reason=refused)
    run_id = new_run_id()
    row("running", run_id)
    try:
        engine = (make_engine or get_engine)(record["engine"])
        chosen = enabled_plugins() if record.get("plugins", True) else []
        meta: dict[str, Any] = {
            "routine": rid, "routine_name": record["name"], "firing": firing_id, "due": due_text,
        }  # fmt: skip
        if chosen:
            meta["plugins"] = [p.pin for p in chosen]
        connection_ids = list(record.get("connection_ids") or [])
        result = await run_job(
            engine=engine,
            prompt=record["goal"],
            cwd=Path(record["cwd"]),
            policy=Policy(
                allow_write=False, allows=frozenset(record.get("allows") or {}), interactive=False
            ),
            approver=DenyAll(),
            # Its own tools pass no gate, and nobody is there to ask about a write.
            read_only=not getattr(engine, "own_tools_gated", False),
            model=record.get("model") or cfg.models.get(record["engine"]),
            plugin_dirs=[p.view for p in chosen],
            switchboard=bool(connection_ids),
            connection_ids=connection_ids,
            criteria=dict(record.get("criteria") or {}),
            run_id=run_id,
            meta=meta,
            limits=Limits(
                max_tool_calls=record.get("turns") or DEFAULT_TURNS,
                max_tokens=record.get("budget"),
            ),
            on_event=on_event,
            project=Path(record["cwd"]) if cfg.project_receipts is True else None,
        )
    except RunCancelled as exc:  # stopped: the receipt was still sealed
        row("error", run_id, "stopped", exc.result.receipt)
        raise
    except asyncio.CancelledError:  # stopped before the run began
        row("error", run_id, "stopped")
        raise
    except Exception as exc:  # the engine could not start, or the run broke once sealed
        return row("error", run_id, f"{type(exc).__name__}: {exc}", _sealed_receipt(run_id))
    return row("sealed", run_id, result.error, result.receipt)
