"""``metabot ui``'s Home (U2): Today, Plan it, and a kept workflow's page.

Today is what needs the person, from what is on this machine now: the asks waiting on runs
this UI started, the newest checks that failed, what is running, and the newest receipt,
checked as the page is drawn. What runs is read from ``<home>/live/``, which is what each
process says of itself and is not signed; the page says so.

Plan it hands one sentence to ``metabot propose``'s rules, for the folder the UI started in,
and shows the draft: each step's engine, what it may change, whether it asks before it
writes, the rules that fired and what is missing. Approve and run starts the draft's step
once, as the page starts an ask, a ladder or an office run, and the run's receipt signs a
``plan`` verdict: the draft's sha256 and the sha256 of what the page showed it runs on,
approved in the web UI. The draft names no ladder rung, and an office desk on ``harness:
any`` takes its engine from the roster, so what it runs on is hashed apart from it. The
approval names the plan the person read: the form carries both, the plan is made again
from the same sentence, folder and cap, and a plan that changed in between, in its draft
or in what it runs on, never runs. Nothing is kept in the workflow store, no workflow
approval is written, and no setting is changed (PD-9).

A workflow kept in the store has a page of its own, by its sha256, read only.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse
from starlette.routing import Route

from metabot.web_guard import Guard, checked_form, nonce_of
from metabot.web_pages import (
    CAP_LABEL,
    SHA256,
    _error,
    _hidden,
    _pre,
    cap_field,
    clip,
    e,
    layout,
    link,
    message_page,
)
from metabot.web_runs import Broker, WebApprover

# ── the words ───────────────────────────────────────────────────────────────

NEEDS = "Needs you"
NEEDS_NONE = "Nothing needs you right now."
FAILED = "Checks that failed"
FAILED_NONE = "No check has failed lately."
RUNNING = "Running now"
RUNNING_NONE = "Nothing is running."
RUNNING_UNSIGNED = (
    "What is running is what each process says of itself, and it is not signed. "
    "The receipt is the record."
)
ELSEWHERE = "started outside this page: stop it where it runs"
LATEST = "Latest receipt"
LATEST_NONE = "No receipt yet."
CHANGED = "It was changed after the run."
PLAN_TITLE = "What should get done?"
PLAN_FIELD = "The work"
PLAN_PLACEHOLDER = "Describe the work in one sentence."
PLAN_BUTTON = "Plan it"
PLAN_RESULT = "Here is the plan."
PLAN_LINE = "Each step shows its engine, what it may change, and whether it asks before it writes."
APPROVE = "Approve and run"
CANCEL = "Cancel"
APPROVE_LINE = "Your approval is recorded in a receipt."
DESTRUCTIVE_LINE = "A call MetaBot classes destructive waits for a person to approve it."
NO_PLAN = "No plan yet. Describe the work in more detail."
ONE_LINE = "No plan yet. Describe the work in one sentence, on one line."
DECLINED = "Plan not approved. Nothing ran."
PLAN_CHANGED = (
    "The plan changed since you read it, so nothing ran. Here it is as it stands now: "
    "read it again before you approve it."
)
BLOCKED = "This plan cannot run yet. Change what is missing, then plan it again."
WORKFLOW_LINE = "This workflow is named by its hash. The same file always gives the same hash."
COPY_HASH = "Copy hash"
SENTENCE_LIMIT = 2000  # characters of the sentence Plan it takes
FAILED_SHOWN = 5  # failed checks Today lists
RUNNING_KINDS = frozenset({"run", "ladder", "office", "routine"})  # what a person may stop
RUNNING_HEAD = ("state", "run", "what it runs", "stop")
PLAN_FIELDS = ("task", "max_cost")
CAP_HERE = f'give a spend cap in "{CAP_LABEL}" below, such as 2.00'  # propose's CAP_HOW, here
CAP_LEFT = "max_cost is left empty for you to set"  # the rule propose fires with no cap


# ── Today ───────────────────────────────────────────────────────────────────


def _signed_acceptance(path: Path) -> str | None:
    """A receipt's ``declared_acceptance`` result, read from the signed envelope once it
    verifies; None when it does not verify or has none."""
    import json

    from emerge.run_attestation import verify_run_attestation

    try:
        envelope = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(envelope, dict) or not verify_run_attestation(envelope).valid:
        return None
    if envelope.get("run_id") != path.name[: -len(".json")]:
        return None
    for verdict in envelope.get("verdicts") or []:
        if isinstance(verdict, dict) and verdict.get("check") == "declared_acceptance":
            return str(verdict.get("result"))
    return None


def failed_checks(rows: Sequence[dict[str, Any]]) -> list[str]:
    """The runs whose checks failed, newest first, from the index rows given: each checks
    run's receipt verifies and signs that its checks failed."""
    from metabot.checks import CHECKS_SUFFIX
    from metabot.paths import receipts_dir
    from metabot.web_data import is_safe_id

    found: list[str] = []
    for row in reversed(rows):
        run_id = row.get("run_id")
        if not (isinstance(run_id, str) and run_id.endswith(CHECKS_SUFFIX) and is_safe_id(run_id)):
            continue
        judged = run_id[: -len(CHECKS_SUFFIX)]
        if judged in found or not is_safe_id(judged):
            continue
        if _signed_acceptance(receipts_dir() / f"{run_id}.json") == "fail":
            found.append(judged)
        if len(found) >= FAILED_SHOWN:
            break
    return found


