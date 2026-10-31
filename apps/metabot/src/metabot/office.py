"""Code Desks' office file, read for headless runs.

MetaBot reads the same ``office.yaml`` Code Desks does, with its own parser:
the desks, and each gate's ``checks``. Threads, milestones, plans and every
key MetaBot does not know belong to Code Desks and are ignored here, so one
file serves both tools.

Four keys are MetaBot's own, and Code Desks ignores them:

- ``engine:`` the engine a ``custom`` or ``any`` desk runs on. A custom desk
  whose ``command`` is itself ``metabot chat -e E -m M`` needs none.
- ``fan:`` how many copies of the desk run at once, at most this machine's
  ``fan_cap``.
- ``turns:`` the most tool calls one run of the desk may make.
- ``read_only: true`` the desk takes its turn read-only, as an advisor does:
  every write is asked, it files nothing, and its answer is handed on.

A desk Code Desks accepts but MetaBot cannot run here still loads, with a
problem. The rest of the office runs, and only that desk is refused.

A role file is read into the engine's prompt, and the prompt goes to the
engine's provider. So a role must be a file inside the project: an office
file from a cloned repository cannot point a role at a key in the person's
home.
"""

from __future__ import annotations

import hashlib
import os
import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import yaml

from metabot.config import MODEL_RE
from metabot.engines import bridge_keeps_read_only, engine_names, known_engine, missing_command
from metabot.gitcmd import main_worktree, toplevel
from metabot.paths import is_file

OFFICE_FILE = "office.yaml"
OFFICE_ENV = "DESK_OFFICE"  # set by Code Desks on every desk process
HARNESSES = ("claude", "codex", "cursor", "custom", "any")
HARNESS_ENGINES = {"claude": "claude", "codex": "codex", "cursor": "cursor"}
YOU = "you"  # Code Desks' name for the person running the office
FAN_CAP_DEFAULT = 3
FAN_CAP_MAX = 5
TURN_CAP_DEFAULT = 50
TURN_CAP_MAX = 1000
MAX_FILE_BYTES = 1024 * 1024
DESK_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
ROSTER_ROLE = "build"  # the role an `any` desk runs on
ADVISOR_ROLE = "review"  # an `any` advisor runs on this role, else on the build role


class OfficeError(ValueError):
    def __init__(self, where: str, reason: str) -> None:
        super().__init__(f"{where}: {reason}")
        self.where = where
        self.reason = reason


@dataclass(frozen=True)
class GateSpec:
    name: str
    checks: dict[str, str]  # item -> command; exit 0 accepts


@dataclass(frozen=True)
class DeskSpec:
    name: str
    role_file: Path | None  # resolved, inside the project; None when absent or refused
    harness: str
    engine: str | None  # None for an `any` desk: the roster decides
    model: str | None
    budget: int | None  # Code Desks' tokens per cycle; one run never spends more
    advises: tuple[str, ...]  # empty: not an advisor
    kit: tuple[str, ...]  # Code Desks' skills for the desk; loaded into its runs (metabot.kits)
    fan: int
    turns: int
    cwd: str
    problem: str | None = None  # why this desk cannot run here, when it cannot
    read_only: bool = False  # it takes its turn read-only and files nothing

    @property
    def is_advisor(self) -> bool:
        return bool(self.advises)


@dataclass(frozen=True)
class OfficeSpec:
    name: str
    root: Path
    top: Path  # the project: the git top level, else the office folder
    gates: dict[str, GateSpec]
    desks: dict[str, DeskSpec]
    sha256: str = ""  # of the office.yaml bytes it was read from; a seal signs it

    def checks(self) -> dict[str, str]:
        """Every gate's checks, keyed ``<gate>:<item>``, in gate order."""
        return {
            f"{gate.name}:{item}": command
            for gate in self.gates.values()
            for item, command in gate.checks.items()
        }


def find_office(start: Path | None = None) -> Path | None:
    """The office folder for ``start``: Code Desks' pin, else the nearest office.yaml above.

    A folder it cannot look in ends the search there: reading the office there says why.
    """
    pinned = os.environ.get(OFFICE_ENV)
    if pinned:
        try:
            return Path(pinned).resolve()
        except (OSError, RuntimeError):  # a loop of links
            return Path(pinned)
    here = (start or Path.cwd()).resolve()
    for folder in (here, *here.parents):
        try:
            found = is_file(folder / OFFICE_FILE)
        except OSError:  # as --dir reads it: the main tree's office, when that tree has one
            return office_root(folder)
        if found:
            return office_root(folder)
    return None


