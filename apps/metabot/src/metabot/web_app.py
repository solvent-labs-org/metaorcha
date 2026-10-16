"""``metabot ui``: the pages and their routes, behind the guard in ``metabot.web_guard``.

The read-only page routes are plain functions, which Starlette runs on its
thread pool, so reading files and checking receipts never holds up the
server. The chat and office routes run in the event loop, beside the runs
they start.
"""

from __future__ import annotations

import contextlib
import copy
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse
from starlette.routing import Route
from starlette.types import ASGIApp

from metabot import web_data as data
from metabot import web_pages as pages
from metabot.web_guard import Guard, checked_form, guarded, nonce_of
from metabot.web_runs import AskAnswered, AskExpired, Broker, WebApprover

PROMPT_LIMIT = 100_000  # characters of a task typed in the page
FIELDS = ("prompt", "engine", "role", "model", "folder")
OFFICE_FIELDS = ("prompt", "folder", "mode", "checks", "advisors")  # and a "desk" per desk
MODES = ("relay", "fan")
TITLES = {400: "Not done", 404: "Not found", 409: "Answered already", 410: "No longer waiting"}


def _message(request: Request, status: int, text: str, back: str | None = None) -> HTMLResponse:
    page = pages.message_page(TITLES.get(status, "Not done"), text, nonce_of(request), back=back)
    return HTMLResponse(page, status_code=status)


# ── what is kept ────────────────────────────────────────────────────────────


def runs(request: Request) -> HTMLResponse:
    rows, total, error = data.run_rows()
    return HTMLResponse(pages.runs_page(rows, total, error, nonce_of(request)))


def run(request: Request) -> HTMLResponse:
    run_id = request.path_params["run_id"]
    if not data.is_safe_id(run_id):
        return _message(request, 400, "That is not a run id.")
    view = data.run_view(run_id)
    if view is None:
        return _message(request, 404, f"There is no run {run_id} in this MetaBot home.")
    return HTMLResponse(pages.run_page(view, nonce_of(request)))


def office(request: Request) -> HTMLResponse:
    office_run_id = request.path_params["office_run_id"]
    if not data.is_safe_id(office_run_id):
        return _message(request, 400, "That is not an office run id.")
    view = data.office_view(office_run_id)
    if view is None:
        return _message(request, 404, f"There is no office run {office_run_id} in this home.")
    return HTMLResponse(pages.office_page(view, nonce_of(request)))


def connections(request: Request) -> HTMLResponse:
    rows, error = data.connection_view()
    return HTMLResponse(pages.connections_page(rows, error, nonce_of(request)))


def routines(request: Request) -> HTMLResponse:
    rows, recent, error = data.routine_view()
    return HTMLResponse(pages.routines_page(rows, recent, error, nonce_of(request)))


# ── a run started in the page ───────────────────────────────────────────────


def _task(prompt: str) -> None:
    """ValueError when a task typed in the page cannot be run."""
    if not prompt.strip():
        raise ValueError("the task is empty")
    if len(prompt) > PROMPT_LIMIT:
        raise ValueError(f"the task is longer than {PROMPT_LIMIT} characters")


def _folder(text: str, start_dir: Path) -> Path:
    """The folder a form names, from where the UI started; ValueError when it is not one."""
    try:
        found = (start_dir / Path(text).expanduser()).resolve()
    except (OSError, RuntimeError, ValueError):
        found = None
    if found is None or not found.is_dir():
        raise ValueError(f"{text[:300]} is not a folder")
    return found