def today_view() -> dict[str, Any]:
    """Today's groups that are read from the MetaBot home: plain data, never HTML."""
    from metabot.live import live_entries
    from metabot.receipts import list_index
    from metabot.web_data import ROW_LIMIT, is_safe_id, receipt_state

    view: dict[str, Any] = {"error": None, "failed": [], "latest": None}
    try:
        rows = list_index()
    except (OSError, ValueError) as exc:
        rows, view["error"] = [], f"cannot read the run index: {exc}"
    view["failed"] = failed_checks(rows[-ROW_LIMIT:])
    newest = next((r["run_id"] for r in reversed(rows) if is_safe_id(r.get("run_id"))), None)
    if newest is not None:
        view["latest"] = (newest, receipt_state(newest))
    alive, _stale = live_entries()  # a stale entry is never shown as running
    view["running"] = [
        entry
        for entry in alive
        if entry.get("kind") in RUNNING_KINDS
        and not entry.get("parent")
        and is_safe_id(entry.get("id"))
    ]
    return view


def _count(n: int, one: str, many: str) -> str:
    return one if n == 1 else many.format(n=n)


def _needs(broker: Broker) -> str:
    asks = [ask for ask in broker.asks.values() if ask.state == "waiting"]
    if not asks:
        return f'<h2>{e(NEEDS)}</h2><p class="muted">{e(NEEDS_NONE)}</p>'
    said = _count(len(asks), "1 approval is waiting.", "{n} approvals are waiting.")
    rows = "".join(
        f'<li><span class="warn">Waiting</span> {link("/live/", ask.run_id, f"Run {ask.run_id}")}'
        f": <b>{e(ask.request.cls.label)}</b> call <code>{e(clip(ask.request.tool, 200))}</code>"
        "</li>"
        for ask in asks
    )
    return f"<h2>{e(NEEDS)}</h2><p>{e(said)}</p><ul>{rows}</ul>"


def _failed(view: dict[str, Any]) -> str:
    runs = view["failed"]
    if not runs:
        return f'<h2>{e(FAILED)}</h2><p class="muted">{e(FAILED_NONE)}</p>'
    rows = "".join(
        f'<li><span class="bad">Failed</span> A check failed on {link("/runs/", run_id)}.</li>'
        for run_id in runs
    )
    return f"<h2>{e(FAILED)}</h2><ul>{rows}</ul>"


