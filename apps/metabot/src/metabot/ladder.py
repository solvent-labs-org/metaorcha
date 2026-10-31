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
tools run unasked failed. In a copy, a call declined because no one was
there to ask does not stop it: no person said no, and the next rung starts
from the same snapshot and meets the same gate. A private task skips every rung that sends the
work off this machine. ``--max-cost`` covers the whole ladder: each rung
gets what is left, and a rung MetaBot cannot price under a cap is skipped.

The person's ``--check`` commands run on each rung that reached its end
within its limits, sealed as ``<rung run>.checks`` as an office desk's checks
are. They then judge the work: a failing check climbs, and a passing one
answers even when a call on the way did not succeed, such as a test the model
ran before its fix. The record cannot show wrong work, a check can.

In a git checkout, each rung works in its own copy of the project
(``metabot.ladder_copies``), so a failed rung's writes in its copy never
reach the project, and a write no longer stops the ladder; the answering
rung's work is brought back. A copy is not a sandbox: a write the gate allows
to an absolute path reaches that path.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from emerge.journal import utc_now

from metabot.approvals import printable
from metabot.checks import CHECKS_SUFFIX, ChecksCancelled, run_gate
from metabot.config import MODEL_RE, Config, ConfigError, Endpoint, endpoint_of, load_config
from metabot.connections import LOCAL_HOSTS, record_secrets
from metabot.engines import engine_names, known_engine
from metabot.ladder_copies import Copies, CopyError
from metabot.models import cached_mode
from metabot.paths import (
    kept_home,
    metabot_home,
    private_dir,
    receipt_path,
    run_path,
    safe_id,
    write_private,
)
from metabot.receipts import (
    acceptance_of,
    budget_verdict,
    listed,
    named_here,
    project_copy,
    scrub,
    signed_cost,
    verdicts_of,
)
from metabot.roster import RosterError, effective_roster, here, stays_here
from metabot.runner import RunCancelled
from metabot.seals import Member, seal_runs
from metabot.spend import dollars, uncounted

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
    endpoint: str | None = None

    def label(self) -> str:
        head = f"{self.engine}@{self.endpoint}" if self.endpoint else self.engine
        return f"{head}/{self.model}" if self.model else head

    def entry(self) -> dict[str, str]:
        return {
            "engine": self.engine,
            **({"model": self.model} if self.model else {}),
            **({"endpoint": self.endpoint} if self.endpoint else {}),
        }


def tier_of_endpoint(ep: Endpoint) -> str:
    return "local" if is_local_url(ep.base_url) else "paid"


def rung_base_url(rung: Rung, cfg: Config) -> str:
    return endpoint_of(cfg, rung.endpoint).base_url if rung.endpoint else cfg.openai_base_url


def rung_engine(rung: Rung, cfg: Config):
    """The engine an openai rung runs on; None for another engine, which comes by name.

    A rung that names an endpoint takes its address, model and key variable; one that names
    none takes the openai settings. Both come from ``cfg``, the settings the ladder read,
    never from a second read of config.json: a private ladder judged this address, and a
    change made while it runs must not send the work elsewhere. An endpoint with no key gets
    an empty variable name, never None: None would fall back to the default key, and that
    key must not reach a second host.
    """
    if rung.engine != "openai":
        return None
    from metabot.engines.openai_compat import (
        OpenAIEngine,
    )  # the HTTP client loads only when a rung needs it

    if rung.endpoint is None:
        return OpenAIEngine(model=rung.model, config=cfg)
    ep = endpoint_of(cfg, rung.endpoint)
    return OpenAIEngine(
        base_url=ep.base_url,
        model=rung.model,
        key_env=ep.key_env or "",
        config=cfg,
        endpoint=rung.endpoint,
    )


