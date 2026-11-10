"""``metabot ui``'s Setup (U1): seven screens, one action each, from a first start to a
first receipt.

Welcome, the signing key, an engine, a check, a login (which can be skipped), a first
ask, and its receipt. The web writes no settings (PD-9): the key is made as any run
makes it, an engine picked here goes with this setup's ask and is never saved, and a
login is added in a terminal, with the commands the screen shows. Nothing an engine
signs in with is opened (I2): an engine is looked for on PATH and asked its version, a
login's folder is looked at with ``stat``, and an endpoint's key is said to be set or
not, never shown.
"""

from __future__ import annotations

import os
import shlex
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse
from starlette.routing import Route

from metabot import web_data as data
from metabot.web_guard import Guard, checked_form, nonce_of
from metabot.web_pages import _error, _hidden, _pre, _state, clip, e, layout, link, message_page
from metabot.web_runs import Broker, WebApprover

STEPS = 7
WELCOME = "Run your work on the plan you already have."
WELCOME_SUB = "Every run ends in a receipt you can check offline."
KEY_TITLE = "Make your signing key."
KEY_BODY = "It signs your receipts. If a receipt is changed, it reads INVALID."
ENGINE_TITLE = "Which engine runs your work?"
ENGINE_BODY = "Pick one for this setup. You can change it for any run."  # the web saves no default
NOT_SET_UP = "Not set up: its command is not on PATH here, or no local model server answers."
NO_ENGINE = "No engine is ready yet. Set one up, then check again."
CHECK_TITLE = "Check the setup."
CHECK_BODY = (  # HTML: the command is a code span
    "MetaBot looks at your signing key, your engines, your endpoints and your logins, as "
    "<code>metabot doctor</code> does. It opens no sign-in."
)
SIGN_IN_NOTE = (
    "MetaBot never opens an engine's sign-in. If a run says the engine is not signed in, "
    "sign in with its own command, then check again."
)
LOGIN_TITLE = "Add one of your own accounts, kept apart."
LOGIN_BODY = (
    "For example, work and personal. Each login has its own folder, and a run uses one login. "
    "You can also use an API key."
)
ASK_TITLE = "Ask about your work."
ASK_LABEL = "What should MetaBot look at?"
ASK_LINE = "A call MetaBot classes destructive waits for a person to approve it."
APPROVALS = (  # what the form sends, its words, and the flags of metabot ask it runs as
    ("read_only", "Read only", "the engine reads and changes nothing.", True, False),
    ("ask", "Ask before writing", "each write waits for a yes. This is the default.", False, False),
    (
        "allow_write",
        "Allow writes",
        "writes run without asking, and an allowed script runs whole. "
        "A destructive call still waits.",
        False,
        True,
    ),
)
NO_APPROVAL = "choose read only, ask before writing or allow writes"
APPROVAL_FIELDS = ("prompt", "approval", "engine")
RECEIPT_TITLE = "Done. Here is your receipt."
CHANGED = "This receipt reads INVALID. It was changed after the run."
NEVER_BEGAN = "This run never began, so nothing was signed."
# a check row's state: its colour and its word
STATES = {
    "ready": ("ok", "Ready"),
    "step": ("warn", "Needs a step"),
    "absent": ("none", "Not set up"),
    "unchecked": ("none", "Not checked"),
}


# ── what setup looks at ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class Key:
    """The signing key: where it is kept, and what it signs as, or why it cannot sign."""

    path: str
    there: bool
    signer: str | None = None
    problem: str | None = None


def key_here() -> Key:
    """The key, looked at as ``metabot doctor`` looks at it. It never makes one."""
    from emerge.record import key_did, load_signing_key, resolve_key_path

    from metabot.receipts import key_problem

    path = resolve_key_path()
    if not os.path.lexists(path):
        return Key(str(path), False)
    problem = key_problem(path)
    if problem is None:
        try:
            return Key(str(path), True, signer=key_did(load_signing_key(path)))
        except (ValueError, OSError) as exc:  # changed between the two reads
            problem = str(exc)
    return Key(str(path), True, problem=problem)