def _running(view: dict[str, Any], broker: Broker, csrf: str) -> str:
    ours = {r.run_id: r for r in broker.runs.values() if r.state == "running"}
    entries = list(view["running"])
    listed = {str(entry.get("id")) for entry in entries}
    # a run this page started that has not said so yet, as a ladder setting up its rungs
    entries += [{"id": rid, "kind": "run"} for rid in sorted(ours) if rid not in listed]
    if not entries:
        return f'<h2>{e(RUNNING)}</h2><p class="muted">{e(RUNNING_NONE)}</p>'
    said = _count(len(entries), "1 run in progress.", "{n} runs in progress.")
    rows = []
    for entry in entries:
        run_id = str(entry.get("id"))
        doing = entry.get("current") or entry.get("name") or entry.get("engine")
        what = f" {e(clip(doing, 120))}" if doing else ""
        kind = e(clip(entry.get("kind"), 20))
        if run_id in ours:  # the button's name says which run it stops
            stop = (
                f'<form method="post" action="/live/{e(run_id)}/stop">{_hidden(csrf)}'
                f'<button aria-label="Stop run {e(run_id)}">Stop</button></form>'
            )
            rows.append(
                f'<tr><td><span class="none">Running</span></td>'
                f"<td>{link('/live/', run_id)}</td><td>{kind}{what}</td><td>{stop}</td></tr>"
            )
        else:
            rows.append(
                f'<tr><td><span class="none">Running</span></td><td><code>{e(clip(run_id))}'
                f'</code></td><td>{kind}{what}</td><td class="muted">{e(ELSEWHERE)}</td></tr>'
            )
    head = "".join(f"<th>{e(h)}</th>" for h in RUNNING_HEAD)
    return (
        f"<h2>{e(RUNNING)}</h2><p>{e(said)}</p><table><tr>{head}</tr>{''.join(rows)}</table>"
        f'<p class="muted">{e(RUNNING_UNSIGNED)}</p>'
    )


def _latest(view: dict[str, Any]) -> str:
    latest = view["latest"]
    if latest is None:
        return f'<h2>{e(LATEST)}</h2><p class="muted">{e(LATEST_NONE)}</p>'
    run_id, state = latest
    name = f"Receipt {link('/runs/', run_id)}"
    if state == "valid" or state.startswith("valid, "):
        said = f'{name}: <span class="ok">valid</span>.'
    elif state.startswith("INVALID"):
        said = f'{name}: <span class="bad">INVALID</span>. {e(CHANGED)}'
    else:  # its journal no longer matches, or a member of its seal does not hold
        said = f'{name}: <span class="bad">{e(clip(state, 200))}</span>.'
    return f"<h2>{e(LATEST)}</h2><p>{said}</p>"


async def today_html(broker: Broker, csrf: str) -> str:
    """Today's four groups. What the home holds is read off the loop; the asks and the runs
    this page started are read on it, where they change."""
    view = await run_in_threadpool(today_view)
    return (
        _error(view["error"])
        + _needs(broker)
        + _failed(view)
        + _running(view, broker, csrf)
        + _latest(view)
    )


# ── Plan it ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Plan:
    """A draft from ``propose``, with the person's spend cap in its step, as it would run."""

    task: str
    cap_said: str  # the cap as the form gave it, so the approval form sends it back
    proposal: Any  # metabot.propose.Proposal
    wf: Any  # metabot.workflows.Workflow: the draft, read as a workflow
    missing: tuple[str, ...]  # what the draft says to set before it runs
    blocked: tuple[str, ...]  # why it cannot run here; empty when it can
    rules: tuple[str, ...]  # each rule that fired, with the person's cap in place

    @property
    def sha256(self) -> str:
        return str(self.wf.sha256)

    @property
    def runs_on_sha256(self) -> str:
        """What the page shows the step runs on, hashed: the draft names no ladder rung, and
        a desk on ``harness: any`` takes its engine from the roster, so the draft's sha256
        alone does not cover it."""
        lines = json.dumps(list(self.proposal.runs_on), ensure_ascii=True, separators=(",", ":"))
        return hashlib.sha256(lines.encode("ascii")).hexdigest()

    @property
    def step(self) -> Any:
        return self.wf.steps[0]


def _sentence(task: str) -> None:
    if not task.strip():
        raise ValueError(NO_PLAN)
    if len(task) > SENTENCE_LIMIT:
        raise ValueError(f"No plan yet. Describe the work in at most {SENTENCE_LIMIT} characters.")
    if not task.isprintable():  # a line break, a tab, or a mark that reorders text
        raise ValueError(ONE_LINE)


def _cap_text(musd: int) -> str:
    """Micro-dollars as the dollars a workflow's ``max_cost`` takes."""
    from metabot.spend import MUSD

    return f"{(Decimal(musd) / MUSD).normalize():f}"