def openai_default(cfg: Config) -> str | None:
    """The model an openai run that names none runs on, as ask picks it."""
    return cfg.models.get("openai") or cfg.openai_model or None


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
        unknown = set(item) - {"engine", "model", "tier", "endpoint"}
        if unknown:
            raise LadderError(f"{where} has a key MetaBot does not know: {sorted(unknown)[0]}")
        engine = item.get("engine")
        if not known_engine(engine):
            raise LadderError(f"{where} names no engine MetaBot has ({', '.join(engine_names())})")
        model = item.get("model")
        if model is not None and not (isinstance(model, str) and MODEL_RE.match(model)):
            raise LadderError(f"{where} has a model that is not a model name")
        endpoint = item.get("endpoint")
        if endpoint is not None:
            if engine != "openai":
                raise LadderError(
                    f"{where} names an endpoint, and only the openai engine takes one"
                )
            try:
                ep = endpoint_of(cfg, endpoint) if isinstance(endpoint, str) else None
            except ConfigError:
                ep = None
            if ep is None:
                raise LadderError(
                    f"{where} names an endpoint the config does not have: {endpoint!r}"
                )
            model = model or ep.model
            if model is None:
                raise LadderError(f"{where} has no model: endpoint {endpoint} names none")
            tier = item.get("tier", ep.tier or tier_of_endpoint(ep))
            if tier not in TIERS:
                raise LadderError(f"{where} has a tier that is not one of {', '.join(TIERS)}")
            rungs.append(Rung(engine, model, tier, endpoint))
            continue
        if engine == "openai" and model is None:
            model = openai_default(cfg)  # the model it runs, shown and probed as such
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
            rung = Rung("openai", openai_default(cfg), rung.tier)
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
    tokens: int = 0
    lost: bool = False  # a request it sent never said what it spent
    late: bool = False  # its engine reported tokens before naming a model pricing them otherwise
    check: str | None = None  # pass, or fail naming each check; None when none ran
    checks_run: str | None = None  # the run the checks were sealed as
    workspace: str | None = None  # the copy the rung worked in
    commit: str | None = None  # what it changed there, committed
    relinked: bool = False  # its work changed the copy's .git file, and MetaBot put it back
    label: str = ""  # the rung as the trace names it: engine, endpoint and model


def why_line(result: LadderResult, limit: int | None = None) -> str:
    """What a ladder did and why, in one line: each climb or skip with its reason, where
    it was done or why it stopped, and what it spent. Past ``limit`` characters the
    reasons are cut; where it ended and what it spent stay whole."""

    def named(row: RungRow) -> str:
        name = row.label or (f"{row.engine}/{row.model}" if row.model else row.engine)
        return printable(name, 80)

    def said(why: str) -> str:  # a run's error is what its server said: nothing it says repaints
        text = printable(why.rstrip("."), 300)  # escaped, a character grows to ten
        return text if len(text) <= 301 else text[:300] + "…"

    parts = []
    for row in result.rows:
        if row.outcome == CLIMBED:
            parts.append(f"{named(row)} failed: {said(row.why)}.")
        elif row.outcome == SKIPPED and row.why:
            parts.append(f"{named(row)} skipped: {said(row.why)}.")
    end = []
    done = next((row for row in result.rows if row.outcome == ANSWERED), None)
    if result.answered and done is not None:
        climbed = any(row.outcome == CLIMBED for row in result.rows)
        end.append(f"Done on {named(done)}{'' if climbed else ', with no climb'}.")
    elif result.stopped:
        end.append(f"Stopped: {said(result.stopped)}.")
    spent = f"Spent {dollars(result.spent_musd)}"
    if result.unpriced:
        rungs = "1 rung" if result.unpriced == 1 else f"{result.unpriced} rungs"
        spent += f", and {rungs} had no cost MetaBot could count"
    end.append(spent + ".")
    line = " ".join([*parts, *end])
    if limit is None or len(line) <= limit:
        return line
    tail = " ".join(end)
    return " ".join(parts)[: max(limit - len(tail) - 2, 0)] + "… " + tail


@dataclass
class LadderResult:
    ladder_id: str
    rows: list[RungRow] = field(default_factory=list)
    final: Any = None  # the last rung's RunResult; None when no rung ran
    answered: bool = False
    stopped: str | None = None  # why the ladder ended without an answer
    spent_musd: int = 0
    unpriced: int = 0  # rungs that ran with no price
    tokens: int = 0  # every rung's, summed
    base: str | None = None  # the snapshot every rung started from, in copies
    applied: bool | None = None  # the answer's work brought back; None: nothing to bring
    apply_note: str | None = None
    patch: str | None = None
    seal: str | None = None  # the receipt over every rung's run; None if it could not be signed
    seal_error: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "ladder_id": self.ladder_id,
            "answered": self.answered,
            "stopped": self.stopped,
            "spent_musd": self.spent_musd,
            "unpriced": self.unpriced,
            "tokens": self.tokens,
            "base": self.base,
            "applied": self.applied,
            "apply_note": self.apply_note,
            "patch": self.patch,
            "seal": self.seal,
            **({"seal_error": self.seal_error} if self.seal_error else {}),
            "rungs": [asdict(row) for row in self.rows],
        }


