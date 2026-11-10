"""Workflow files (``metabot.workflow/1``) and the store that keeps them by sha256.

A workflow is a JSON file: a name, the folder it works in, and steps that run
one after another. Each step is a run MetaBot already makes: an ``ask`` (on
an engine, a role, a login), a ``ladder`` (with its checks) or an ``office``
run. A workflow is addressed by the sha256 of its exact bytes, so the file one
person hands another is the file the other approves, and a change of one byte
is another workflow.

The file is read strictly: a key it does not know, a key given twice, or a
value of the wrong type is refused, naming where. A ``schedule`` and its
``ceiling`` are read and checked here and kept for later; nothing here fires
a workflow on a schedule or counts against a ceiling.

The store is ``<home>/workflows/``: each workflow's bytes as ``<sha256>.json``,
and ``index.json``, which says when each was stored. A name finds the newest
version stored under it; every older one stays, by its sha256. Reading a kept
workflow hashes its bytes again, and bytes changed since they were stored are
refused.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from emerge.journal import utc_now

from metabot.paths import metabot_home, private_dir, write_private

FORMAT = "metabot.workflow/1"
MAX_BYTES = 256 * 1024
MAX_STEPS = 16
MAX_ALLOWS = 32
MAX_CHECKS = 8
PROMPT_LIMIT = 50_000
ABOUT_LIMIT = 2_000
ROUNDS_MAX = 10
FIRINGS_MAX = 1_000_000
SHORT = 12  # the digits of a sha256 a list shows, and the fewest a reference may give
KINDS = ("ask", "ladder", "office")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
HEXISH = re.compile(rf"^[0-9a-f]{{{SHORT},64}}$")  # a name of this shape reads as a sha256
ENGINE_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

TOP = ("format", "name", "about", "cwd", "steps", "schedule", "ceiling")
COMMON = ("name", "kind", "prompt", "allow_write", "allow", "connections", "plugins", "max_cost")
FIELDS = {
    "ask": (*COMMON, "read_only", "engine", "model", "role", "login"),
    "ladder": (*COMMON, "read_only", "checks", "role"),
    "office": (
        *COMMON, "dir", "office_sha256", "desks", "checks", "until_checks", "rounds", "fan",
        "advisors",
    ),
}  # fmt: skip
SCHEDULE = ("cron", "tz")
CEILING = ("firings", "expires", "month_cap")


class WorkflowError(ValueError):
    """A file that is not a workflow MetaBot runs; ``field`` names where it is wrong."""

    def __init__(self, field: str, reason: str) -> None:
        super().__init__(f"{field}: {reason}")
        self.field = field
        self.reason = reason


@dataclass(frozen=True)
class Step:
    name: str
    kind: str  # one of KINDS
    prompt: str  # an office step's task
    allow_write: bool = False
    allow: tuple[str, ...] = ()
    connections: bool = True
    plugins: bool = True
    max_cost_musd: int | None = None  # one run's money cap, as --max-cost
    read_only: bool = False
    engine: str | None = None  # ask
    model: str | None = None  # ask
    role: str | None = None  # ask; a ladder's may be private alone
    login: str | None = None  # ask
    ladder_checks: tuple[str, ...] = ()  # ladder: commands judged on each rung's work
    dir: str | None = None  # office: its folder
    office_sha256: str | None = None  # office: its office.yaml, as the workflow names it
    desks: tuple[str, ...] | None = None  # office: the desks, in order; None for all
    office_checks: bool = False  # office: run its gate checks
    until_checks: bool = False  # office
    rounds: int | None = None  # office, with until_checks
    fan: bool = False  # office
    advisors: bool = True  # office, in a relay


@dataclass(frozen=True)
class Schedule:
    cron: str
    tz: str


@dataclass(frozen=True)
class Ceiling:
    firings: int | None = None
    expires: str | None = None  # YYYY-MM-DD
    month_cap_musd: int | None = None


@dataclass(frozen=True)
class Workflow:
    raw: bytes
    sha256: str
    name: str
    cwd: str
    steps: tuple[Step, ...]
    about: str | None = None
    schedule: Schedule | None = None
    ceiling: Ceiling | None = None
    path: Path | None = None  # where the store keeps it; None for a file not stored

    @property
    def short(self) -> str:
        return self.sha256[:SHORT]


# ── reading a file ──────────────────────────────────────────────────────────


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:  # a reader that keeps the first would see another workflow
            raise WorkflowError(key, "given twice")
        out[key] = value
    return out


def _constant(word: str) -> Any:
    raise WorkflowError("file", f"{word} is not a number JSON allows")


def _only(data: Mapping[str, Any], allowed: tuple[str, ...], where: str, what: str) -> None:
    for key in data:
        if key not in allowed:
            raise WorkflowError(
                f"{where}{key}", f"not a field of {what}; it takes {', '.join(allowed)}"
            )


def _flag(data: Mapping[str, Any], key: str, where: str, default: bool) -> bool:
    value = data.get(key, default)
    if not isinstance(value, bool):
        raise WorkflowError(f"{where}{key}", "true or false")
    return value


def _text(value: Any, where: str, limit: int) -> str:
    if not isinstance(value, str):
        raise WorkflowError(where, "must be text")
    if not value.strip():
        raise WorkflowError(where, "is empty")
    if len(value) > limit:
        raise WorkflowError(where, f"at most {limit} characters")
    return value


def _texts(value: Any, where: str, most: int, limit: int = 1000) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise WorkflowError(where, "must be a list of text")
    if len(value) > most:
        raise WorkflowError(where, f"at most {most}")
    return tuple(_text(item, f"{where}[{n}]", limit) for n, item in enumerate(value))


def dollars_musd(value: Any) -> int | None:
    """Dollars, given as text or a number, as whole micro-dollars; None when not an amount
    above zero. Never rounded up past what was given."""
    from metabot.spend import MUSD

    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        return None
    if not amount.is_finite() or int(amount * MUSD) < 1:
        return None
    return int(amount * MUSD)


def _money(value: Any, where: str) -> int:
    musd = dollars_musd(value)
    if musd is None:
        raise WorkflowError(where, 'an amount of dollars above 0, such as "2.00"')
    return musd


def _name(value: Any, where: str) -> str:
    if not isinstance(value, str) or not NAME_RE.match(value):
        raise WorkflowError(
            where, "1-64 of A-Z, a-z, 0-9, '.', '_' and '-', starting with a letter or digit"
        )
    return value


def _absolute(value: Any, where: str) -> str:
    if not isinstance(value, str) or "\0" in value or not os.path.isabs(value):
        raise WorkflowError(where, "an absolute path to a folder")
    return value


def _step(raw: Any, n: int) -> Step:
    where = f"steps[{n}]"
    if not isinstance(raw, dict):
        raise WorkflowError(where, "must be an object")
    kind = raw.get("kind")
    if kind not in KINDS:
        raise WorkflowError(f"{where}.kind", "ask, ladder or office")
    dot = f"{where}."
    _only(raw, FIELDS[kind], dot, f"an {kind} step" if kind != "ladder" else "a ladder step")
    name = _name(raw.get("name"), f"{dot}name")
    prompt = _text(raw.get("prompt"), f"{dot}prompt", PROMPT_LIMIT)
    allow = _texts(raw.get("allow", []), f"{dot}allow", MAX_ALLOWS, 200)
    cost = raw.get("max_cost")
    step: dict[str, Any] = {
        "name": name,
        "kind": kind,
        "prompt": prompt,
        "allow_write": _flag(raw, "allow_write", dot, False),
        "allow": allow,
        "connections": _flag(raw, "connections", dot, True),
        "plugins": _flag(raw, "plugins", dot, True),
        "max_cost_musd": None if cost is None else _money(cost, f"{dot}max_cost"),
    }
    if kind in ("ask", "ladder"):
        step["read_only"] = _flag(raw, "read_only", dot, False)
        if step["read_only"] and (step["allow_write"] or allow):
            raise WorkflowError(where, "a read_only step takes no allow_write and no allow")
    if kind == "ask":
        step.update(_ask_fields(raw, dot))
    elif kind == "ladder":
        step["ladder_checks"] = _texts(raw.get("checks", []), f"{dot}checks", MAX_CHECKS)
        if raw.get("role") not in (None, "private"):
            raise WorkflowError(
                f"{dot}role", "a ladder picks each rung itself; the role it takes is private"
            )
        step["role"] = raw.get("role")
    else:
        step.update(_office_fields(raw, dot))
    return Step(**step)


def _ask_fields(raw: Mapping[str, Any], dot: str) -> dict[str, Any]:
    from metabot.config import MODEL_RE, login_name_ok

    out: dict[str, Any] = {}
    engine, model, role, login = (raw.get(k) for k in ("engine", "model", "role", "login"))
    if engine is not None and not (isinstance(engine, str) and ENGINE_RE.match(engine)):
        raise WorkflowError(f"{dot}engine", f"{engine!r} is not an engine name")
    if model is not None and not (isinstance(model, str) and MODEL_RE.match(model)):
        raise WorkflowError(f"{dot}model", f"{model!r} is not a model name")
    if role is not None and not (isinstance(role, str) and ENGINE_RE.match(role)):
        raise WorkflowError(f"{dot}role", f"{role!r} is not a role name")
    if login is not None and not login_name_ok(login):
        raise WorkflowError(f"{dot}login", f"{login!r} is not a login name")
    out.update(engine=engine, model=model, role=role, login=login)
    return out


def _office_fields(raw: Mapping[str, Any], dot: str) -> dict[str, Any]:
    folder = _absolute(raw.get("dir"), f"{dot}dir")
    pinned = raw.get("office_sha256")
    if not (isinstance(pinned, str) and SHA_RE.match(pinned)):
        raise WorkflowError(
            f"{dot}office_sha256", "the sha256 of the office's office.yaml, 64 hex digits"
        )
    desks = raw.get("desks")
    until = _flag(raw, "until_checks", dot, False)
    rounds = raw.get("rounds")
    if rounds is not None:
        if not until:
            raise WorkflowError(f"{dot}rounds", "goes with until_checks")
        if not (isinstance(rounds, int) and not isinstance(rounds, bool)) or not (
            1 <= rounds <= ROUNDS_MAX
        ):
            raise WorkflowError(f"{dot}rounds", f"a whole number from 1 to {ROUNDS_MAX}")
    return {
        "dir": folder,
        "office_sha256": pinned,
        "desks": None if desks is None else _texts(desks, f"{dot}desks", 32, 64),
        "office_checks": _flag(raw, "checks", dot, False) or until,
        "until_checks": until,
        "rounds": rounds,
        "fan": _flag(raw, "fan", dot, False),
        "advisors": _flag(raw, "advisors", dot, True),
    }


def _schedule(raw: Any) -> Schedule:
    from metabot.cron import Cron, CronError, zone

    if not isinstance(raw, dict):
        raise WorkflowError("schedule", "must be an object with cron and tz")
    _only(raw, SCHEDULE, "schedule.", "a schedule")
    cron, tz = raw.get("cron"), raw.get("tz")
    if not isinstance(cron, str):
        raise WorkflowError("schedule.cron", "five cron fields, such as '0 9 * * 1-5'")
    if not isinstance(tz, str):
        raise WorkflowError("schedule.tz", "a time zone, such as Europe/London")
    try:
        where = zone(tz)
    except CronError as exc:
        raise WorkflowError("schedule.tz", str(exc)) from None
    try:
        Cron.parse(cron).next_after(datetime.now(UTC), where)
    except CronError as exc:
        raise WorkflowError("schedule.cron", str(exc)) from None
    return Schedule(cron, tz)


def _ceiling(raw: Any) -> Ceiling:
    if not isinstance(raw, dict):
        raise WorkflowError("ceiling", "must be an object")
    _only(raw, CEILING, "ceiling.", "a ceiling")
    if not raw:
        raise WorkflowError("ceiling", "at least one of firings, expires and month_cap")
    firings, expires, cap = raw.get("firings"), raw.get("expires"), raw.get("month_cap")
    if firings is not None and not (
        isinstance(firings, int) and not isinstance(firings, bool) and 1 <= firings <= FIRINGS_MAX
    ):
        raise WorkflowError("ceiling.firings", f"a whole number from 1 to {FIRINGS_MAX}")
    if expires is not None:
        try:
            ok = isinstance(expires, str) and DATE_RE.match(expires) is not None
            ok = ok and date.fromisoformat(expires) is not None
        except ValueError:
            ok = False
        if not ok:
            raise WorkflowError("ceiling.expires", "a date, YYYY-MM-DD")
    return Ceiling(
        firings=firings,
        expires=expires,
        month_cap_musd=None if cap is None else _money(cap, "ceiling.month_cap"),
    )


def parse_workflow(raw: bytes, path: Path | None = None) -> Workflow:
    """The workflow these bytes hold, addressed by their sha256; WorkflowError when they
    hold none."""
    if len(raw) > MAX_BYTES:
        raise WorkflowError("file", f"larger than {MAX_BYTES} bytes")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise WorkflowError("file", "not UTF-8 text") from None
    try:
        data = json.loads(text, object_pairs_hook=_pairs, parse_constant=_constant)
    except RecursionError:
        raise WorkflowError("file", "nested too deeply") from None
    except ValueError as exc:
        if isinstance(exc, WorkflowError):
            raise
        raise WorkflowError("file", f"not JSON ({exc})") from None
    if not isinstance(data, dict):
        raise WorkflowError("file", "must be a JSON object")
    if data.get("format") != FORMAT:
        raise WorkflowError("format", f"must be {FORMAT}")
    _only(data, TOP, "", "a workflow")
    name = _name(data.get("name"), "name")
    if HEXISH.match(name):
        raise WorkflowError("name", "reads as a sha256; give it a letter outside a-f")
    about = data.get("about")
    if about is not None:
        about = _text(about, "about", ABOUT_LIMIT)
    cwd = _absolute(data.get("cwd"), "cwd")
    steps = data.get("steps")
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
        raise WorkflowError("steps", f"a list of 1 to {MAX_STEPS} steps")
    read: list[Step] = []
    for n, raw_step in enumerate(steps):
        step = _step(raw_step, n)
        if any(s.name == step.name for s in read):
            raise WorkflowError(f"steps[{n}].name", f"a step named {step.name} is there already")
        read.append(step)
    schedule = None if data.get("schedule") is None else _schedule(data["schedule"])
    ceiling = None if data.get("ceiling") is None else _ceiling(data["ceiling"])
    if schedule is not None and ceiling is None:
        raise WorkflowError(
            "ceiling", "a workflow with a schedule needs one: firings, expires or month_cap"
        )
    return Workflow(
        raw=raw,
        sha256=hashlib.sha256(raw).hexdigest(),
        name=name,
        cwd=cwd,
        steps=tuple(read),
        about=about,
        schedule=schedule,
        ceiling=ceiling,
        path=path,
    )


def workflow_bytes(data: Mapping[str, Any]) -> bytes:
    """The bytes ``workflow new`` writes for ``data``: indented JSON, then a newline."""
    return (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


# ── the store ───────────────────────────────────────────────────────────────

INDEX = "index.json"


@dataclass(frozen=True)
class Stored:
    sha256: str
    name: str
    stored_at: str
    newest: bool  # the version its name finds


def _folder() -> Path:
    return private_dir(metabot_home() / "workflows")


def kept_path(sha256: str) -> Path:
    """Where the store keeps a workflow's bytes; ``sha256`` is checked to be one."""
    if not SHA_RE.match(sha256):
        raise ValueError(f"not a sha256: {sha256!r}")
    return _folder() / f"{sha256}.json"