def office_root(folder: Path) -> Path:
    """``folder`` as the main working tree sees it, when that tree has the office too."""
    folder = folder.resolve()
    mapped = main_worktree(folder)
    try:
        return mapped if is_file(mapped / OFFICE_FILE) else folder
    except OSError:  # an office it cannot look at, never another: reading it says why
        return mapped


def load_office(root: Path, fan_cap: int = FAN_CAP_DEFAULT) -> OfficeSpec:
    path = root / OFFICE_FILE
    try:
        with path.open("rb") as fh:
            raw = fh.read(MAX_FILE_BYTES + 1)
    except OSError as exc:
        raise OfficeError(OFFICE_FILE, f"cannot read {path}: {exc.strerror or exc}") from None
    if len(raw) > MAX_FILE_BYTES:
        raise OfficeError(OFFICE_FILE, f"larger than {MAX_FILE_BYTES} bytes")
    try:
        data = yaml.safe_load(raw.decode("utf-8")) or {}
    except UnicodeDecodeError:
        raise OfficeError(OFFICE_FILE, "not UTF-8 text") from None
    except RecursionError:
        raise OfficeError(OFFICE_FILE, "nested too deeply") from None
    except (yaml.YAMLError, ValueError) as exc:  # a date like 2026-13-45 is a ValueError
        raise OfficeError(OFFICE_FILE, " ".join(str(exc).split())) from None
    try:
        root = root.resolve()
    except (OSError, RuntimeError) as exc:  # a relative name, from a folder that was removed
        why = exc.strerror if isinstance(exc, OSError) and exc.strerror else exc
        raise OfficeError(
            OFFICE_FILE, f"cannot read {path}: the current folder cannot be read ({why})"
        ) from None
    spec = parse_office(data, root, toplevel(root) or root, fan_cap)
    return replace(spec, sha256=hashlib.sha256(raw).hexdigest())


def parse_office(data: Any, root: Path, top: Path, fan_cap: int = FAN_CAP_DEFAULT) -> OfficeSpec:
    if not isinstance(data, dict):
        raise OfficeError(OFFICE_FILE, "expected a mapping")
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise OfficeError("name", "required")
    gates = _gates(data.get("gates"))
    raw_desks = data.get("desks")
    if not isinstance(raw_desks, dict) or not raw_desks:
        raise OfficeError("desks", "at least one desk is required")
    cap = fan_cap_of(fan_cap)
    desks: dict[str, DeskSpec] = {}
    for key, raw in raw_desks.items():
        dname = str(key)
        if dname in desks:
            raise OfficeError(f"desks.{dname}", "named twice")
        desks[dname] = _desk(dname, raw, root, top, cap)
    for desk in desks.values():
        for who in desk.advises:
            if who == desk.name:
                raise OfficeError(f"desks.{desk.name}.advises", "a desk does not advise itself")
            if who != YOU and who not in desks:
                raise OfficeError(f"desks.{desk.name}.advises", f"no desk named {who!r} (or {YOU})")
    return OfficeSpec(name=name.strip(), root=root, top=top, gates=gates, desks=desks)


def desk_engine(
    desk: DeskSpec, roster: Mapping[str, Mapping[str, str]] | None = None
) -> tuple[str, str | None]:
    """The engine and model a desk runs on."""
    if desk.engine:
        return desk.engine, desk.model
    if desk.harness == "any":
        for name in (ADVISOR_ROLE, ROSTER_ROLE) if desk.is_advisor else (ROSTER_ROLE,):
            role = (roster or {}).get(name) or {}
            engine = role.get("engine")
            if known_engine(engine):
                return str(engine), role.get("model")
        raise OfficeError(
            f"desks.{desk.name}",
            f"an any desk runs on the roster's {ROSTER_ROLE} role; "
            f"set it with metabot roster set {ROSTER_ROLE}, or add engine:",
        )
    raise OfficeError(f"desks.{desk.name}", desk.problem or "no engine")


def desk_problem(
    desk: DeskSpec, roster: Mapping[str, Mapping[str, str]] | None = None
) -> str | None:
    """Why a desk cannot run here, or None when it can."""
    if desk.problem:
        return desk.problem
    try:
        engine, _model = desk_engine(desk, roster)
    except OfficeError as exc:
        return exc.reason
    if (desk.read_only or desk.is_advisor) and not bridge_keeps_read_only(engine):
        return (
            f"bridge {engine} does not keep to read-only (read_only: honored), "
            "and this desk runs read-only"
        )
    missing = missing_command(engine)  # else the run would start, and fail at this desk
    return f"{missing} is not on PATH" if missing else None