def ladder_path(ladder_id: str) -> Path:
    """A ladder's record, for a lookup: nothing is created; a writer makes the folder."""
    return kept_home() / "ladders" / f"{safe_id(ladder_id, 'ladder id')}.json"


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
    except (OSError, ValueError, RecursionError):
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


def declined_why(receipt: Path | str) -> str:
    """Why the receipt's first declined call was declined: by a person, or by the run's policy."""
    for v in verdicts_of(receipt):
        if str(v.get("check")).startswith("scope_approval:") and v.get("result") == "warn":
            detail = str(v.get("detail") or "declined")
            return "by the person" if detail == "declined" else detail
    return "declined"


NOBODY = "declined: no human present"  # the gate had no one to ask; no person said no


def declined_by_nobody(receipt: Path | str) -> bool:
    """Every call the receipt declines was declined because no person was there to ask."""
    details = [
        str(v.get("detail"))
        for v in verdicts_of(receipt)
        if str(v.get("check")).startswith("scope_approval:") and v.get("result") == "warn"
    ]
    return bool(details) and all(detail == NOBODY for detail in details)


def finished(result: Any) -> bool:
    """The run reached its own end within its limits, so its work is there to check."""
    return bool(not result.limit and result.ok)


def ended_well(result: Any) -> bool:
    """The run's own record holds no failure: the answer stands until a check says otherwise."""
    return bool(finished(result) and not result.failed and acceptance_of(result.receipt) != "fail")


def judge(
    result: Any,
    gated: bool,
    *,
    check: str | None = None,
    copied: bool = False,
    unread: Sequence[str] = (),
) -> tuple[str, str]:
    """What a rung's run means for the ladder: answered, climb, or stop, and why.

    ``gated``: every tool the engine runs goes through MetaBot's gate first, so
    a failed run on it changed nothing the receipt does not show. ``check``:
    the person's checks on the rung's work, pass or fail. ``copied``: the rung
    worked in a copy of the project, so what it wrote is not in the project,
    and a decline with no one to ask climbs instead of stopping. ``unread``:
    the files here the task names; with no check, a run that made no tool
    call read none of them, so its answer is not work on them.
    """
    if result.limit:
        return STOPPED, f"it passed a limit: {result.limit}"
    if check is None and unread and not result.steps and ended_well(result):
        named = listed(unread)
        if not gated:
            return STOPPED, (
                f"it showed no read of {named}, which the task names, on an engine "
                "whose own tools may have changed the project"
            )
        return CLIMBED, f"it made no tool call, so it read none of {named}, which the task names"
    if check is None and ended_well(result):
        return ANSWERED, ""  # a declined call leaves the answer standing, with its caveat
    if check == "pass" and finished(result) and acceptance_of(result.receipt) != "fail":
        # The person's check judges the work; a call that failed on the way does not undo it.
        if result.failed:
            return ANSWERED, f"its check passed; {result.failed} call(s) on the way did not succeed"
        return ANSWERED, ""
    nobody = bool(result.declined) and copied and declined_by_nobody(result.receipt)
    if result.declined and not nobody:
        why = declined_why(result.receipt)
        return (
            STOPPED,
            f"{result.declined} call(s) declined ({why}); every rung meets the same gate",
        )
    outcome, why = _failed(result, gated, check, copied)
    if nobody and outcome == CLIMBED:
        why += f"; {result.declined} call(s) declined with no one to ask"
    return outcome, why


def _failed(result: Any, gated: bool, check: str | None, copied: bool) -> tuple[str, str]:
    """Climb or stop, for a rung that did not answer and that no decline stopped."""
    wrote = 0 if copied else let_through(result.receipt)
    if wrote:
        return STOPPED, f"it failed after {wrote} write(s) went through; see {result.run_id} first"
    if not gated:
        return STOPPED, "it failed on an engine whose own tools may have changed the project"
    if not result.ok:
        return CLIMBED, result.error or "the run did not end well"
    if result.failed:
        return CLIMBED, f"{result.failed} call(s) did not succeed"
    if check not in (None, "pass"):
        return CLIMBED, f"its check failed: {check.removeprefix('fail: ')}"
    return CLIMBED, "its declared acceptance failed"