def make_plan(values: dict[str, str], start_dir: Path) -> Plan:
    """The plan ``propose`` makes for the form's sentence in the start folder, with its cap.

    Raises ValueError naming what is wrong. Nothing is started, written or kept.
    """
    from metabot.config import ConfigError, load_config
    from metabot.propose import CAP_HOW, propose
    from metabot.spend import dollars
    from metabot.web_app import spend_cap
    from metabot.workflows import WorkflowError, here_problems, parse_workflow, workflow_bytes

    task = values["task"]
    _sentence(task)
    cap = spend_cap(values["max_cost"])
    try:
        cfg = load_config()
    except ConfigError as exc:
        raise ValueError(str(exc)) from None
    proposal = propose(task, start_dir.resolve(), cfg)
    data = copy.deepcopy(proposal.data)
    if cap is not None:
        for step in data["steps"]:
            step["max_cost"] = _cap_text(cap)
    try:
        wf = parse_workflow(workflow_bytes(data))
    except WorkflowError:
        raise ValueError(NO_PLAN) from None
    capped = cap is not None
    missing = tuple(  # the cap goes in this page's field, not in a file the page cannot edit
        m.removesuffix(CAP_HOW) + CAP_HERE if m.endswith(CAP_HOW) else m
        for m in proposal.missing
        if not (capped and m.endswith(CAP_HOW))
    )
    blocked = missing or tuple(here_problems(wf))
    rules = proposal.rules
    if cap is not None:  # the rule that left max_cost empty is the person's cap now
        said = f"max_cost is the spend cap you gave: {dollars(cap)}"
        rules = tuple(said if r.startswith(CAP_LEFT) else r for r in rules)
        rules += () if said in rules else (said,)
    return Plan(task, values["max_cost"].strip(), proposal, wf, missing, blocked, rules)


def _changes(step: Any) -> tuple[str, str]:
    """What a step may change, and whether it asks before it writes."""
    if step.read_only:
        return "Read only", "Yes: it changes nothing, and a write it tries waits for you"
    if step.allow_write:
        return "Allowed writes", "No: writes run without asking. A destructive call still waits."
    if step.allow:
        return "Asks before it writes", "Yes, but for " + ", ".join(step.allow)
    return "Asks before it writes", "Yes: each write waits for you"


def _plan_table(plan: Plan) -> str:
    from metabot.spend import dollars

    step = plan.step
    may, asks = _changes(step)
    engines = "<br>".join(e(clip(line, 300)) for line in plan.proposal.runs_on) or "-"
    cap = dollars(step.max_cost_musd) if step.max_cost_musd is not None else "none"
    head = "".join(
        f"<th>{h}</th>"
        for h in ("step", "engine", "what it may change", "asks before it writes", "spend cap")
    )
    row = (
        f"<tr><td>{e(step.name)} ({e(step.kind)})</td><td>{engines}</td><td>{e(may)}</td>"
        f"<td>{e(asks)}</td><td>{e(cap)}</td></tr>"
    )
    return f"<table><tr>{head}</tr>{row}</table>"


def _plan_form(csrf: str, values: dict[str, str], *, primary: bool = True) -> str:
    button = f'<button class="{"primary" if primary else ""}">{e(PLAN_BUTTON)}</button>'
    return (
        f'<form method="post" action="/plan">{_hidden(csrf)}'
        f'<p><label for="plan-task">{e(PLAN_FIELD)}</label><br>'
        f'<input id="plan-task" name="task" size="80" autocomplete="off" '
        f'placeholder="{e(PLAN_PLACEHOLDER)}" '
        f'value="{e(clip(values.get("task", ""), SENTENCE_LIMIT))}"></p>'
        + cap_field(values.get("max_cost", ""))
        + f'<div class="actions">{button}</div></form>'
    )


def _a(shape: str) -> str:
    return f"an {e(shape)}" if shape[:1] in "aeiou" else f"a {e(shape)}"


