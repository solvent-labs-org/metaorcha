"""The HTML of ``metabot ui``'s pages.

Every value from a file, a run or a model goes through ``e``, so it shows as
text, with control characters and the marks that reorder text escaped. The
pages hold no script; the one style block carries the response's nonce,
which the Content-Security-Policy names.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from html import escape
from pathlib import Path
from typing import Any

from metabot.approvals import terminal_safe
from metabot.web_data import is_safe_id

STYLE = """
body { font: 15px/1.5 system-ui, sans-serif; margin: 0; color: #1d1d1f; background: #fbfbfa; }
header { padding: 12px 24px; border-bottom: 1px solid #e5e5e2; }
header b { font-weight: 600; margin-right: 24px; }
nav a { margin-right: 16px; color: #2f4f9e; text-decoration: none; }
main { padding: 8px 24px 32px; max-width: 1180px; }
h1 { font-size: 20px; font-weight: 600; }
h2 { font-size: 16px; font-weight: 600; margin-top: 24px; }
table { border-collapse: collapse; width: 100%; }
th, td { text-align: left; padding: 4px 10px 4px 0; border-bottom: 1px solid #ecece8;
         vertical-align: top; }
th { font-weight: 600; color: #4a4a46; }
table.facts th { width: 8em; }
a { color: #2f4f9e; }
code, pre { font: 13px/1.45 ui-monospace, monospace; }
pre { white-space: pre-wrap; word-break: break-word; background: #f3f3ef; padding: 10px; }
summary { cursor: pointer; color: #4a4a46; }
.bad { color: #b3261e; } .ok { color: #1b6e3a; } .muted { color: #6b6b66; }
.indent { padding-left: 22px; }
footer { padding: 0 24px 24px; }
textarea, input, select, button { font: inherit; }
textarea { width: 100%; box-sizing: border-box; }
form.ask { border: 1px solid #e5e5e2; padding: 4px 12px 8px; margin: 12px 0; background: #fff; }
"""

NAV: Sequence[tuple[str, str]] = (
    ("/", "Runs"),
    ("/chat", "Chat"),
    ("/office", "Office"),
    ("/connections", "Connections"),
    ("/routines", "Routines"),
)
FOOT = "This page runs on this machine only, until metabot ui stops."
CLIP = 160  # characters of a value in a table cell
TEXT_LIMIT = 20_000  # characters of a prompt, an answer, or a step's arguments or output
NO_RUNS = "No runs yet: metabot ask, chat and office run make them."


def e(value: Any) -> str:
    """A value as HTML text: every character that means something in HTML escaped."""
    return escape(terminal_safe(str(value)), quote=True)


def clip(value: Any, limit: int = CLIP) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= limit else text[:limit] + "…"


def link(prefix: str, value: Any, text: Any = None) -> str:
    """A link to a run's or an office run's page; plain text for an id that is not safe."""
    shown = e(clip(value if text is None else text))
    return f'<a href="{prefix}{e(value)}">{shown}</a>' if is_safe_id(value) else shown


def layout(title: str, body: str, nonce: str, *, refresh: int | None = None) -> str:
    """A whole page. ``refresh`` reloads it every that many seconds."""
    reload = f'<meta http-equiv="refresh" content="{int(refresh)}">' if refresh else ""
    nav = "".join(f'<a href="{e(href)}">{e(label)}</a>' for href, label in NAV)
    return (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        f"<title>{e(clip(title, 80))} · MetaBot</title>{reload}"
        f'<style nonce="{e(nonce)}">{STYLE}</style></head>'
        f"<body><header><b>MetaBot</b><nav>{nav}</nav></header>"
        f"<main><h1>{e(clip(title, 200))}</h1>{body}</main>"
        f'<footer class="muted">{e(FOOT)}</footer></body></html>\n'
    )


def message_page(title: str, text: str, nonce: str, *, back: str | None = None) -> str:
    more = f'<p><a href="{e(back)}">Back to the run</a></p>' if back else ""
    return layout(title, f"<p>{e(text)}</p>{more}", nonce)


# ── pieces ──────────────────────────────────────────────────────────────────


def _state(state: str) -> str:
    cls = "ok" if state == "valid" else "muted" if state == "no receipt" else "bad"
    return f'<span class="{cls}">{e(state)}</span>'


def _error(error: Any) -> str:
    return f'<p class="bad">{e(error)}</p>' if error else ""


def _td(html: str, cls: str | None = None) -> str:
    return f'<td class="{cls}">{html}</td>' if cls else f"<td>{html}</td>"


def _tr(*cells: str) -> str:
    return "<tr>" + "".join(cells) + "</tr>"


def _table(head: Sequence[str], rows: Iterable[str], empty: str) -> str:
    body = "".join(rows)
    if not body:
        return f'<p class="muted">{e(empty)}</p>'
    th = "".join(f"<th>{e(h)}</th>" for h in head)
    return f"<table><tr>{th}</tr>{body}</table>"


def _facts(pairs: Iterable[tuple[str, str]]) -> str:
    rows = "".join(f"<tr><th>{e(k)}</th><td>{v}</td></tr>" for k, v in pairs)
    return f'<table class="facts">{rows}</table>'


def _pre(value: Any) -> str:
    return f"<pre>{e(clip(value, TEXT_LIMIT))}</pre>"


def _json(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, indent=2, default=str)
    return _pre(text)


def _runs_on(engine: Any, model: Any) -> str:
    return f"{engine}/{model}" if model else str(engine or "?")


def changed(stat: tuple[int, int, int] | None) -> str:
    if stat is None:
        return "-"
    files, added, removed = stat
    return f"{files} file{'' if files == 1 else 's'} +{added} -{removed}"


# ── runs ────────────────────────────────────────────────────────────────────


def _run_row(row: dict[str, Any]) -> str:
    office = row.get("office")
    return _tr(
        _td(link("/runs/", row.get("run_id"))),
        _td(e(clip(row.get("finished_at") or "-", 40))),
        _td(e(clip(_runs_on(row.get("engine"), row.get("model")), 60))),
        _td(e(clip(row.get("status") or "?", 40))),
        _td(e(clip(row.get("steps", "-"), 12))),
        _td(_state(str(row.get("state")))),
        _td(link("/offices/", office) if isinstance(office, str) else ""),
        _td(e(clip(row.get("prompt") or "", 120))),
    )


def runs_page(rows: list[dict[str, Any]], total: int, error: str | None, nonce: str) -> str:
    more = f"The newest {len(rows)} of {total} runs." if total > len(rows) else ""
    head = ("run", "finished", "runs on", "status", "steps", "receipt", "office run", "prompt")
    body = (
        _error(error)
        + (f'<p class="muted">{e(more)}</p>' if more else "")
        + _table(head, map(_run_row, rows), NO_RUNS)
    )
    return layout("Runs", body, nonce)


def _step_row(step: dict[str, Any]) -> str:
    what = (
        "<details><summary>arguments and output</summary>"
        f"{_json(step.get('args'))}{_json(step.get('output'))}</details>"
    )
    return _tr(
        _td(f"<code>{e(clip(step.get('call_id'), 80))}</code>"),
        _td(e(clip(step.get("tool"), 120))),
        _td('<span class="ok">ok</span>' if step.get("success") is True else "FAIL"),
        _td(e(clip(step.get("approval"), 200))),
        _td(what),
    )


def _verdict_row(verdict: dict[str, Any]) -> str:
    return _tr(*(_td(e(clip(verdict.get(key), 300))) for key in ("check", "result", "detail")))


UNSIGNED = (
    "The receipt signs the steps, the verdicts, the signer and the times. The rest of "
    "this page comes from the run's journal, which it does not sign: the prompt, the "
    "answer, the status and what the run ran on."
)


def run_page(view: dict[str, Any], nonce: str) -> str:
    meta = view["meta"]
    office = meta.get("office")
    facts = [
        ("run", e(view["run_id"])),
        ("runs on", e(clip(_runs_on(meta.get("engine"), meta.get("model")), 120))),
        ("status", e(clip(meta.get("status") or "?", 60))),
        ("started", e(clip(view.get("started_at") or "-", 40))),
        ("finished", e(clip(view.get("finished_at") or "-", 40))),
        ("receipt", f"{_state(view['state'])} <code>{e(clip(view.get('receipt') or '-'))}</code>"),
        ("signed by", f"<code>{e(clip(view.get('signer') or '-'))}</code>"),
    ]
    if isinstance(office, str):
        facts.append(("office run", link("/offices/", office)))
    more = view.get("more_steps")
    body = [
        _error(view.get("error")),
        _facts(facts),
        f'<p class="muted">{e(UNSIGNED)}</p>',
        "<h2>Prompt</h2>" + _pre(meta.get("prompt", "")),
    ]
    if meta.get("final"):
        body.append("<h2>Answer</h2>" + _pre(meta["final"]))
        if view.get("caveat"):
            body.append(f'<p class="bad">{e(view["caveat"])}</p>')
    body += [
        "<h2>Steps</h2>",
        _table(
            ("call", "tool", "result", "approval", "what it did"),
            map(_step_row, view["steps"]),
            "This run made no tool call.",
        ),
        f'<p class="muted">{e(f"{more} more steps are in the journal.")}</p>' if more else "",
        "<h2>Verdicts</h2>",
        _table(("check", "result", "detail"), map(_verdict_row, view["verdicts"]), "None."),
    ]
    return layout(f"Run {clip(view['run_id'], 80)}", "".join(body), nonce)


# ── an office run ───────────────────────────────────────────────────────────


def _desk_row(row: dict[str, Any], going: bool = False) -> str:
    ok = row.get("ok") is True
    running = going and not ok and not row.get("error")  # its office run goes on
    result = "ok" if ok else "running" if running else "error"
    if row.get("error"):
        result += f": {clip(row['error'], 300)}"
    checks = row.get("checks")
    checks_cell = "-"
    if "checks_state" in row:
        checks_cell = f"{e(clip(checks or '-', 200))} ({_state(row['checks_state'])})"
    return _tr(
        _td(e(clip(row["label"], 60)), "indent" if row["indent"] else None),
        _td(e(result), None if ok or running else "bad"),
        _td(_state(row["state"])),
        _td(e(clip(_runs_on(row.get("engine"), row.get("model")), 60))),
        _td(e(changed(row["stat"]))),
        _td(checks_cell),
        _td(link("/runs/", row.get("run_id"))),
    )


def _desks_table(view: dict[str, Any], going: bool = False) -> str:
    return _table(
        ("desk", "result", "receipt", "runs on", "changed", "checks", "run"),
        (_desk_row(row, going) for row in view["desks"]),
        "No desk ran.",
    )


def _offer(view: dict[str, Any]) -> str:
    offer = view.get("offer")
    if offer is None:
        return '<p class="muted">This record does not say which desk runs to pick from.</p>'
    if not offer:
        return '<p class="muted">Nothing to pick: no desk left work to bring in.</p>'
    lines = "\n".join(f"metabot desks pick {view['office_run_id']}/{label}" for label in offer)
    return "<p>Bring one desk's work into your checkout with:</p>" + _pre(lines)


def office_page(view: dict[str, Any], nonce: str) -> str:
    checks = view.get("checks")
    gate = ", ".join(map(str, checks)) if isinstance(checks, list) and checks else "none"
    facts = [
        ("office run", e(view["office_run_id"])),
        ("office", e(clip(view.get("office") or "?", 80))),
        ("mode", e(clip(view.get("mode") or "?", 20))),
        ("started", e(clip(view.get("started_at") or "-", 40))),
        ("finished", e(clip(view.get("finished_at") or "-", 40))),
        ("from commit", f"<code>{e(clip(view.get('base') or '-', 64))}</code>"),
        ("checks", e(clip(gate, 300))),
    ]
    body = [
        _error(view.get("error")),
        _facts(facts) if not view.get("error") else "",
        "<h2>Task</h2>" + _pre(view.get("task", "")) if not view.get("error") else "",
        "<h2>Desks</h2>",
        _desks_table(view),
    ]
    if not view.get("error"):
        body += ["<h2>Pick</h2>", _offer(view)]
    return layout(f"Office run {clip(view['office_run_id'], 80)}", "".join(body), nonce)


# ── connections ─────────────────────────────────────────────────────────────


def _connection_row(row: dict[str, Any]) -> str:
    origins = row.get("origins")
    where = "not a browser" if origins is None else ", ".join(map(str, origins)) or "nowhere"
    return _tr(
        _td(e(clip(row.get("name"), 40))),
        _td(f"<code>{e(clip(row.get('id'), 120))}</code>"),
        _td(e(clip(row.get("state"), 20))),
        _td(e(clip(row.get("transport"), 20))),
        _td(f"<code>{e(clip(row.get('runs') or '-', 300))}</code>"),
        _td(e(row.get("token"))),
        _td(e(clip(where, 600))),
    )


def connections_page(rows: list[dict[str, Any]], error: str | None, nonce: str) -> str:
    head = ("name", "id", "state", "transport", "runs", "token", "a browser may act on")
    body = _error(error) + _table(
        head, map(_connection_row, rows), "No connections. metabot connections add makes one."
    )
    return layout("Connections", body, nonce)


# ── routines ────────────────────────────────────────────────────────────────


def _firing(firing: Any) -> str:
    if firing is None:
        return "never fired"
    text = e(f"{clip(firing.state, 20)} {clip(firing.at, 40)}")
    return f"{text} {link('/runs/', firing.run_id)}" if firing.run_id else text


def _routine_row(row: dict[str, Any]) -> str:
    when = f"{clip(row.get('cron'), 60)} ({clip(row.get('tz'), 60)})"
    return _tr(
        _td(e(clip(row.get("name"), 64))),
        _td(f"<code>{e(clip(row.get('id'), 40))}</code>"),
        _td("on" if row.get("enabled") is True else "off"),
        _td(e(when)),
        _td(e(row.get("next_due"))),
        _td(e(clip(_runs_on(row.get("engine"), row.get("model")), 60))),
        _td(_firing(row.get("last"))),
    )


def _firing_row(row: dict[str, Any]) -> str:
    run_id = row.get("run_id")
    return _tr(
        _td(e(clip(row.get("routine_name"), 64))),
        _td(e(clip(row.get("due") or "asked for", 40))),
        _td(e(clip(row.get("state"), 20))),
        _td(link("/runs/", run_id) if run_id else "-"),
        _td(e(clip(row.get("reason") or "", 300))),
        _td(e(clip(row.get("at"), 40))),
    )


def routines_page(
    rows: list[dict[str, Any]], recent: list[dict[str, Any]], error: str | None, nonce: str
) -> str:
    body = (
        _error(error)
        + _table(
            ("name", "id", "on", "schedule", "next due", "runs on", "last firing"),
            map(_routine_row, rows),
            "No routines. metabot routine save makes one.",
        )
        + "<h2>Firings</h2>"
        + _table(
            ("routine", "due", "state", "run", "why", "at"),
            map(_firing_row, recent),
            "No routine has fired.",
        )
    )
    return layout("Routines", body, nonce)


# ── chat and a run going on in the page ─────────────────────────────────────

LIVE_REFRESH_S = 2  # how often a live page reloads while its run goes and asks nothing
TRACE_LIMIT = TEXT_LIMIT - 64  # leaves room for the line that says how many are not shown


def _newest(lines: Sequence[str], limit: int = TRACE_LIMIT) -> tuple[list[str], int]:
    """The newest lines that fit in ``limit`` characters, and how many older ones do not."""
    kept: list[str] = []
    size = 0
    for line in reversed(lines):
        size += len(line) + 1
        if size > limit:
            if not kept:  # one line longer than the page: its end, which is newest
                kept.append("…" + line[max(len(line) - limit + 2, 0) :])
            break
        kept.append(line)
    kept.reverse()
    return kept, len(lines) - len(kept)


def _hidden(csrf: str) -> str:
    return f'<input type="hidden" name="csrf" value="{e(csrf)}">'


def _select(name: str, options: Sequence[tuple[str, str]], chosen: str) -> str:
    items = "".join(
        f'<option value="{e(value)}"{" selected" if value == chosen else ""}>{e(text)}</option>'
        for value, text in options
    )
    return f'<select name="{e(name)}">{items}</select>'


def _started_row(live: Any) -> str:
    office = live.office is not None
    runs_on = f"office {live.office}: {live.mode}" if office else _runs_on(live.engine, live.model)
    return _tr(
        _td(link("/live/", live.run_id)),
        _td(e(live.state)),
        _td(e(clip(runs_on, 60))),
        _td(e(clip(live.prompt, 120))),
    )


def chat_page(
    nonce: str,
    csrf: str,
    *,
    values: dict[str, str],
    engines: Sequence[tuple[str, str]],
    roles: Sequence[tuple[str, str]],
    started: Sequence[Any],
    error: str | None = None,
) -> str:
    form = (
        '<form method="post" action="/chat">'
        + _hidden(csrf)
        + '<p><label>Task<br><textarea name="prompt" rows="6">'
        + e(values.get("prompt", ""))
        + "</textarea></label></p><p>"
        + "<label>Engine "
        + _select("engine", [("", "the default"), *engines], values.get("engine", ""))
        + "</label> <label>Role "
        + _select("role", [("", "none"), *roles], values.get("role", ""))
        + '</label> <label>Model <input name="model" size="28" value="'
        + e(values.get("model", ""))
        + '"></label></p><p><label>Folder <input name="folder" size="70" value="'
        + e(values.get("folder", ""))
        + '"></label></p><p><button>Start</button></p></form>'
    )
    body = (
        _error(error)
        + form
        + '<p class="muted">The run asks you on its page before a call that writes or '
        + "destroys. Allow lets a write run; a destructive call runs only when you type yes. "
        + "Its receipt is sealed when it ends.</p>"
        + "<h2>Runs started here</h2>"
        + _table(("run", "state", "runs on", "task"), map(_started_row, started), "None yet.")
    )
    return layout("Chat", body, nonce)


MODES = (
    ("relay", "relay: each desk in turn, from the work before it"),
    ("fan", "fan: every desk at once, from one start"),
)
OFFICE_NOTE = (
    "Each desk works in its own worktree, on its own branch. A desk asks you on the run's "
    "page before a call that writes or destroys, and the ask names the desk. When the run "
    "ends, metabot desks pick brings a desk's work into your checkout."
)


def _desk_choice(desk: tuple[str, str, str | None], chosen: Sequence[str] | None) -> str:
    name, runs_on, problem = desk
    on = name in chosen if chosen is not None else problem is None
    box = f'<input type="checkbox" name="desk" value="{e(name)}"{" checked" if on else ""}>'
    return _tr(
        _td(f"<label>{box} {e(clip(name, 60))}</label>"),
        _td(e(clip(runs_on, 60))),
        _td(e(clip(problem, 300)), "bad") if problem else _td("yes"),
    )


def _box(name: str, on: bool, text: str) -> str:
    checked = " checked" if on else ""
    box = f'<input type="checkbox" name="{e(name)}" value="1"{checked}>'
    return f"<p><label>{box} {e(text)}</label></p>"


def office_form_page(
    nonce: str,
    csrf: str,
    *,
    values: dict[str, str],
    found: dict[str, Any],
    chosen: Sequence[str] | None,
    started: Sequence[Any],
    error: str | None = None,
) -> str:
    """Pick a folder's office, its desks and the mode, and start the run.

    ``chosen`` is the desks the person ticked; None ticks each that runs here.
    """
    look = (
        '<form method="get" action="/office"><p><label>Folder <input name="folder" size="70" '
        f'value="{e(values.get("folder", ""))}"></label> <button>Show its desks</button></p></form>'
    )
    if "missing" in found:
        office = f'<p class="muted">{e(found["missing"])}</p>'
    else:
        extra = ""
        if found["checks"]:
            checks = values.get("checks") == "1"
            extra += _box("checks", checks, "Run the office's checks on each desk's work")
        if found["advisors"]:
            said = "In a relay, its advisors review the work: " + ", ".join(found["advisors"])
            blocked = found["advisors_blocked"]
            on = values.get("advisors", "0" if blocked else "1") == "1"
            extra += _box("advisors", on, clip(said, 300))
            extra += "".join(
                f'<p class="bad">Cannot run here: {e(clip(why, 300))}</p>' for why in blocked
            )
        office = (
            f"<p>Office <b>{e(clip(found['office'], 80))}</b> in "
            f"<code>{e(clip(found['root'], 300))}</code></p>"
            '<form method="post" action="/office">'
            + _hidden(csrf)
            + f'<input type="hidden" name="folder" value="{e(found["root"])}">'
            + '<p><label>Task<br><textarea name="prompt" rows="6">'
            + e(values.get("prompt", ""))
            + "</textarea></label></p>"
            + _table(
                ("desk", "runs on", "runs here"),
                (_desk_choice(desk, chosen) for desk in found["desks"]),
                "This office has no desk that runs on its own.",
            )
            + "<p><label>Mode "
            + _select("mode", MODES, values.get("mode") or "relay")
            + "</label></p>"
            + extra
            + "<p><button>Start</button></p></form>"
            + f'<p class="muted">{e(OFFICE_NOTE)}</p>'
        )
    body = (
        _error(error)
        + look
        + office
        + "<h2>Runs started here</h2>"
        + _table(("run", "state", "runs on", "task"), map(_started_row, started), "None yet.")
    )
    return layout("Office", body, nonce)


def _ask_form(ask: Any, csrf: str) -> str:
    req = ask.request
    who = f"<b>{e(clip(req.who, 64))}</b>: " if req.who else ""  # the desk that asks
    destructive = req.cls.label == "destructive"
    word = (
        "<p><label>This call is destructive. Type <b>yes</b> to allow it: "
        '<input name="word" autocomplete="off" size="6"></label></p>'
        if destructive
        else ""
    )
    return (
        f'<form class="ask" method="post" action="/asks/{e(ask.ask_id)}">'
        + _hidden(csrf)
        + f"<p>{who}<b>{e(req.cls.label)}</b> call <code>{e(clip(req.tool, 200))}</code></p>"
        + _pre(req.preview)
        + word
        + '<p><button name="allow" value="1">Allow</button> '
        + '<button name="allow" value="0">Decline</button></p></form>'
    )


def live_page(
    live: Any,
    asks: Sequence[Any],
    state: str | None,
    nonce: str,
    csrf: str,
    office: dict[str, Any] | None = None,
) -> str:
    """A run started here, as it goes. An office run's ``office`` is its record's view."""
    from metabot.receipts import answer_caveat, named_here, unread_caveat

    going = live.state == "running"
    what = "office run" if live.office is not None else "run"
    if live.office is not None:
        facts = [
            ("office run", e(live.run_id)),
            ("office", e(clip(live.office, 80))),
            ("mode", e(live.mode)),
            ("desks", e(clip(", ".join(live.desks), 300))),
            ("folder", f"<code>{e(clip(live.cwd, 300))}</code>"),
            ("state", e(live.state)),
        ]
        if office is not None:
            facts.append(("record", link("/offices/", live.run_id, "the office run's page")))
    else:
        facts = [
            ("run", e(live.run_id)),
            ("runs on", e(clip(_runs_on(live.engine, live.model), 120))),
            ("folder", f"<code>{e(clip(live.cwd, 300))}</code>"),
            ("state", e(live.state)),
        ]
    if state is not None:
        facts.append(("receipt", f"{_state(state)} {link('/runs/', live.run_id, 'the run')}"))
    body = [_error(live.error), _facts(facts)]
    if asks:
        body.append("<h2>Waiting for you</h2>" + "".join(_ask_form(a, csrf) for a in asks))
    if going:
        body.append(
            f'<form method="post" action="/live/{e(live.run_id)}/stop">{_hidden(csrf)}'
            f"<p><button>Stop the {what}</button></p></form>"
        )
    if office is not None:
        body.append("<h2>Desks</h2>" + _desks_table(office, going))
        if live.state == "done":
            body.append("<h2>Pick</h2>" + _offer(office))
    result = live.result
    if live.office is None and result is not None and result.final_text:
        body.append("<h2>Answer</h2>" + _pre(result.final_text))
        caveat = answer_caveat(result.declined, result.failed)
        if caveat:
            body.append(f'<p class="bad">{e(caveat)}</p>')
        if not result.steps:  # a page run never resumes, so nothing earlier read them
            unread = unread_caveat(named_here(live.prompt, Path(live.cwd)))
            if unread:
                body.append(f'<p class="bad">{e(unread)}</p>')
    # the newest lines are what the person needs: what waits for them, and how the run ended
    lines, cut = _newest(live.out.lines)
    dropped = live.out.dropped + cut
    skipped = f"{dropped} earlier lines are not shown.\n" if dropped else ""
    body.append("<h2>Trace</h2>" + _pre(skipped + "\n".join(lines) if lines else "Nothing yet."))
    refresh = LIVE_REFRESH_S if going and not asks else None
    title = f"{what.capitalize()} {clip(live.run_id, 80)}"
    return layout(title, "".join(body), nonce, refresh=refresh)