# rung, its run id, its meta, the money left, and the folder it works in (None: the person's)
RunRung = Callable[[Rung, str, dict[str, Any], int | None, Path | None], Awaitable[Any]]


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
    checks: Mapping[str, str] | None = None,
    copies: Copies | None = None,
    task: str = "",
    cwd: Path | None = None,
    project: Path | None = None,
) -> LadderResult:
    """Run the rungs in order until one answers, the ladder stops, or the attempts run out.

    ``run_rung(rung, run_id, meta, cap_musd, cwd)`` runs one rung as ``ask``
    would and returns its RunResult; it raises ``no_price`` for a rung it
    cannot price under a cap. ``checks`` run on each rung that finished, in
    its copy or else in ``cwd``, and judge it. With ``copies``,
    each rung works in its own copy of the project, and the answering rung's
    work is brought back. A cancelled rung's error goes up after the record
    is saved.
    """
    _claim(ladder_id)
    named = named_here(task, cwd or Path.cwd()) if task else []  # what no rung may answer unread
    started_at = utc_now()
    result = LadderResult(ladder_id, base=copies.base if copies else None)
    parent: str | None = None
    ran = 0
    answered_at: int | None = None
    current: RungRow | None = None  # the rung under way: a stop still seals what it ran
    try:
        for n, rung in enumerate(rungs, 1):
            row = RungRow(n, rung.engine, rung.model, rung.tier, SKIPPED, label=rung.label())
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
            # a rung whose cost is unknown, its tokens unpriced or none counted, or a request
            # of it lost, leaves the cap unkept
            unknown = next((r for r in result.rows if r.run_id and r.cost_musd is None), None)
            if cap is not None and unknown is not None:
                unkept_why = uncounted(
                    f"rung {unknown.n}", unknown.tokens, unknown.lost, unknown.late
                )
                result.stopped = (
                    f"{unkept_why}, so the ladder's {dollars(max_cost_musd or 0)} cannot be kept"
                )
                break
            run_id = f"{ladder_id}.{n}"
            meta: dict[str, Any] = {"ladder": ladder_id, "rung": n}
            if parent is not None:
                meta["parent"] = parent
            where = copies.make(n) if copies is not None else None
            if copies is not None:
                row.workspace = str(copies.trees[n])
            current = row
            try:
                ran_result = await run_rung(rung, run_id, meta, cap, where)
            except no_price as exc:
                current = None
                row.why = str(exc)
                result.rows.append(row)
                on_line(f"rung {n} {rung.label()} skipped: {row.why}")
                continue
            ran += 1
            parent = run_id
            result.final = ran_result
            row.run_id, row.ok = ran_result.run_id, ran_result.ok
            row.cost_musd, row.cost_source = ran_result.cost_musd, ran_result.cost_source
            row.tokens, row.lost = ran_result.tokens, getattr(ran_result, "lost", False)
            row.late = getattr(ran_result, "named_late", False)
            result.tokens += ran_result.tokens
            if ran_result.cost_musd is None:
                result.unpriced += 1
            else:
                result.spent_musd += ran_result.cost_musd
            unkept = None
            if copies is not None:
                try:
                    row.commit, row.relinked = copies.keep(n, task, run_id)
                except CopyError as exc:
                    unkept = str(exc)
                if row.relinked:
                    on_line(f"rung {n}'s work changed its copy's .git file; MetaBot put it back")
            if checks and finished(ran_result) and unkept is None:
                on_line(f"rung {n} {rung.label()} finished; running its checks")
                try:
                    gate = await run_gate(
                        checks,
                        where if where is not None else (cwd or Path.cwd()),
                        desk_run_id=run_id,
                        inside=copies.trees[n] if copies is not None else None,
                        meta={"ladder": ladder_id, "rung": n},
                        project=project,
                    )
                except ChecksCancelled as exc:  # the stop carries the rung's run, which is sealed
                    raise RunCancelled(ran_result) from exc
                row.check, row.checks_run = gate.summary, gate.run_id
            if copies is not None:
                try:
                    copies.restore(n)
                except CopyError as exc:
                    on_line(f"rung {n}'s copy was not put back to its commit: {exc}")
            row.outcome, row.why = judge(
                ran_result,
                gated(rung.engine),
                check=row.check,
                copied=copies is not None,
                unread=named,
            )
            if unkept is not None and row.outcome != STOPPED:  # no commit: nothing to bring back
                unsaid = f"its work could not be committed in its copy: {unkept}"
                row.why = f"{row.why}; {unsaid}" if row.outcome == CLIMBED else unsaid
                row.outcome = CLIMBED
            result.rows.append(row)
            current = None
            _save(result)
            if row.outcome == ANSWERED:
                result.answered = True
                answered_at = n
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
        commit = result.rows[-1].commit if answered_at is not None else None
        if copies is not None and answered_at is not None and commit:
            result.applied, result.apply_note = copies.bring_back(answered_at, commit)
            result.patch = str(copies.patch_path())
            on_line(result.apply_note)
    finally:
        if current is not None:  # stopped or broken before its row was kept
            _unfinished(result, current, f"{ladder_id}.{current.n}")
        if copies is not None:  # the copy whose work did not apply stays, for the person
            left = copies.close(keep=answered_at if result.applied is False else None)
            if left:
                on_line(f"{len(left)} rung copy(ies) held work and stay: {left[0].parent}")
        try:
            result.seal = str(_seal(result, task, private, max_cost_musd, started_at))
        except (OSError, ValueError) as exc:  # the record says so, and every rung receipt stands
            result.seal_error = str(exc)
        else:  # beside the rungs' copies, so the project's folder checks alone
            _copied, why = project_copy(Path(result.seal), project)
            if why is not None:
                on_line(f"the seal: {why}")
        _save(result)
    return result