def _plan_result(plan: Plan, csrf: str) -> str:
    p = plan.proposal
    body = (
        f"<h2>{e(PLAN_RESULT)}</h2><p>{e(PLAN_LINE)}</p>"
        + f'<p class="muted">For <code>{e(clip(plan.task, 300))}</code> in '
        + f"<code>{e(clip(plan.wf.cwd, 300))}</code>, as {_a(p.shape)}.</p>"
        + _plan_table(plan)
        + "<h2>Rules that fired</h2><ul>"
        + "".join(f"<li>{e(clip(rule, 600))}</li>" for rule in plan.rules)
        + "</ul>"
    )
    if plan.missing:
        body += "<h2>Missing</h2><ul>" + "".join(
            f'<li class="bad">{e(clip(m, 600))}</li>' for m in plan.missing
        )
        body += "</ul>"
    body += (
        f'<p class="muted">Plan sha256 <code>{e(plan.sha256)}</code>; what it runs on, sha256 '
        f"<code>{e(plan.runs_on_sha256)}</code></p>"
    )
    if plan.blocked:
        if not plan.missing:
            body += "<h2>Missing</h2><ul>" + "".join(
                f'<li class="bad">{e(clip(m, 600))}</li>' for m in plan.blocked
            )
            body += "</ul>"
        return body + f'<p class="warn">{e(BLOCKED)}</p>'
    approve = (
        f'<form method="post" action="/plan/run">{_hidden(csrf)}'
        f'<input type="hidden" name="task" value="{e(plan.task)}">'
        f'<input type="hidden" name="max_cost" value="{e(plan.cap_said)}">'
        f'<input type="hidden" name="sha256" value="{e(plan.sha256)}">'
        f'<input type="hidden" name="runs_on" value="{e(plan.runs_on_sha256)}">'
        f'<div class="actions"><button class="primary">{e(APPROVE)}</button>'
        f'<a class="button" href="/plan?cancelled=1">{e(CANCEL)}</a></div>'
        f'<p class="muted">{e(APPROVE_LINE)}</p><p class="muted">{e(DESTRUCTIVE_LINE)}</p></form>'
    )
    return body + approve


def plan_page(
    nonce: str,
    csrf: str,
    *,
    values: dict[str, str],
    plan: Plan | None = None,
    error: str | None = None,
    note: str | None = None,
) -> str:
    """Plan it: the sentence and the cap, and once planned, the plan and its approval.

    A plan that can run offers its approval, and the form to plan again is left out, so
    the screen has one action; a plan that cannot run shows the form again instead."""
    top = _error(error) + (f'<p class="muted">{e(note)}</p>' if note else "")
    if plan is None:
        body = top + _plan_form(csrf, values)
    elif plan.blocked:
        body = top + _plan_result(plan, csrf) + _plan_form(csrf, values)
    else:
        body = top + _plan_result(plan, csrf)
    return layout(PLAN_TITLE, body, nonce, kicker=PLAN_BUTTON, narrow=plan is None)


# ── a kept workflow ─────────────────────────────────────────────────────────


def workflow_view(sha256: str) -> dict[str, Any] | None:
    """A workflow the store keeps by ``sha256``, as its page shows it; None when the store
    keeps none by it. Its bytes are hashed again as they are read."""
    from metabot.workflow_approval import approval_state
    from metabot.workflow_commands import fact_lines
    from metabot.workflows import WorkflowError, load_workflow

    try:
        wf = load_workflow(sha256)
    except KeyError:
        return None
    except (WorkflowError, OSError) as exc:  # changed since it was kept, or the index
        return {"sha256": sha256, "error": str(exc)}
    view: dict[str, Any] = {"sha256": wf.sha256, "name": wf.name, "raw": wf.raw, "error": None}
    try:
        view["facts"] = fact_lines(wf)
    except ValueError as exc:  # settings that do not read
        view["facts"] = [f"cannot be shown here: {exc}"]
    state = approval_state(wf.sha256)  # matched by this home's public key
    if state.unreadable_revokes:
        view["approval"] = (
            "bad",
            "Revoked",
            "a revoke receipt for it does not verify, so every approval of it is taken as "
            f"revoked ({', '.join(state.unreadable_revokes)})",
        )
    elif state.in_force:
        newest = max(state.in_force, key=lambda a: (a.signed_at, a.run_id))
        said = f"approved at {newest.signed_at}, receipt {newest.run_id}"
        view["approval"] = ("ok", "In force", said)
    elif state.revoked:
        view["approval"] = ("bad", "Revoked", "its approval was revoked")
    else:
        view["approval"] = ("none", "None", "not approved on this machine")
    return view