def plan_run(values: dict[str, str], start_dir: Path) -> dict[str, Any]:
    """What a chat form asks to run, as ``ask`` would run it; ValueError naming what is wrong."""
    from metabot.commands import default_engine
    from metabot.config import MODEL_RE, load_config
    from metabot.engines import ENGINES
    from metabot.roster import ROLES, pick

    load_config()  # settings that do not read stop the run here, as they stop metabot ask
    prompt = values["prompt"]
    _task(prompt)
    role = values["role"] or None
    if role is not None and role not in ROLES:
        raise ValueError(f"unknown role {role[:40]!r}; choose one of: {', '.join(ROLES)}")
    engine = values["engine"] or None
    if engine is not None and engine not in ENGINES:
        raise ValueError(f"unknown engine {engine[:40]!r}; choose one of: {', '.join(ENGINES)}")
    model = values["model"].strip() or None
    if model is not None and not MODEL_RE.match(model):
        raise ValueError(f"{model[:60]!r} is not a model name")
    engine, model = pick(role, engine, model)
    cwd = _folder(values["folder"].strip() or str(start_dir), start_dir)
    return {
        "engine_name": engine or default_engine(),
        "model": model,
        "cwd": cwd,
        "prompt": prompt,
        "role": role,
    }


# ── an office run started in the page ───────────────────────────────────────


def office_in(folder: str, start_dir: Path) -> Any:
    """The office in a form's folder, read as ``office run --dir`` reads it; ValueError if none."""
    from metabot.config import load_config
    from metabot.office import OFFICE_FILE, OfficeError, load_office, office_root

    root = office_root(_folder(folder.strip() or str(start_dir), start_dir))
    if not (root / OFFICE_FILE).is_file():
        raise ValueError(f"no {OFFICE_FILE} in {str(root)[:300]}; Code Desks' desk init writes one")
    try:
        return load_office(root, load_config().fan_cap)
    except OfficeError as exc:
        raise ValueError(f"{str(root / OFFICE_FILE)[:300]}: {exc}") from None


def office_choices(folder: str, start_dir: Path) -> dict[str, Any]:
    """What the office form offers for a folder: its desks and advisors, or why there are none.

    Each desk that runs on its own comes with what it runs on, and with why
    it cannot run here when it cannot. An advisor that cannot run here is
    named with why.
    """
    from metabot.kits import kit_problem
    from metabot.office import cwd_link_problem, desk_problem, desk_runs_on
    from metabot.roster import roster_entries

    try:
        office = office_in(folder, start_dir)
        roster = roster_entries()
    except ValueError as exc:
        return {"missing": str(exc)}
    desks, advisors = [], []
    for name, desk in office.desks.items():
        problem = (
            desk_problem(desk, roster)
            or cwd_link_problem(office, desk)
            or kit_problem(office, desk)
        )
        if desk.is_advisor:
            advisors.append((name, problem))
        else:
            desks.append((name, desk_runs_on(desk, roster), problem))
    return {
        "office": office.name,
        "root": str(office.root),
        "desks": desks,
        "advisors": [name for name, _ in advisors],
        "advisors_blocked": [f"{name}: {problem}" for name, problem in advisors if problem],
        "checks": bool(office.checks()),
    }


def plan_office(values: dict[str, str], desks: list[str], start_dir: Path) -> dict[str, Any]:
    """What an office form asks to run, as ``office run`` would run it; ValueError if it cannot."""
    from metabot.office import OfficeError
    from metabot.office_run import check_start
    from metabot.roster import roster_entries

    office = office_in(values["folder"], start_dir)
    _task(values["prompt"])
    mode = values["mode"]
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode[:40]!r}; choose relay or fan")
    if not desks:
        raise ValueError("choose at least one desk to run")
    unknown = next((name for name in desks if name not in office.desks), None)
    if unknown is not None:
        raise ValueError(f"no desk named {unknown[:40]!r}")
    roster = roster_entries()
    checks = values["checks"] == "1"
    advisors = mode == "relay" and values["advisors"] == "1"
    try:
        start = check_start(
            office, values["prompt"], desks=desks, roster=roster, checks=checks, advisors=advisors
        )
    except OfficeError as exc:
        raise ValueError(str(exc)) from None
    return {
        "office": office,
        "task": values["prompt"],
        "mode": mode,
        "desks": start.names,
        "roster": roster,
        "checks": checks,
        "advisors": advisors,
    }


