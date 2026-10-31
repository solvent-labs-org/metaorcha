"""The roster: each role an engine and a model.

``fast`` takes short turns; ``build`` takes longer work, and an office's
``any`` desks run on it; ``review`` gives a second look; ``second`` gives
another engine's opinion; ``private`` keeps the work on this machine.

The default roster uses only what is here: an engine CLI on PATH, and the
local model server when it answers on this machine. It names a model only
where the engine has a short name for one. A role the person sets wins over
the default, and the roster shows which is which.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from collections.abc import Mapping
from urllib.parse import urlparse

from metabot.config import MODEL_RE, Config, ConfigError, endpoint_of, load_config, save_config
from metabot.connections import LOCAL_HOSTS
from metabot.engines import BINARIES, available, engine_names, known_engine

ROLES = ("fast", "build", "review", "second", "private")
ABOUT = {
    "fast": "short, quick turns",
    "build": "longer build work; an office's any desks run on it",
    "review": "a second look at finished work",
    "second": "another engine's opinion",
    "private": "work that never leaves this machine",
}
BUILD_ORDER = ("claude", "codex", "cursor")
YOURS, DEFAULT = "yours", "default"
OFF_MACHINE = "this engine sends the work off this machine"

Entry = dict[str, str]


class RosterError(ValueError):
    pass


def local_status(base_url: str) -> str:
    """Checks a loopback endpoint only: a remote one is never contacted here."""
    if urlparse(base_url).hostname not in LOCAL_HOSTS:
        return "configured (remote; not checked)"
    try:
        with urllib.request.urlopen(f"{base_url.rstrip('/')}/models", timeout=2) as resp:
            return "reachable" if resp.status == 200 else f"answered {resp.status}"
    except OSError:
        return "not reachable (is the local model server running?)"


def listed_models(base_url: str) -> list[str]:
    """The models a loopback server lists; none for a remote one, which is never contacted."""
    if urlparse(base_url).hostname not in LOCAL_HOSTS:
        return []
    try:
        with urllib.request.urlopen(f"{base_url.rstrip('/')}/models", timeout=5) as resp:
            data = json.loads(resp.read(1_000_000).decode("utf-8"))
    except (OSError, ValueError):
        return []
    rows = data.get("data") if isinstance(data, dict) else None
    ids = [row.get("id") for row in rows or [] if isinstance(row, dict)]
    return [i for i in ids if isinstance(i, str) and MODEL_RE.match(i)]


def stays_here(entry: Mapping[str, str], cfg: Config) -> bool:
    """Whether a run on this entry keeps the work on this machine.

    The address judged is the one the run calls: the endpoint the entry names, or the
    openai settings when it names none. An endpoint the config does not have is never here.
    """
    if entry.get("engine") != "openai":
        return False
    name = entry.get("endpoint")
    if name is None:
        base_url = cfg.openai_base_url
    else:
        try:
            base_url = endpoint_of(cfg, name).base_url
        except (ConfigError, KeyError, TypeError):
            return False
    try:
        return isinstance(base_url, str) and urlparse(base_url).hostname in LOCAL_HOSTS
    except ValueError:  # an address that does not parse is not known to be here
        return False


def here(cfg: Config) -> tuple[dict[str, str | None], bool]:
    """The engine CLIs on PATH, and whether the local model server answers."""
    return available(), local_status(cfg.openai_base_url) == "reachable"


def default_roster(
    installed: Mapping[str, str | None], local_reachable: bool, local_model: str = ""
) -> dict[str, Entry]:
    """The roster from what is here, and nothing else."""
    has = {name for name, path in installed.items() if path}
    local = {"engine": "openai", "model": local_model} if local_reachable and local_model else None
    out: dict[str, Entry] = {}
    if "claude" in has:
        out["fast"] = {"engine": "claude", "model": "haiku"}
    build = next((name for name in BUILD_ORDER if name in has), None)
    if build is not None:
        out["build"] = {"engine": build}
    elif local is not None:
        out["build"] = dict(local)
    review = next((name for name in ("codex", "claude") if name in has), None)
    if review is not None:
        out["review"] = {"engine": review}
    if "cursor" in has:
        out["second"] = {"engine": "cursor"}
    if local is not None:
        out["private"] = dict(local)
    return out


def _table(cfg: Config) -> dict[str, object]:
    """The person's roles; a hand-edited roster that is not a table counts as none."""
    return dict(cfg.roster) if isinstance(cfg.roster, dict) else {}


def _mine(cfg: Config, role: str) -> Entry | None:
    raw = _table(cfg).get(role)
    if not isinstance(raw, dict) or not known_engine(raw.get("engine")):
        return None
    model = raw.get("model")
    entry = {"engine": str(raw["engine"])}
    if isinstance(model, str) and MODEL_RE.match(model):
        entry["model"] = model
    return entry