def workflow_page(view: dict[str, Any], nonce: str) -> str:
    sha = view["sha256"]
    copy_hash = (
        f'<p><label for="wf-hash">{e(COPY_HASH)}</label><br>'
        f'<input id="wf-hash" readonly size="66" value="{e(sha)}"></p>'
    )
    if view["error"]:
        body = f"<p>{e(WORKFLOW_LINE)}</p>{copy_hash}{_error(view['error'])}"
        return layout("Workflow", body, nonce)
    cls, word, said = view["approval"]
    facts = (
        '<table class="facts">'
        f"<tr><th>name</th><td><code>{e(view['name'])}</code></td></tr>"
        f'<tr><th>approval</th><td><span class="{cls}">{e(word)}</span> '
        f"{e(clip(said, 600))}</td></tr>"
        "</table>"
    )
    text = view["raw"].decode("utf-8")  # it read as UTF-8, or the store would not load it
    body = (
        f"<p>{e(WORKFLOW_LINE)}</p>"
        + copy_hash
        + facts
        + "<h2>Steps</h2>"
        + _pre("\n".join(view["facts"]))
        + "<h2>The file</h2>"
        + f"<pre>{e(text)}</pre>"  # all of it, as the store keeps it: at most MAX_BYTES
    )
    return layout("Workflow", body, nonce)


# ── the routes ──────────────────────────────────────────────────────────────

StartLadder = Callable[[dict[str, Any], tuple[dict[str, str], ...]], Awaitable[RedirectResponse]]