def make_key() -> str:
    """Make the signing key, as a run makes it on first use; what it signs as.

    A key that is there is kept, and one that does not read is never replaced: KeyFileError.
    """
    from metabot.receipts import ensure_key

    return ensure_key()[1]


def first_start() -> bool:
    """Whether MetaBot was never set up here: no signing key, and no settings file."""
    from emerge.record import resolve_key_path

    from metabot.paths import metabot_home

    there = os.path.lexists
    return not there(resolve_key_path()) and not there(metabot_home() / "config.json")


@dataclass(frozen=True)
class Engine:
    """A built-in engine, by the name a run takes, as setup sees it."""

    name: str
    looks_for: str  # the command looked for on PATH, or the local model server's address
    ready: bool | None  # None: a remote endpoint, which is never contacted here
    billing: str  # how its runs are paid for, in plain words
    found: str | None = None  # where its command is


def _billing(signs_in: Sequence[str]) -> str:
    """How a CLI engine's runs are paid for, said from the names of the variables alone."""
    if not signs_in:
        return "your plan"
    keyed = any(not {"KEY", "TOKEN"}.isdisjoint(name.split("_")) for name in signs_in)
    verb = "is" if len(signs_in) == 1 else "are"
    return f"{'API key' if keyed else 'another sign-in'}: {', '.join(signs_in)} {verb} set here"


def engines_here() -> list[Engine]:
    """Each built-in engine: its command on PATH, or a local model server that answers.

    ConfigError when the settings do not read.
    """
    from metabot.config import load_config
    from metabot.connections import LOCAL_HOSTS
    from metabot.engines import BINARIES, available, get_engine
    from metabot.roster import local_status

    cfg = load_config()
    found = available()
    rows = []
    for name, binary in BINARIES.items():
        path = found.get(name)
        signs_in = get_engine(name).sign_in_present()  # by name: no value is read out
        rows.append(Engine(name, binary, path is not None, _billing(signs_in), path))
    url = cfg.openai_base_url
    if urlparse(url).hostname in LOCAL_HOSTS:
        rows.append(Engine("openai", url, local_status(url) == "reachable", "local"))
    else:
        rows.append(Engine("openai", url, None, "API key"))
    return rows


def engine_for(said: object) -> str:
    """The engine a screen goes on with: the one it was given, when a run takes it, else the
    one a run takes when none is named."""
    from metabot.commands import default_engine
    from metabot.engines import known_engine

    if known_engine(said):
        return said
    try:
        return default_engine()
    except ValueError:  # settings that do not read: the check says why
        return "openai"


@dataclass(frozen=True)
class Row:
    """One line of the check: what was looked at, its state, and what was found or the step."""

    what: str
    state: str  # a key of STATES
    line: str
    code: bool = False  # ``what`` is a command
    fix: tuple[str, str] | None = None  # a screen that does the step: its address and words


def _engine_row(fact: Engine, mine: bool) -> Row:
    from metabot.cli import _binary_version

    if fact.ready is None:
        said = f"{fact.looks_for}: a remote endpoint, never contacted here"
        return Row(fact.name, "unchecked", said, code=True)
    if fact.name == "openai":
        said = f"the local model server at {fact.looks_for}"
        if fact.ready:
            return Row(fact.name, "ready", f"{said} answers", code=True)
        missing, mend = f"no local model server answers at {fact.looks_for}", "start it"
    else:
        if fact.ready and fact.found is not None:
            return Row(fact.name, "ready", f"{_binary_version(fact.found)}, on PATH", code=True)
        missing, mend = f"{fact.looks_for} is not on PATH", "install it"
    if mine:
        return Row(fact.name, "step", f"{missing}: {mend}, or pick another engine", code=True)
    return Row(fact.name, "absent", missing, code=True)


