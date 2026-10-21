"""The ladder: a task tries the cheapest rung first, and climbs only on a failure the record shows.

Rungs come from ``ladder`` in ``config.json``, an ordered list of
``{engine, model?, tier?}``. With none set, the ladder is the local model,
when its last probe (``metabot doctor --models``) found it can call tools,
and then the ``build`` role. A tier is ``local``, ``sub`` or ``paid``; with
none given, the openai engine on this machine is local, an engine CLI is
sub, and the openai engine anywhere else is paid. Rules decide everything
here: no model is asked which rung to take.

Each rung is an ordinary run with its own receipt. Its run id is
``<ladder>.<n>``; its meta names the ladder, the rung, and as ``parent`` the
run before it. ``<METABOT_HOME>/ladders/<ladder>.json`` lists the rungs.

A rung climbs when it did not end well: its run is not ok (an engine error,
a tool call the model wrote that MetaBot could not read, a limit), a call
did not succeed, or its declared acceptance failed. The ladder stops instead
when climbing would not be the person's choice or would start from a changed
project: a call was declined, a write was let through, or an engine whose own
tools run unasked failed. A private task skips every rung that sends the
work off this machine. ``--max-cost`` covers the whole ladder: each rung
gets what is left, and a rung MetaBot cannot price under a cap is skipped.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from metabot.config import MODEL_RE, Config, load_config
from metabot.connections import LOCAL_HOSTS
from metabot.engines import ENGINES
from metabot.models import cached_mode
from metabot.paths import metabot_home, private_dir, safe_id, write_private
from metabot.receipts import acceptance_of, verdicts_of
from metabot.roster import RosterError, effective_roster, here, stays_here
from metabot.spend import dollars

TIERS = ("local", "sub", "paid")
USABLE = ("native", "repaired", "json")  # a probe that found the model can call tools
ATTEMPTS = 3
ATTEMPTS_MAX = 5
YOURS, DEFAULT = "yours", "default"

ANSWERED, CLIMBED, STOPPED, SKIPPED = "answered", "climbed", "stopped", "skipped"


class LadderError(ValueError):
    """The ladder in ``config.json`` does not read, or there is no rung to run."""


@dataclass(frozen=True)
class Rung:
    engine: str
    model: str | None
    tier: str

    def label(self) -> str:
        return f"{self.engine}/{self.model}" if self.model else self.engine

    def entry(self) -> dict[str, str]:
        return {"engine": self.engine, **({"model": self.model} if self.model else {})}


def is_local_url(base_url: str) -> bool:
    return urlparse(base_url).hostname in LOCAL_HOSTS


def tier_of(engine: str, cfg: Config) -> str:
    if engine == "openai":
        return "local" if is_local_url(cfg.openai_base_url) else "paid"
    return "sub"


def read_ladder(raw: Any, cfg: Config) -> list[Rung]:
    """The ladder the person set; LadderError naming the rung that does not read."""
    if not isinstance(raw, list):
        raise LadderError("ladder in config.json is not a list of rungs")
    rungs: list[Rung] = []
    for n, item in enumerate(raw, 1):
        where = f"ladder rung {n} in config.json"
        if not isinstance(item, dict):
            raise LadderError(f"{where} is not a table")
        unknown = set(item) - {"engine", "model", "tier"}
        if unknown:
            raise LadderError(f"{where} has a key MetaBot does not know: {sorted(unknown)[0]}")
        engine = item.get("engine")
        if engine not in ENGINES:
            raise LadderError(f"{where} names no engine MetaBot has ({', '.join(ENGINES)})")
        model = item.get("model")
        if model is not None and not (isinstance(model, str) and MODEL_RE.match(model)):
            raise LadderError(f"{where} has a model that is not a model name")
        if engine == "openai" and model is None:
            model = cfg.openai_model or None  # the model it runs, shown and probed as such
        tier = item.get("tier", tier_of(engine, cfg))
        if tier not in TIERS:
            raise LadderError(f"{where} has a tier that is not one of {', '.join(TIERS)}")
        rungs.append(Rung(engine, model, tier))
    if not rungs:
        raise LadderError("ladder in config.json has no rungs")
    return rungs


def attempts(cfg: Config) -> int:
    value = cfg.ladder_attempts
    if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= ATTEMPTS_MAX:
        return value
    raise LadderError(
        f"ladder_attempts in config.json is not a whole number from 1 to {ATTEMPTS_MAX}"
    )


def default_ladder(
    cfg: Config, installed: Mapping[str, str | None], local_reachable: bool
) -> tuple[list[Rung], str | None]:
    """The ladder from what is here: the local model when a probe found it can call tools,
    then the build role. With it, a note when the local model is left out for want of a probe."""
    rungs: list[Rung] = []
    note = None
    model = cfg.openai_model
    local = local_reachable and is_local_url(cfg.openai_base_url) and bool(model)
    if local:
        mode = cached_mode(cfg.openai_base_url, model)
        if mode in USABLE:
            rungs.append(Rung("openai", model, "local"))
        elif mode is None:
            note = f"the local model {model} joins the ladder once probed: "
            note += f"metabot doctor --models {model}"
        else:
            note = f"the local model {model} is not on the ladder: its probe found no tool calls"
    build = effective_roster(cfg, installed, local_reachable).get("build")
    if build is not None:
        entry = build[0]
        rung = Rung(entry["engine"], entry.get("model"), tier_of(entry["engine"], cfg))
        if rung.engine == "openai" and rung.model is None:
            rung = Rung("openai", model, rung.tier)
        if rung not in rungs:
            rungs.append(rung)
    return rungs, note


def effective_ladder(cfg: Config | None = None) -> tuple[list[Rung], str, str | None]:
    """The ladder as it stands here, whose it is, and a note about what it leaves out."""
    cfg = cfg or load_config()
    if cfg.ladder:
        return read_ladder(cfg.ladder, cfg), YOURS, None
    installed, reachable = here(cfg)
    rungs, note = default_ladder(cfg, installed, reachable)
    return rungs, DEFAULT, note


# ── running it ──────────────────────────────────────────────────────────────


@dataclass
class RungRow:
    n: int
    engine: str
    model: str | None
    tier: str
    outcome: str  # answered | climbed | stopped | skipped
    why: str = ""
    run_id: str | None = None
    ok: bool | None = None
    cost_musd: int | None = None
    cost_source: str | None = None


@dataclass
class LadderResult:
    ladder_id: str
    rows: list[RungRow] = field(default_factory=list)
    final: Any = None  # the last rung's RunResult; None when no rung ran
    answered: bool = False
    stopped: str | None = None  # why the ladder ended without an answer
    spent_musd: int = 0
    unpriced: int = 0  # rungs that ran with no price

    def to_json(self) -> dict[str, Any]:
        return {
            "ladder_id": self.ladder_id,
            "answered": self.answered,
            "stopped": self.stopped,
            "spent_musd": self.spent_musd,
            "unpriced": self.unpriced,
            "rungs": [asdict(row) for row in self.rows],
        }


def ladder_path(ladder_id: str) -> Path:
    return metabot_home() / "ladders" / f"{safe_id(ladder_id, 'ladder id')}.json"


def _claim(ladder_id: str) -> None:
    """Take the ladder's record only if its id is free, so two ladders never share one."""
    private_dir(metabot_home() / "ladders")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    try:
        fd = os.open(ladder_path(ladder_id), flags, 0o600)
    except FileExistsError:
        raise LadderError(f"ladder {ladder_id} exists already") from None
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"ladder_id": ladder_id, "rungs": []}, indent=2) + "\n")