def _unfinished(result: LadderResult, row: RungRow, run_id: str) -> None:
    """A rung that never got to its row: its run, its checks and its spend still count."""
    if row.run_id is None:  # the rung never returned: what it sealed says what it spent
        row.run_id, row.ok = run_id, False
        row.cost_musd = signed_cost(receipt_path(run_id))
        if row.cost_musd is not None:
            result.spent_musd += row.cost_musd
        elif receipt_path(run_id).is_file() or run_path(run_id).is_dir():  # it began: unknown
            result.unpriced += 1
    checks = run_id + CHECKS_SUFFIX
    if row.checks_run is None and receipt_path(checks).is_file():
        row.checks_run = checks
    row.outcome, row.why = STOPPED, row.why or "it did not finish"
    result.rows.append(row)
    if result.stopped is None and not result.answered:
        result.stopped = f"rung {row.n} did not finish"


def _seal(
    result: LadderResult, task: str, private: bool, cap_musd: int | None, started_at: str
) -> Path:
    """One receipt over every rung's run and checks run, in the order they ran."""
    members: list[Member] = []
    for row in result.rows:
        if row.run_id is None:  # skipped: it never ran
            continue
        answered = row.outcome == ANSWERED
        members.append(Member(row.run_id, "rung", receipt_path(row.run_id), answered))
        if row.checks_run is not None:
            checked = row.check == "pass"
            members.append(Member(row.checks_run, "checks", receipt_path(row.checks_run), checked))
    said = scrub(task, record_secrets())
    workflow = f"private={'true' if private else 'false'}"
    verdicts = [
        {"check": "workflow", "result": "pass", "detail": workflow},
        *([budget_verdict(cap_musd, _counted(result))] if cap_musd is not None else []),
    ]
    return seal_runs(
        result.ladder_id,
        members,
        delivered=result.answered,
        kind_detail="kind=ladder",
        verdicts=verdicts,
        started_at=started_at,
        meta={"sealed": "ladder", "task": said},
    )


def _counted(result: LadderResult) -> int | None:
    """What the ladder spent, for its seal; None when a rung's cost is unknown."""
    return None if result.unpriced else result.spent_musd


def spent_line(result: LadderResult) -> str:
    text = f"ladder {result.ladder_id} cost {dollars(result.spent_musd)}"
    if result.unpriced:
        text += f", with {result.unpriced} rung(s) whose cost MetaBot could not count"
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
        base = rung_base_url(rung, cfg)
        if rung.engine == "openai" and is_local_url(base):
            found = cached_mode(base, rung.model or "")
            probe = f"  probe {found or 'none yet'}"
        print(f"rung {n}  {rung.tier:<5}  {rung.label()}{probe}  ({whose})")
    if not rungs:
        print("no rung here: install an engine CLI, or start and probe the local model server")
    if note:
        print(f"note: {note}")
    print(f"at most {most} rung(s) run for one task")
    return 0