def check(engine: str) -> tuple[list[Row], bool, bool]:
    """Setup's check, as ``metabot doctor`` makes it, in one list: the key, each engine, each
    endpoint's key, each login's folder and the plugins' commands. With it, whether a run
    can start on ``engine``, and whether any engine is ready at all."""
    from metabot.config import ConfigError, load_config
    from metabot.engines import missing_command
    from metabot.needs import doctor_lines
    from metabot.plugins import PluginError

    key = key_here()
    if key.signer is not None:
        rows = [Row("Signing key", "ready", f"signs as {key.signer}")]
    elif key.problem is not None:
        rows = [Row("Signing key", "step", f"{key.path} does not read: {key.problem}")]
    else:
        fix = ("/setup/key", "Make your signing key")
        rows = [Row("Signing key", "step", "not made yet:", fix=fix)]
    try:
        cfg = load_config()
        engines = engines_here()
    except ConfigError as exc:
        return [*rows, Row("Settings", "step", str(exc))], False, False
    rows += [_engine_row(fact, fact.name == engine) for fact in engines]
    runs = next((fact.ready is not False for fact in engines if fact.name == engine), None)
    if runs is None:  # a bridge: from the settings or an enabled plugin
        missing = missing_command(engine)
        said = f"a bridge: {missing} is not on PATH" if missing else "a bridge, its command on PATH"
        rows.append(Row(engine, "step" if missing else "ready", said, code=True))
        runs = missing is None
    rows += _endpoint_rows(cfg) + _login_rows(cfg)
    try:
        needs = doctor_lines()
    except PluginError as exc:  # every run that takes the plugins refuses them
        rows.append(Row("plugins", "step", f"{exc}; --no-plugins runs without them"))
    else:
        for line in needs:
            said = line.removeprefix("plugin").strip()
            rows.append(Row("plugin", "step" if ": missing" in said else "ready", said))
    ready = key.signer is not None and runs
    return rows, ready, any(fact.ready is not False for fact in engines)


def _endpoint_rows(cfg: Any) -> list[Row]:
    """Each endpoint that takes a key: whether its variable is set. No value is shown, and
    no endpoint is contacted."""
    from metabot.config import configured_keys

    held = configured_keys(cfg)
    keyed: list[tuple[str, object]] = [("openai", cfg.openai_key_env)] if cfg.openai_key_env else []
    keyed += [(n, s.get("key_env")) for n, s in cfg.endpoints.items() if isinstance(s, dict)]
    rows = []
    for name, key_env in keyed:
        what = f"endpoint {name}"
        if not isinstance(key_env, str) or not key_env:
            rows.append(Row(what, "unchecked", "takes no key, and is never contacted here"))
        elif key_env in held:
            rows.append(Row(what, "ready", f"its key variable {key_env} is set (never shown)"))
        else:
            said = f"its key variable {key_env} is not set: export {key_env}=… (never shown here)"
            rows.append(Row(what, "step", said))
    return rows


def _login_rows(cfg: Any) -> list[Row]:
    """Each login, as ``metabot login check`` says it: its folder looked at with ``stat``,
    and its sign-in file looked for, never opened."""
    from metabot.config import login_of
    from metabot.logins import SIGN_IN_FILES, run_problems, sign_in_line

    rows = []
    for name in cfg.logins:
        login = login_of(cfg, name)
        if login is None:
            continue
        what = f"login {name}"
        problems = run_problems(login.home)
        if problems:
            rows.append(Row(what, "step", "; ".join(problems)))
        elif os.path.lexists(os.path.join(login.home, SIGN_IN_FILES[login.engine])):
            rows.append(Row(what, "ready", "a sign-in file is there (not opened)"))
        else:
            said = "the folder holds no sign-in file yet; sign in once, in a terminal: "
            rows.append(Row(what, "step", said + sign_in_line(login.engine, login.home)))
    return rows


def logins_here() -> list[tuple[str, str]]:
    """Each login the settings name, with its engine; none when they do not read."""
    from metabot.config import ConfigError, load_config

    try:
        logins = load_config().logins
    except ConfigError:
        return []
    return [(name, str(s.get("engine"))) for name, s in logins.items() if isinstance(s, dict)]