@contextlib.contextmanager
def _locked() -> Iterator[None]:
    fd = os.open(_folder() / f".{INDEX}.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _index() -> dict[str, dict[str, Any]]:
    path = _folder() / INDEX
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, RecursionError):
        raise WorkflowError("store", f"{path} does not read as the workflows index") from None
    if not isinstance(data, dict):
        raise WorkflowError("store", f"{path} is not the workflows index")
    return {
        sha: row
        for sha, row in data.items()
        if isinstance(sha, str)
        and SHA_RE.match(sha)
        and isinstance(row, dict)
        and isinstance(row.get("name"), str)
    }


def _order(row: Mapping[str, Any]) -> tuple[int, str]:
    seq = row.get("seq")
    return (seq if isinstance(seq, int) else -1, str(row.get("stored_at", "")))


def _write_bytes(path: Path, raw: bytes) -> None:
    """``raw`` to ``path`` whole, mode 0600: a reader sees the old file or the new one."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(raw)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def store_workflow(raw: bytes) -> Workflow:
    """Check ``raw`` and keep it; the name now finds this version. The same bytes again
    keep one copy."""
    wf = parse_workflow(raw)
    with _locked():
        index = _index()
        path = kept_path(wf.sha256)
        _write_bytes(path, raw)
        seq = max((_order(row)[0] for row in index.values()), default=0) + 1
        index[wf.sha256] = {"name": wf.name, "stored_at": utc_now(), "seq": seq}
        write_private(path.parent / INDEX, json.dumps(index, indent=2, sort_keys=True) + "\n")
    return parse_workflow(raw, path)


def stored_workflows() -> list[Stored]:
    """Every stored version, by name, then oldest first."""
    index = _index()
    newest: dict[str, str] = {}
    for sha, row in sorted(index.items(), key=lambda item: _order(item[1])):
        newest[row["name"]] = sha
    rows = sorted(index.items(), key=lambda item: (item[1]["name"], _order(item[1])))
    return [
        Stored(sha, row["name"], str(row.get("stored_at", "")), newest[row["name"]] == sha)
        for sha, row in rows
    ]


def resolve(ref: str) -> str:
    """The sha256 ``ref`` names: a name (its newest version), a sha256, or its first 12 or
    more digits. KeyError when it names none, or more than one."""
    rows = stored_workflows()
    named = [r.sha256 for r in rows if r.name == ref and r.newest]
    if named:
        return named[0]
    if HEXISH.match(ref):
        found = [r.sha256 for r in rows if r.sha256.startswith(ref)]
        if len(found) > 1:
            raise KeyError(f"{ref} starts more than one workflow's sha256; give more of it")
        if found:
            return found[0]
    raise KeyError(f"no workflow named {ref!r}")


def load_workflow(ref: str) -> Workflow:
    """A stored workflow by name or sha256, its bytes hashed again as they are read.

    KeyError when there is none; WorkflowError when its bytes changed since they were
    stored, or no longer read as a workflow.
    """
    sha = resolve(ref)
    path = kept_path(sha)
    try:
        with path.open("rb") as fh:
            raw = fh.read(MAX_BYTES + 1)
    except OSError as exc:
        raise WorkflowError("bytes", f"{path} cannot be read ({exc.strerror or exc})") from None
    now = hashlib.sha256(raw).hexdigest()
    if now != sha:
        raise WorkflowError(
            "bytes",
            f"the workflow kept as {sha[:SHORT]} has changed since it was stored "
            f"(its bytes now hash to {now[:SHORT]}); keep the file again with metabot "
            "workflow add FILE",
        )
    wf = parse_workflow(raw, path)
    named = next((r.name for r in stored_workflows() if r.sha256 == sha), wf.name)
    if named != wf.name:  # the index was changed: a name must never find another's bytes
        raise WorkflowError("store", f"the index names {sha[:SHORT]} {named}, its bytes {wf.name}")
    return wf


# ── what stops it here ──────────────────────────────────────────────────────


def here_problems(wf: Workflow) -> list[str]:
    """Why the workflow cannot run on this machine as it stands; empty when it can.

    Each step is looked at as its run would start: the engine, the role, the login, the
    office and its office.yaml, the ladder and its checks. Nothing runs.
    """
    problems = []
    if not os.path.isdir(wf.cwd):
        problems.append(f"{wf.cwd} is not a folder")
    for step in wf.steps:
        said = _step_problem(wf, step)
        if said:
            problems.append(f"step {step.name}: {said}")
    return problems


def _step_problem(wf: Workflow, step: Step) -> str | None:
    try:
        if step.kind == "ask":
            ask_engine(step)
        elif step.kind == "ladder":
            return _ladder_problem(step)
        else:
            open_office(step)
    except (ValueError, OSError) as exc:  # ConfigError, RosterError, OfficeError, PluginError
        return str(exc)
    return None


def ask_engine(step: Step) -> tuple[Any, str, str | None]:
    """An ask step's engine, made as its run makes it, with the engine's name and model.

    ValueError when it cannot be made here: an unknown engine or role, or a login that is
    not there or not fit.
    """
    from metabot.commands import default_engine
    from metabot.engines import engine_names, get_engine, known_engine
    from metabot.roster import pick

    name, model = pick(step.role, step.engine, step.model)
    name = name or default_engine()
    if not known_engine(name):
        raise ValueError(f"unknown engine {name!r}; here: {', '.join(engine_names())}")
    return get_engine(name, login=step.login), name, model


def _ladder_problem(step: Step) -> str | None:
    from metabot.checks import refusal
    from metabot.config import load_config
    from metabot.ladder import effective_ladder

    refused = refusal({f"ask:{n}": c for n, c in enumerate(step.ladder_checks, 1)})
    if refused:
        return " ".join(refused.replace("change them in office.yaml", "change the step").split())
    cfg = load_config()
    rungs, _whose, _note = effective_ladder(cfg)
    if not rungs:
        return "no rung here: install an engine CLI, or start and probe the local model server"
    if step.role == "private":
        from metabot.roster import stays_here

        if not any(stays_here(rung.entry(), cfg) for rung in rungs):
            return (
                "no rung here keeps the work on this machine, and a private ladder skips "
                "every rung that sends it off: start the local model server and probe it "
                "(metabot doctor --models), or add a rung on it to ladder in config.json"
            )
    return None


def open_office(step: Step) -> Any:
    """An office step's office, read as its run reads it; ValueError when it cannot run:
    no office.yaml there, one that does not read, or one changed since the workflow
    named it."""
    from metabot.config import load_config
    from metabot.office import OfficeError, load_office, office_root

    root = office_root(Path(str(step.dir)))
    office = load_office(root, load_config().fan_cap)
    if office.sha256 != step.office_sha256:
        raise OfficeError(
            "office.yaml",
            f"office.yaml has changed since the workflow named it (it was "
            f"{str(step.office_sha256)[:SHORT]}, it is {office.sha256[:SHORT]}); "
            "make the workflow again",
        )
    return office