def _save(result: LadderResult) -> None:
    write_private(ladder_path(result.ladder_id), json.dumps(result.to_json(), indent=2) + "\n")


def load_ladder(ladder_id: str) -> dict[str, Any] | None:
    try:
        data = json.loads(ladder_path(ladder_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def let_through(receipt: Path | str) -> int:
    """How many writes the receipt says went past the gate: allowed by policy, or by a person."""
    return sum(
        1
        for v in verdicts_of(receipt)
        if (str(v.get("check")).startswith("scope_allow:"))
        or (str(v.get("check")).startswith("scope_approval:") and v.get("result") == "pass")
    )


def judge(result: Any, gated: bool) -> tuple[str, str]:
    """What a rung's run means for the ladder: answered, climb, or stop, and why.

    ``gated``: every tool the engine runs goes through MetaBot's gate first, so
    a failed run on it changed nothing the receipt does not show.
    """
    if result.limit:
        return STOPPED, f"it passed a limit: {result.limit}"
    if result.ok and not result.failed and acceptance_of(result.receipt) != "fail":
        return ANSWERED, ""  # a declined call leaves the answer standing, with its caveat
    if result.declined:
        return STOPPED, f"{result.declined} call(s) declined; a costlier model would ask the same"
    wrote = let_through(result.receipt)
    if wrote:
        return STOPPED, f"it failed after {wrote} write(s) went through; see {result.run_id} first"
    if not gated:
        return STOPPED, "it failed on an engine whose own tools may have changed the project"
    if not result.ok:
        return CLIMBED, result.error or "the run did not end well"
    if result.failed:
        return CLIMBED, f"{result.failed} call(s) did not succeed"
    return CLIMBED, "its declared acceptance failed"


RunRung = Callable[[Rung, str, dict[str, Any], int | None], Awaitable[Any]]


async def run_ladder(
    rungs: list[Rung],
    run_rung: RunRung,
    *,
    cfg: Config,
    ladder_id: str,
    max_attempts: int = ATTEMPTS,
    max_cost_musd: int | None = None,
    private: bool = False,
    gated: Callable[[str], bool],
    on_line: Callable[[str], None] = lambda _line: None,
    no_price: type[Exception] = ValueError,
) -> LadderResult:
    """Run the rungs in order until one answers, the ladder stops, or the attempts run out.

    ``run_rung(rung, run_id, meta, cap_musd)`` runs one rung as ``ask`` would
    and returns its RunResult; it raises ``no_price`` for a rung it cannot
    price under a cap. A cancelled rung's error goes up after the record is saved.
    """
    _claim(ladder_id)
    result = LadderResult(ladder_id)
    parent: str | None = None
    ran = 0
    try:
        for n, rung in enumerate(rungs, 1):
            row = RungRow(n, rung.engine, rung.model, rung.tier, SKIPPED)
            if ran >= max_attempts:
                result.stopped = f"{max_attempts} rung(s) tried, the most this ladder takes"
                break
            if private and not stays_here(rung.entry(), cfg):
                row.why = "the task reads as private, and this rung sends the work off this machine"
                result.rows.append(row)
                on_line(f"rung {n} {rung.label()} skipped: {row.why}")
                continue
            cap = None if max_cost_musd is None else max_cost_musd - result.spent_musd
            if cap is not None and cap <= 0:
                result.stopped = f"the ladder's {dollars(max_cost_musd or 0)} is spent"
                break
            run_id = f"{ladder_id}.{n}"
            meta: dict[str, Any] = {"ladder": ladder_id, "rung": n}
            if parent is not None:
                meta["parent"] = parent
            try:
                ran_result = await run_rung(rung, run_id, meta, cap)
            except no_price as exc:
                row.why = str(exc)
                result.rows.append(row)
                on_line(f"rung {n} {rung.label()} skipped: {row.why}")
                continue
            ran += 1
            parent = run_id
            result.final = ran_result
            row.run_id, row.ok = ran_result.run_id, ran_result.ok
            row.cost_musd, row.cost_source = ran_result.cost_musd, ran_result.cost_source
            if ran_result.cost_musd is None:
                result.unpriced += 1
            else:
                result.spent_musd += ran_result.cost_musd
            row.outcome, row.why = judge(ran_result, gated(rung.engine))
            result.rows.append(row)
            _save(result)
            if row.outcome == ANSWERED:
                result.answered = True
                on_line(f"rung {n} {rung.label()} answered ({ran_result.run_id})")
                break
            if row.outcome == STOPPED:
                result.stopped = row.why
                on_line(f"rung {n} {rung.label()} stopped the ladder: {row.why}")
                break
            on_line(f"rung {n} {rung.label()} failed: {row.why}")
        else:
            if not result.answered and result.stopped is None:
                result.stopped = "no rung left to try" if ran else "no rung could run"
    finally:
        _save(result)
    return result


def spent_line(result: LadderResult) -> str:
    text = f"ladder {result.ladder_id} cost {dollars(result.spent_musd)}"
    if result.unpriced:
        text += f", with {result.unpriced} rung(s) MetaBot has no price for"
    return text


# ── the command ─────────────────────────────────────────────────────────────


def cmd_ladder(_args: argparse.Namespace) -> int:
    cfg = load_config()
    try:
        rungs, whose, note = effective_ladder(cfg)
        most = attempts(cfg)
    except (LadderError, RosterError) as exc:
        print(f"metabot: {exc}", file=sys.stderr)
        return 2
    for n, rung in enumerate(rungs, 1):
        probe = ""
        if rung.engine == "openai" and is_local_url(cfg.openai_base_url):
            found = cached_mode(cfg.openai_base_url, rung.model or "")
            probe = f"  probe {found or 'none yet'}"
        print(f"rung {n}  {rung.tier:<5}  {rung.label()}{probe}  ({whose})")
    if not rungs:
        print("no rung here: install an engine CLI, or start and probe the local model server")
    if note:
        print(f"note: {note}")
    print(f"at most {most} rung(s) run for one task")
    return 0