def note_sections(note: str) -> tuple[list[tuple[str, list[str]]], list[str]]:
    """What ``metabot login add`` prints after its first line: each heading with its points,
    a point's next lines joined to it, and the links."""
    sections: list[tuple[str, list[str]]] = []
    links: list[str] = []
    for line in note.splitlines()[1:]:
        if line.startswith("  - ") and sections:
            sections[-1][1].append(line[4:])
        elif line.startswith("    ") and sections and sections[-1][1]:
            sections[-1][1][-1] += " " + line.strip()
        elif line.startswith("  "):
            links.append(line.strip())
        elif line.strip():
            sections.append((line.rstrip(":"), []))
    return sections, links


def plan_ask(values: dict[str, str], start_dir: Path) -> dict[str, Any]:
    """What the setup ask runs, as the chat runs a task, in the folder the UI started in,
    with the approval's flags; ValueError naming what is wrong."""
    from metabot.web_app import plan_run

    flags = {value: (ro, aw) for value, _label, _said, ro, aw in APPROVALS}
    if values["approval"] not in flags:
        raise ValueError(NO_APPROVAL)
    read_only, allow_write = flags[values["approval"]]
    form = {"prompt": values["prompt"], "engine": values["engine"], "role": "", "model": ""}
    plan = plan_run({**form, "folder": ""}, start_dir)
    return {**plan, "read_only": read_only, "allow_write": allow_write}


# ── the screens ─────────────────────────────────────────────────────────────


def _screen(step: int, title: str, body: str, nonce: str) -> str:
    return layout(title, body, nonce, kicker=f"Step {step} of {STEPS}", narrow=True)


def _go(href: str, text: str, *, primary: bool = True) -> str:
    """A link that is the screen's action."""
    cls = "button primary" if primary else "button"
    return f'<a class="{cls}" href="{e(href)}">{e(text)}</a>'


def _actions(*items: str) -> str:
    return f'<div class="actions">{"".join(items)}</div>'


def _q(engine: str) -> str:
    return quote(engine, safe="")


def welcome_page(nonce: str) -> str:
    body = f'<p class="lead">{e(WELCOME_SUB)}</p>' + _actions(_go("/setup/key", "Set up"))
    return _screen(1, WELCOME, body, nonce)


def key_page(nonce: str, csrf: str, key: Key, error: str | None = None) -> str:
    kept = f'<p class="muted">It is kept in <code>{e(key.path)}</code>, for you alone to read.</p>'
    body = _error(error) + f'<p class="lead">{e(KEY_BODY)}</p>'
    if key.signer is not None:
        body += (
            f'<p><span class="ok">Ready</span> Your receipts are signed as '
            f"<code>{e(key.signer)}</code>.</p>{kept}" + _actions(_go("/setup/engine", "Continue"))
        )
    elif key.problem is not None:
        body += (
            f'<p><span class="bad">Does not read</span> <code>{e(key.path)}</code>: '
            f"{e(clip(key.problem, 300))}</p>"
            '<p class="muted">MetaBot never replaces a key that is there: the receipts it '
            "signed name it as their signer.</p>" + _actions(_go("/setup/key", "Check again"))
        )
    else:
        create = '<button class="primary">Create key</button>'
        body += f'<form method="post" action="/setup/key">{_hidden(csrf)}{_actions(create)}</form>'
        body += kept
    return _screen(2, KEY_TITLE, body, nonce)


def _engine_choice(fact: Engine, on: str | None) -> str:
    cls, word = ("ok", "ready") if fact.ready else ("none", "not set up")
    if fact.ready is None:
        cls, word = "none", "not checked"
    radio = (
        f'<input type="radio" name="engine" value="{e(fact.name)}"'
        + (" checked" if fact.name == on else "")
        + (" disabled" if fact.ready is False else "")
        + ">"
    )
    said = f'<span class="{cls}">{word}</span> <span class="muted">{e(fact.billing)}</span>'
    return f'<label class="choice">{radio}<span><code>{e(fact.name)}</code> {said}</span></label>'