def make_app(
    guard: Guard,
    *,
    start_dir: Path,
    make_engine: Callable[[str], Any] | None = None,
    ask_timeout_s: float = 600.0,
) -> ASGIApp:
    """The UI for one start: its routes, behind ``guard``. Its runs stop when it stops."""
    broker = Broker(ask_timeout_s)

    def started() -> list[Any]:
        return sorted(broker.runs.values(), key=lambda r: r.run_id, reverse=True)

    def chat_page(
        request: Request, values: dict[str, str], error: str | None = None, status: int = 200
    ) -> HTMLResponse:
        from metabot.engines import ENGINES, available
        from metabot.roster import ABOUT, ROLES

        found = available()
        engines = [
            (n, f"{n} ({'installed' if found.get(n) else 'not found'})" if n in found else n)
            for n in ENGINES
        ]
        page = pages.chat_page(
            nonce_of(request),
            guard.csrf,
            values=values,
            engines=engines,
            roles=[(r, f"{r}: {ABOUT[r]}") for r in ROLES],
            started=started(),
            error=error,
        )
        return HTMLResponse(page, status_code=status)

    async def chat(request: Request) -> HTMLResponse | RedirectResponse:
        if request.method != "POST":
            return chat_page(request, {"folder": str(start_dir)})
        from metabot.commands import ask_once
        from metabot.paths import new_run_id

        form = await checked_form(request, guard)
        values = {k: v if isinstance(v := form.get(k), str) else "" for k in FIELDS}
        try:
            plan = await run_in_threadpool(plan_run, values, start_dir)
        except ValueError as exc:
            return chat_page(request, values, error=str(exc), status=400)
        run_id = new_run_id()
        live = broker.open(
            run_id,
            engine=plan["engine_name"],
            model=plan["model"],
            cwd=str(plan["cwd"]),
            prompt=plan["prompt"],
        )
        engine = make_engine(plan["engine_name"]) if make_engine is not None else None
        approver = WebApprover(broker, run_id)
        coro = ask_once(**plan, approver=approver, trace=live.trace, run_id=run_id, engine=engine)
        broker.start(run_id, coro)
        return RedirectResponse(f"/live/{run_id}", status_code=303)

    async def office_form(
        request: Request,
        values: dict[str, str],
        chosen: list[str] | None = None,
        error: str | None = None,
        status: int = 200,
    ) -> HTMLResponse:
        found = await run_in_threadpool(office_choices, values["folder"], start_dir)
        page = pages.office_form_page(
            nonce_of(request),
            guard.csrf,
            values=values,
            found=found,
            chosen=chosen,
            started=started(),
            error=error,
        )
        return HTMLResponse(page, status_code=status)

    async def office_run(request: Request) -> HTMLResponse | RedirectResponse:
        from metabot.office import find_office

        if request.method != "POST":
            folder = request.query_params.get("folder")
            if folder is None:  # the office here or above, as office run finds it
                folder = str(await run_in_threadpool(find_office, start_dir) or start_dir)
            return await office_form(request, {"folder": folder})
        from metabot.office_run import run_fan, run_relay
        from metabot.paths import new_run_id

        form = await checked_form(request, guard)
        values = {k: v if isinstance(v := form.get(k), str) else "" for k in OFFICE_FIELDS}
        desks = [name for name in form.getlist("desk") if isinstance(name, str)]
        try:
            plan = await run_in_threadpool(plan_office, values, desks, start_dir)
        except ValueError as exc:
            return await office_form(request, values, desks, error=str(exc), status=400)
        office, relay = plan["office"], plan["mode"] == "relay"
        office_run_id = new_run_id()
        live = broker.open(
            office_run_id,
            cwd=str(office.root),
            prompt=plan["task"],
            office=office.name,
            mode=plan["mode"],
            desks=plan["desks"],
        )

        def on_desk(row: Any) -> None:
            runs_on = f"{row.engine}/{row.model}" if row.model else row.engine
            live.trace.note(f"[metabot] desk {row.label}: {runs_on}")

        coro = (run_relay if relay else run_fan)(
            office,
            plan["task"],
            approver=WebApprover(broker, office_run_id),
            desks=plan["desks"],
            roster=plan["roster"],
            on_event=live.trace,
            on_desk=on_desk,
            make_engine=make_engine,
            checks=plan["checks"],
            office_run_id=office_run_id,
            **({"advisors": plan["advisors"]} if relay else {}),
        )
        broker.start(office_run_id, coro)
        return RedirectResponse(f"/live/{office_run_id}", status_code=303)

    async def live(request: Request) -> HTMLResponse | RedirectResponse:
        run_id = request.path_params["run_id"]
        if not data.is_safe_id(run_id):
            return _message(request, 400, "That is not a run id.")
        found = broker.runs.get(run_id)
        if found is None:
            if await run_in_threadpool(data.run_view, run_id) is not None:
                return RedirectResponse(f"/runs/{run_id}", status_code=303)
            if await run_in_threadpool(data.office_view, run_id) is not None:
                return RedirectResponse(f"/offices/{run_id}", status_code=303)
            return _message(request, 404, f"No run {run_id} was started here.")
        # the run as it stood before its record is read: a run that says done
        # was saved before this read, so its pick list is in what the read finds,
        # and a desk that asks was saved before it asked, so its row is there too
        seen = copy.copy(found)
        asks = broker.pending(run_id)
        state, office = None, None
        if seen.office is not None:  # its record says how each desk is doing
            office = await run_in_threadpool(data.office_view, run_id)
        elif seen.result is not None:
            state = await run_in_threadpool(data.receipt_state, run_id)
        page = pages.live_page(seen, asks, state, nonce_of(request), guard.csrf, office=office)
        return HTMLResponse(page)

    async def answer(request: Request) -> HTMLResponse | RedirectResponse:
        form = await checked_form(request, guard)
        ask_id = request.path_params["ask_id"]
        known = broker.asks.get(ask_id)
        back = f"/live/{known.run_id}" if known is not None else None
        word = form.get("word")
        try:
            ask = broker.answer(
                ask_id, form.get("allow") == "1", word if isinstance(word, str) else ""
            )
        except KeyError:
            return _message(request, 404, "No call is waiting under that name here.")
        except AskAnswered as exc:
            return _message(request, 409, f"This call was {exc} already; that answer stands.", back)
        except AskExpired:
            text = "This call is no longer waiting: its time ran out, or its run ended."
            return _message(request, 410, text, back)
        except ValueError as exc:
            return _message(request, 400, f"Not allowed: {exc}.", back)
        return RedirectResponse(f"/live/{ask.run_id}", status_code=303)

    async def stop(request: Request) -> HTMLResponse | RedirectResponse:
        await checked_form(request, guard)
        run_id = request.path_params["run_id"]
        try:
            broker.stop(run_id)
        except KeyError:
            return _message(request, 404, "No run under that name was started here.")
        return RedirectResponse(f"/live/{run_id}", status_code=303)

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await broker.close()  # every pending ask declined, every run sealed

    routes = [
        Route("/", runs),
        Route("/runs/{run_id}", run),
        Route("/offices/{office_run_id}", office),
        Route("/connections", connections),
        Route("/routines", routines),
        Route("/chat", chat, methods=["GET", "POST"]),
        Route("/office", office_run, methods=["GET", "POST"]),
        Route("/live/{run_id}", live),
        Route("/live/{run_id}/stop", stop, methods=["POST"]),
        Route("/asks/{ask_id}", answer, methods=["POST"]),
    ]
    return guarded(Starlette(routes=routes, lifespan=lifespan), guard)