def effective_roster(
    cfg: Config, installed: Mapping[str, str | None], local_reachable: bool
) -> dict[str, tuple[Entry, str]]:
    """Each role's entry and whose it is: ``yours`` wins over ``default``."""
    defaults = default_roster(installed, local_reachable, cfg.openai_model)
    out: dict[str, tuple[Entry, str]] = {}
    for role in ROLES:
        mine = _mine(cfg, role)
        if mine is not None:
            out[role] = (mine, YOURS)
        elif role in defaults:
            out[role] = (defaults[role], DEFAULT)
    return out


def roster_entries(cfg: Config | None = None) -> dict[str, Entry]:
    """The roster as it stands here, role to entry."""
    cfg = cfg or load_config()
    installed, reachable = here(cfg)
    return {role: entry for role, (entry, _) in effective_roster(cfg, installed, reachable).items()}


def role_entry(role: str, cfg: Config | None = None) -> Entry:
    """One role's engine and model; RosterError when the role has none here."""
    if role not in ROLES:
        raise RosterError(f"no role {role!r}; the roles are {', '.join(ROLES)}")
    cfg = cfg or load_config()
    mine = _mine(cfg, role)
    if mine is not None:
        return mine
    installed, reachable = here(cfg)
    entry = default_roster(installed, reachable, cfg.openai_model).get(role)
    if entry is None:
        raise RosterError(
            f"no engine for the {role} role here; set one with: "
            f"metabot roster set {role} --engine ENGINE"
        )
    return entry


def fast_model(engine: str, cfg: Config | None = None) -> str | None:
    """The model a plain ``ask`` takes on ``engine``: the fast role's, when that role runs
    on the same engine and no model is set for it; None otherwise.

    The model only: where the work goes stays the default engine's, so a local
    default never sends work off this machine. Nothing is contacted here.
    """
    cfg = cfg or load_config()
    if cfg.models.get(engine):
        return None
    entry = _mine(cfg, "fast") or default_roster(available(), False).get("fast")
    if entry is None or entry["engine"] != engine:
        return None
    return entry.get("model") or None


def pick(
    role: str | None, engine: str | None, model: str | None, cfg: Config | None = None
) -> tuple[str | None, str | None]:
    """The engine and model for a run: ``-e`` and ``-m`` win, and a role fills the rest.

    A role's model goes with its engine only: with another engine named, that
    engine's own model is used.
    """
    if role is None:
        return engine, model
    return fill(role_entry(role, cfg), engine, model)


def fill(entry: Mapping[str, str], engine: str | None, model: str | None) -> tuple[str, str | None]:
    """``-e`` and ``-m`` win over an entry; its model goes with its own engine only."""
    chosen = engine or entry["engine"]
    if model is None and chosen == entry["engine"]:
        model = entry.get("model")
    return chosen, model


# ── the command ─────────────────────────────────────────────────────────────


def _err(text: str) -> None:
    print(f"metabot: {text}", file=sys.stderr)


def cmd_roster(_args: argparse.Namespace) -> int:
    cfg = load_config()
    installed, reachable = here(cfg)
    roster = effective_roster(cfg, installed, reachable)
    for role in ROLES:
        if role not in roster:
            print(f"{role:<8} {'-':<8} {'':<22} {'':<8} not set: {ABOUT[role]}")
            continue
        entry, whose = roster[role]
        engine = entry["engine"]
        note = ABOUT[role]
        if role == "private" and not stays_here(entry, cfg):
            note = OFF_MACHINE
        if engine in BINARIES and not installed.get(engine):
            note = f"{BINARIES[engine]} is not installed"
        model = entry.get("model") or "(its own)"
        print(f"{role:<8} {engine:<8} {model[:22]:<22} {whose:<8} {note}")
    if not roster:
        why = "no engine CLI is installed"
        why += f", and no local model server answered at {cfg.openai_base_url}"
        print(f"\nthe roster is empty: {why}.")
        print("install an engine, start the local server, or: metabot roster set ROLE --engine E")
    return 0


def cmd_roster_set(args: argparse.Namespace) -> int:
    if args.role not in ROLES:
        _err(f"no role {args.role!r}; the roles are {', '.join(ROLES)}")
        return 2
    if not known_engine(args.engine):
        _err(f"unknown engine {args.engine!r}; choose one of: {', '.join(engine_names())}")
        return 2
    if args.model is not None and not MODEL_RE.match(args.model):
        _err(f"{args.model!r} is not a model name")
        return 2
    cfg = load_config()
    entry = {"engine": args.engine, **({"model": args.model} if args.model else {})}
    cfg.roster = {**_table(cfg), args.role: entry}
    save_config(cfg)
    print(f"{args.role}: {args.engine}{'/' + args.model if args.model else ''} (yours)")
    if args.role == "private" and not stays_here(entry, cfg):
        print(f"note: {OFF_MACHINE}; the private role is meant for the local model server")
    return 0


def cmd_roster_unset(args: argparse.Namespace) -> int:
    cfg = load_config()
    mine = _table(cfg)
    if args.role not in mine:
        _err(f"you have not set {args.role!r}; the default stays")
        return 2
    cfg.roster = {k: v for k, v in mine.items() if k != args.role}
    save_config(cfg)
    print(f"{args.role}: back to the default")
    return 0