def engine_page(
    nonce: str, engines: Sequence[Engine], chosen: str, error: str | None = None
) -> str:
    lead = f'<p class="lead">{e(ENGINE_BODY)}</p>'
    check = _actions(_go("/setup/check?run=1", "Run check"))
    if error is not None:
        return _screen(3, ENGINE_TITLE, lead + _error(error) + check, nonce)
    usable = [fact.name for fact in engines if fact.ready is not False]
    on = chosen if chosen in usable else next(iter(usable), None)
    rows = "".join(_engine_choice(fact, on) for fact in engines)
    if on is None:
        body = lead + rows + f'<p class="warn">{e(NO_ENGINE)}</p>' + check
    else:
        use = '<button class="primary">Use this engine</button>'
        body = (
            lead
            + f'<form method="get" action="/setup/check">{rows}{_actions(use)}</form>'
            + f'<p class="muted">{e(NOT_SET_UP)}</p>'
        )
    return _screen(3, ENGINE_TITLE, body, nonce)


def _check_row(row: Row) -> str:
    cls, word = STATES[row.state]
    what = f"<code>{e(row.what)}</code>" if row.code else e(row.what)
    fix = f' <a href="{e(row.fix[0])}">{e(row.fix[1])}</a>' if row.fix else ""
    line = f"{e(clip(row.line, 600))}{fix}"
    return f'<tr><th>{what}</th><td><span class="{cls}">{word}</span></td><td>{line}</td></tr>'


def check_page(
    nonce: str,
    engine: str,
    result: tuple[list[Row], bool, bool] | None,
    default: str | None = None,
) -> str:
    """The check: before it runs, its button; after, its rows and the next step.
    ``default`` is the engine a run takes when none is named."""
    run = (
        '<form method="get" action="/setup/check">'
        f'<input type="hidden" name="engine" value="{e(engine)}">'
        '<input type="hidden" name="run" value="1">'
        + _actions('<button class="primary">Run check</button>')
        + "</form>"
    )
    if result is None:
        return _screen(4, CHECK_TITLE, f'<p class="lead">{CHECK_BODY}</p>{run}', nonce)
    rows, ready, any_ready = result
    if ready:
        verdict = (
            f'<p><span class="ok">Ready</span> Runs can start on <code>{e(engine)}</code>.</p>'
        )
        then = _actions(_go(f"/setup/login?engine={_q(engine)}", "Continue"))
    else:
        said = "Do the steps below, then check again." if any_ready else NO_ENGINE
        verdict = f'<p><span class="warn">Needs a step</span> {e(said)}</p>'
        then = run
    table = '<table class="facts">' + "".join(map(_check_row, rows)) + "</table>"
    note = f'<p class="muted">{e(SIGN_IN_NOTE)}</p>'
    if ready and default is not None and engine != default:
        note += (
            f'<p class="muted">To make <code>{e(engine)}</code> the engine of every run that '
            f"names none, in a terminal: <code>metabot init --engine {e(engine)}</code></p>"
        )
    return _screen(4, CHECK_TITLE, verdict + table + note + then, nonce)


def login_page(nonce: str, engine: str, logins: Sequence[tuple[str, str]], folder: Path) -> str:
    """The logins: how to add one in a terminal, and the note ``metabot login add`` prints.
    ``folder`` is the person's home folder, where the example login's folder goes."""
    from metabot.config import LOGIN_ENGINES
    from metabot.logins import VENDOR_NOTE, sign_in_line

    body = f'<p class="lead">{e(LOGIN_BODY)}</p>'
    if engine in LOGIN_ENGINES:
        home = str(folder / f".{engine}-work")
        lines = (
            f"mkdir -m 700 {shlex.quote(home)}",
            f"metabot login add work --engine {engine} --home {shlex.quote(home)}",
            sign_in_line(engine, home),
        )
        body += "<h2>Add a login, in a terminal</h2>" + _pre("\n".join(lines))
    else:
        takes = " and ".join(f"<code>{e(name)}</code>" for name in LOGIN_ENGINES)
        said = f"<code>{e(engine)}</code> takes no login: logins are for {takes}."
        body += f'<p class="muted">{said}</p>'
    if logins:
        named = ", ".join(f"<code>{e(name)}</code> ({e(kind)})" for name, kind in logins)
        body += f"<p>Your logins: {named}. See them with <code>metabot login check</code>.</p>"
    sections, links = note_sections(VENDOR_NOTE)
    for heading, points in sections:
        if points:
            body += f"<h2>{e(heading)}</h2><ul>" + "".join(f"<li>{e(p)}</li>" for p in points)
            body += "</ul>"
        else:
            body += f"<p>{e(heading)}:</p>"
    body += "<ul>" + "".join(f"<li><code>{e(url)}</code></li>" for url in links) + "</ul>"
    then = "Continue" if logins else "Skip for now"
    body += _actions(_go(f"/setup/ask?engine={_q(engine)}", then))
    return _screen(5, LOGIN_TITLE, body, nonce)