def home_routes(
    guard: Guard,
    broker: Broker,
    start_dir: Path,
    make_engine: Callable[[str], Any] | None,
    start_ladder: StartLadder,
) -> list[Route]:
    """Plan it and the workflow page, for one start of the UI."""

    def page(
        request: Request,
        values: dict[str, str],
        status: int = 200,
        **kw: Any,
    ) -> HTMLResponse:
        return HTMLResponse(
            plan_page(nonce_of(request), guard.csrf, values=values, **kw), status_code=status
        )

    async def plan(request: Request) -> HTMLResponse:
        if request.method != "POST":
            cancelled = request.query_params.get("cancelled") == "1"
            return page(request, {}, note=DECLINED if cancelled else None)
        form = await checked_form(request, guard)
        values = {k: v if isinstance(v := form.get(k), str) else "" for k in PLAN_FIELDS}
        try:
            made = await run_in_threadpool(make_plan, values, start_dir)
        except ValueError as exc:
            return page(request, values, 400, error=str(exc))
        return page(request, values, plan=made)

    async def approve(request: Request) -> HTMLResponse | RedirectResponse:
        form = await checked_form(request, guard)
        values = {k: v if isinstance(v := form.get(k), str) else "" for k in PLAN_FIELDS}
        read, read_on = form.get("sha256"), form.get("runs_on")
        if not (
            isinstance(read, str)
            and SHA256.fullmatch(read)
            and isinstance(read_on, str)
            and SHA256.fullmatch(read_on)
        ):
            return page(request, values, 400, error="That is not a plan's sha256.")
        try:
            made = await run_in_threadpool(make_plan, values, start_dir)
        except ValueError as exc:
            return page(request, values, 400, error=str(exc))
        same = hmac.compare_digest(made.sha256, read)
        same_on = hmac.compare_digest(made.runs_on_sha256, read_on)
        if not (same and same_on):  # the plan, or what it runs on, changed under the person
            return page(request, values, 400, plan=made, error=PLAN_CHANGED)
        if made.blocked:
            return page(request, values, 400, plan=made, error=BLOCKED)
        try:
            return await start(made)
        except ValueError as exc:  # its engine, login or office cannot start here
            return page(request, values, 400, plan=made, error=str(exc))

    async def start(made: Plan) -> RedirectResponse:
        """One run of the plan's step, as the page starts its kind, signing the plan."""
        from metabot.receipts import plan_verdict

        step, signed = made.step, (plan_verdict(made.sha256, made.runs_on_sha256),)
        if step.kind == "ladder":
            ladder = {
                "prompt": step.prompt,
                "cwd": Path(made.wf.cwd),
                "checks": list(step.ladder_checks),
                "role": step.role,
                "read_only": step.read_only,
                "allow_write": step.allow_write,
                "allow": list(step.allow),
                "connections": step.connections,
                "plugins": step.plugins,
                "max_cost": step.max_cost_musd,
            }
            return await start_ladder(ladder, signed)
        if step.kind == "ask":
            return await _start_ask(made, signed)
        return await _start_office(made, signed)

    async def _start_ask(made: Plan, signed: tuple[dict[str, str], ...]) -> RedirectResponse:
        from metabot.commands import ask_once
        from metabot.paths import new_run_id
        from metabot.runner import Limits
        from metabot.workflows import ask_engine

        step, cwd = made.step, Path(made.wf.cwd)
        engine, name, model = await run_in_threadpool(ask_engine, step)  # ValueError: refused
        if make_engine is not None:
            engine = make_engine(name)
        run_id = new_run_id()
        live = broker.open(run_id, engine=name, model=model, cwd=str(cwd), prompt=step.prompt)
        cap = step.max_cost_musd
        coro = ask_once(
            engine_name=name, prompt=step.prompt, cwd=cwd, model=model,
            allow_write=step.allow_write, allows=list(step.allow), read_only=step.read_only,
            connections=step.connections, plugins=step.plugins, trace=live.trace,
            role=step.role, approver=WebApprover(broker, run_id), run_id=run_id, engine=engine,
            limits=Limits(max_cost_musd=cap) if cap is not None else None, verdicts=signed,
        )  # fmt: skip
        broker.start(run_id, coro)
        return RedirectResponse(f"/live/{run_id}", status_code=303)

    async def _start_office(made: Plan, signed: tuple[dict[str, str], ...]) -> RedirectResponse:
        from metabot.office_run import ROUNDS_DEFAULT, run_fan, run_relay
        from metabot.paths import new_run_id
        from metabot.roster import roster_entries
        from metabot.workflows import open_office

        step = made.step
        office = await run_in_threadpool(open_office, step)  # as pinned; ValueError: refused
        roster = await run_in_threadpool(roster_entries)
        names = list(step.desks) if step.desks else [
            n for n, desk in office.desks.items() if not desk.is_advisor
        ]  # fmt: skip
        office_run_id = new_run_id()
        mode = "fan" if step.fan else "relay"
        live = broker.open(
            office_run_id, cwd=str(office.root), prompt=step.prompt, office=office.name,
            mode=mode, desks=names,
        )  # fmt: skip

        def on_desk(row: Any) -> None:
            runs_on = f"{row.engine}/{row.model}" if row.model else row.engine
            live.trace.note(f"[metabot] desk {row.label}: {runs_on}")

        coro = (run_fan if step.fan else run_relay)(
            office,
            step.prompt,
            approver=WebApprover(broker, office_run_id),
            desks=list(step.desks) if step.desks else None,
            allow_write=step.allow_write,
            allows=list(step.allow),
            roster=roster,
            connections=step.connections,
            plugins=step.plugins,
            on_event=live.trace,
            on_desk=on_desk,
            make_engine=make_engine,
            checks=step.office_checks,
            office_run_id=office_run_id,
            rounds=(step.rounds or ROUNDS_DEFAULT) if step.until_checks else 1,
            max_cost_musd=step.max_cost_musd,
            verdicts=signed,
            **({} if step.fan else {"advisors": step.advisors}),
        )
        broker.start(office_run_id, coro)
        return RedirectResponse(f"/live/{office_run_id}", status_code=303)

    def workflow(request: Request) -> HTMLResponse:
        sha = request.path_params["sha"]
        if not SHA256.fullmatch(sha):
            page = message_page("Not done", "That is not a workflow's sha256.", nonce_of(request))
            return HTMLResponse(page, status_code=400)
        view = workflow_view(sha)
        if view is None:
            said = f"No workflow is kept as {sha} in this MetaBot home."
            return HTMLResponse(message_page("Not found", said, nonce_of(request)), 404)
        return HTMLResponse(workflow_page(view, nonce_of(request)))

    return [
        Route("/plan", plan, methods=["GET", "POST"]),
        Route("/plan/run", approve, methods=["POST"]),
        Route("/workflows/{sha}", workflow),
    ]