def desk_runs_on(desk: DeskSpec, roster: Mapping[str, Mapping[str, str]] | None = None) -> str:
    """What a desk runs on, as engine or engine/model, also when it cannot run here;
    ``-`` when no engine can be named."""
    try:
        engine, model = desk_engine(desk, roster)
    except OfficeError:
        return "-"
    return f"{engine}/{model}" if model else engine


def cwd_link_problem(office: OfficeSpec, desk: DeskSpec) -> str | None:
    """Why a desk's cwd leads out of the project through a link here, or None when it stays in."""
    try:
        resolved = (office.root / desk.cwd).resolve()
    except (OSError, RuntimeError):
        return "cwd must stay inside the desk's worktree"
    if not resolved.is_relative_to(office.top):
        return "cwd must stay inside the desk's worktree"
    return None


def fan_cap_of(fan_cap: Any) -> int:
    """The machine's fan cap from config: 1 to 5, and the default when it is not a number."""
    if isinstance(fan_cap, bool) or not isinstance(fan_cap, int):
        return FAN_CAP_DEFAULT
    return max(1, min(fan_cap, FAN_CAP_MAX))


def _int(value: Any, where: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise OfficeError(where, "expected a whole number")
    if isinstance(value, int):
        return value
    try:
        return int(str(value).replace("_", ""))
    except ValueError:
        raise OfficeError(where, "expected a whole number") from None


def _bounded(value: Any, where: str, default: int, top: int) -> int:
    number = _int(value, where)
    if number is None:
        return default
    if not 1 <= number <= top:
        raise OfficeError(where, f"between 1 and {top}")
    return number


def _strlist(value: Any, where: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise OfficeError(where, "expected a list of names")
    return list(value)


def _gates(raw: Any) -> dict[str, GateSpec]:
    if not isinstance(raw, dict) or not raw:
        raise OfficeError("gates", "at least one gate is required")
    gates: dict[str, GateSpec] = {}
    for key, gate in raw.items():
        gname = str(key)
        where = f"gates.{gname}"
        if not isinstance(gate, dict):
            raise OfficeError(where, "expected a mapping")
        owner = _strlist(gate.get("owner"), f"{where}.owner")
        engineer = _strlist(gate.get("engineer"), f"{where}.engineer")
        if not owner and not engineer:
            raise OfficeError(where, "both halves are empty")
        checks = gate.get("checks") or {}
        if not isinstance(checks, dict):
            raise OfficeError(f"{where}.checks", "expected a mapping of item to command")
        found: dict[str, str] = {}
        for item, command in checks.items():
            if item not in owner and item not in engineer:
                raise OfficeError(f"{where}.checks.{item}", "not an item of this gate")
            if not isinstance(command, str) or not command.strip():
                raise OfficeError(f"{where}.checks.{item}", "expected a command")
            found[str(item)] = command.strip()
        gates[gname] = GateSpec(gname, found)
    return gates


def _desk(name: str, raw: Any, root: Path, top: Path, cap: int) -> DeskSpec:
    where = f"desks.{name}"
    if not isinstance(raw, dict):
        raise OfficeError(where, "expected a mapping")
    harness = raw.get("harness", "claude")
    if harness not in HARNESSES:
        raise OfficeError(f"{where}.harness", f"one of {', '.join(HARNESSES)}")
    role = raw.get("role")
    if role is not None and (not isinstance(role, str) or not role):
        raise OfficeError(f"{where}.role", "expected a path to a markdown job description")
    if role is None and harness != "any":
        raise OfficeError(f"{where}.role", "required: a path to a markdown job description")
    command = raw.get("command")
    if harness == "custom" and not command:
        raise OfficeError(f"{where}.command", "required for a custom desk")
    model = None if raw.get("model") is None else str(raw.get("model"))
    engine, model, engine_problem = _engine(where, harness, raw.get("engine"), command, model)
    advises_raw = raw.get("advises")
    if advises_raw is True:
        advises: tuple[str, ...] = (YOU,)
    elif advises_raw is None:
        advises = ()
    else:
        advises = tuple(_strlist(advises_raw, f"{where}.advises") or [YOU])
    kit = tuple(_strlist(raw.get("kit"), f"{where}.kit"))
    budget = _int(raw.get("budget"), f"{where}.budget")
    fan = _bounded(raw.get("fan"), f"{where}.fan", 1, FAN_CAP_MAX)
    turns = _bounded(raw.get("turns"), f"{where}.turns", TURN_CAP_DEFAULT, TURN_CAP_MAX)
    cwd = str(raw.get("cwd", "."))
    read_only = raw.get("read_only", False)
    if not isinstance(read_only, bool):
        raise OfficeError(f"{where}.read_only", "true or false")
    role_file, role_problem = _role(role, root, top)
    problems = [
        None if DESK_NAME.match(name) else _NAME_PROBLEM,
        engine_problem,
        None
        if model is None or MODEL_RE.match(model)
        else f"model {model[:60]!r} is not a model name",  # it would reach the engine's argv
        role_problem,
        _cwd_problem(cwd),
        None if budget is None or budget > 0 else "budget must be above 0",
        None
        if fan <= cap
        else f"fan {fan} is over this machine's cap of {cap} "
        f"(config fan_cap, at most {FAN_CAP_MAX})",
        "an advisor runs once for each desk it advises, so it has no fan"
        if advises and fan > 1
        else None,
        "a read-only desk files nothing, so it has no fan" if read_only and fan > 1 else None,
    ]
    return DeskSpec(
        name=name,
        role_file=role_file,
        harness=harness,
        engine=engine,
        model=model,
        budget=budget,
        advises=advises,
        kit=kit,
        fan=fan,
        turns=turns,
        cwd=cwd,
        problem="; ".join(p for p in problems if p) or None,
        read_only=read_only,
    )


_NAME_PROBLEM = (
    "the name must be letters, digits, - or _, at most 32, starting with a letter or digit: "
    "it names the desk's branch and folder"
)


def _engine(
    where: str, harness: str, engine: Any, command: Any, model: str | None
) -> tuple[str | None, str | None, str | None]:
    """(engine, model, problem) for a desk."""
    if engine is not None:
        if not known_engine(engine):
            raise OfficeError(f"{where}.engine", f"one of {', '.join(engine_names())}")
        fixed = HARNESS_ENGINES.get(harness)
        if fixed and engine != fixed:
            raise OfficeError(f"{where}.engine", f"{engine} conflicts with harness: {harness}")
        return str(engine), model, None
    if harness in HARNESS_ENGINES:
        return HARNESS_ENGINES[harness], model, None
    if harness == "any":
        return None, model, None
    said = _metabot_command(str(command))
    if said is None:
        return None, model, "a custom desk runs its own command; add engine: to run it here"
    named, named_model = said
    if named is None:
        return None, model, "its command runs MetaBot's default engine; name one with -e or engine:"
    if not known_engine(named):
        return None, model, f"its command names engine {named!r}, which MetaBot does not have"
    return named, named_model or model, None


def _metabot_command(command: str) -> tuple[str | None, str | None] | None:
    """The engine and model in a ``metabot chat`` or ``metabot ask`` command, else None."""
    try:
        words = shlex.split(command)
    except ValueError:
        return None
    if len(words) < 2 or Path(words[0]).name != "metabot" or words[1] not in ("chat", "ask"):
        return None
    found: dict[str, str] = {}
    rest = iter(words[2:])
    for word in rest:
        for long, short in (("--engine", "-e"), ("--model", "-m")):
            if word in (long, short):
                value = next(rest, None)
            elif word.startswith(long + "="):
                value = word.split("=", 1)[1]
            elif word.startswith(short) and len(word) > len(short):
                value = word[len(short) :]
            else:
                continue
            if value:
                found[long] = value
    return found.get("--engine"), found.get("--model")


def _role(role: str | None, root: Path, top: Path) -> tuple[Path | None, str | None]:
    if role is None:
        return None, None
    if Path(role).is_absolute():
        return None, "role must be a file inside the office's project"
    path = root / role
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        return None, f"job description missing: {role}"
    if not resolved.is_relative_to(top):
        return None, "role must be a file inside the office's project"
    if not resolved.is_file():
        return None, f"job description is not a file: {role}"
    return resolved, None


def _cwd_problem(cwd: str) -> str | None:
    if os.path.isabs(cwd) or os.path.normpath(cwd).split(os.sep)[0] == "..":
        return "cwd must stay inside the desk's worktree"
    return None