def ask_page(
    nonce: str,
    csrf: str,
    *,
    engine: str,
    folder: str,
    values: dict[str, str] | None = None,
    error: str | None = None,
) -> str:
    values = values or {}
    chosen = values.get("approval", "ask")
    choices = "".join(
        f'<label class="choice"><input type="radio" name="approval" value="{value}"'
        f"{' checked' if value == chosen else ''}><span><strong>{e(label)}</strong>: "
        f"{e(said)}</span></label>"
        for value, label, said, _ro, _aw in APPROVALS
    )
    form = (
        f'<form method="post" action="/setup/ask">{_hidden(csrf)}'
        f'<input type="hidden" name="engine" value="{e(engine)}">'
        f'<p><label for="setup-prompt">{e(ASK_LABEL)}</label>'
        f'<textarea id="setup-prompt" name="prompt" rows="4">{e(values.get("prompt", ""))}'
        "</textarea></p>"
        f"<fieldset><legend>Approval</legend>{choices}</fieldset>"
        f'<p class="muted">{e(ASK_LINE)}</p>'
        f'<p class="muted">It runs on <code>{e(engine)}</code>, in <code>{e(folder)}</code>.</p>'
        + _actions('<button class="primary">Run</button>')
        + "</form>"
    )
    return _screen(6, ASK_TITLE, _error(error) + form, nonce)


def receipt_page(nonce: str, run_id: str, state: str, status: str | None, engine: str) -> str:
    """A setup run's receipt, as it reads now, and the command that checks it offline."""
    again = _go(f"/setup/ask?engine={_q(engine)}", "Run another", primary=False)
    if state == "no receipt":
        check = _go(f"/setup/check?engine={_q(engine)}&run=1", "Run check")
        body = f"<p>{_state(state)} {e(NEVER_BEGAN)}</p>" + _actions(check, again)
        return _screen(7, "No receipt for this run", body, nonce)
    facts = f"<tr><th>receipt</th><td>{link('/runs/', run_id)} {_state(state)}</td></tr>"
    if status:
        facts += f"<tr><th>status</th><td>{e(clip(status, 60))}</td></tr>"
    body = (
        f'<table class="facts">{facts}</table>'
        + (f'<p class="bad">{e(CHANGED)}</p>' if state == "INVALID" else "")
        + f"<p>Check it any time, offline: <code>metabot receipts verify {e(run_id)}</code></p>"
        + _actions(_go("/", "Open Home"), again)
    )
    return _screen(7, RECEIPT_TITLE, body, nonce)


# ── the routes ──────────────────────────────────────────────────────────────


def setup_routes(
    guard: Guard,
    broker: Broker,
    start_dir: Path,
    make_engine: Callable[[str], Any] | None,
) -> list[Route]:
    """Setup's screens, for one start of the UI: its asks run as the chat's do."""

    def _engine(request: Request) -> str:
        return engine_for(request.query_params.get("engine"))

    def welcome(request: Request) -> HTMLResponse:
        return HTMLResponse(welcome_page(nonce_of(request)))

    async def key(request: Request) -> HTMLResponse | RedirectResponse:
        if request.method == "POST":
            await checked_form(request, guard)
            try:
                await run_in_threadpool(make_key)
            except ValueError as exc:  # a key that does not read, or cannot be made
                found = await run_in_threadpool(key_here)
                page = key_page(nonce_of(request), guard.csrf, found, error=str(exc))
                return HTMLResponse(page, status_code=400)
            return RedirectResponse("/setup/key", status_code=303)
        found = await run_in_threadpool(key_here)
        return HTMLResponse(key_page(nonce_of(request), guard.csrf, found))

    def engine(request: Request) -> HTMLResponse:
        from metabot.config import ConfigError

        chosen = _engine(request)
        try:
            return HTMLResponse(engine_page(nonce_of(request), engines_here(), chosen))
        except ConfigError as exc:
            return HTMLResponse(engine_page(nonce_of(request), [], chosen, error=str(exc)))

    def checked(request: Request) -> HTMLResponse:
        chosen = _engine(request)
        result = check(chosen) if request.query_params.get("run") == "1" else None
        default = engine_for(None) if result is not None else None
        return HTMLResponse(check_page(nonce_of(request), chosen, result, default))

    def logins(request: Request) -> HTMLResponse:
        from metabot.sandbox import user_home

        page = login_page(nonce_of(request), _engine(request), logins_here(), user_home())
        return HTMLResponse(page)

    async def ask(request: Request) -> HTMLResponse | RedirectResponse:
        if request.method != "POST":
            chosen = await run_in_threadpool(_engine, request)
            page = ask_page(nonce_of(request), guard.csrf, engine=chosen, folder=str(start_dir))
            return HTMLResponse(page)
        from metabot.commands import ask_once
        from metabot.paths import new_run_id

        form = await checked_form(request, guard)
        values = {k: v if isinstance(v := form.get(k), str) else "" for k in APPROVAL_FIELDS}
        try:
            plan = await run_in_threadpool(plan_ask, values, start_dir)
        except ValueError as exc:
            chosen = await run_in_threadpool(engine_for, values["engine"])
            page = ask_page(
                nonce_of(request), guard.csrf, engine=chosen, folder=str(start_dir),
                values=values, error=str(exc),
            )  # fmt: skip
            return HTMLResponse(page, status_code=400)
        run_id = new_run_id()
        name = plan["engine_name"]
        live = broker.open(
            run_id, engine=name, model=plan["model"], cwd=str(plan["cwd"]), prompt=plan["prompt"]
        )
        live.then = f"/setup/receipt/{run_id}?engine={_q(name)}"  # where its page goes on to
        made = make_engine(name) if make_engine is not None else None
        approver = WebApprover(broker, run_id)
        coro = ask_once(**plan, approver=approver, trace=live.trace, run_id=run_id, engine=made)
        broker.start(run_id, coro)
        return RedirectResponse(f"/live/{run_id}", status_code=303)

    async def receipt(request: Request) -> HTMLResponse | RedirectResponse:
        run_id = request.path_params["run_id"]
        if not data.is_safe_id(run_id):
            page = message_page("Not done", "That is not a run id.", nonce_of(request))
            return HTMLResponse(page, status_code=400)
        chosen = await run_in_threadpool(_engine, request)
        view = await run_in_threadpool(data.run_view, run_id)
        if view is None:
            live = broker.runs.get(run_id)
            if live is not None and live.state == "running":  # not sealed yet: its page says
                return RedirectResponse(f"/live/{run_id}", status_code=303)
            if live is None:
                said = f"There is no run {run_id} in this MetaBot home."
                return HTMLResponse(message_page("Not found", said, nonce_of(request)), 404)
            return HTMLResponse(receipt_page(nonce_of(request), run_id, "no receipt", None, chosen))
        status = view["meta"].get("status")
        status = status if isinstance(status, str) else None
        page = receipt_page(nonce_of(request), run_id, view["state"], status, chosen)
        return HTMLResponse(page)

    return [
        Route("/setup", welcome),
        Route("/setup/key", key, methods=["GET", "POST"]),
        Route("/setup/engine", engine),
        Route("/setup/check", checked),
        Route("/setup/login", logins),
        Route("/setup/ask", ask, methods=["GET", "POST"]),
        Route("/setup/receipt/{run_id}", receipt),
    ]
