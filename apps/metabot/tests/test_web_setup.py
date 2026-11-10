"""The Setup flow (U1): seven screens, one action each, from a first start to a receipt.

The web writes no settings (PD-9), opens nothing an engine signs in with (I2), and never
shows the signing key. Engines are stand-ins on a PATH of their own; nothing is started
but their ``--version``.
"""

from __future__ import annotations

import base64
import builtins
import html
import io
import json
import os
import re
import stat
from pathlib import Path

import pytest

from metabot.paths import receipts_dir
from test_web_runs import Answers, Asks, _asked, _ended, _journal, _post, _start, _until, ui

WELCOME = "Run your work on the plan you already have."
NO_ENGINE = "No engine is ready yet. Set one up, then check again."
SCREENS = (
    "/setup",
    "/setup/key",
    "/setup/engine",
    "/setup/check",
    "/setup/check?run=1",
    "/setup/login",
    "/setup/ask",
)


@pytest.fixture(autouse=True)
def _nothing_signed_in_here(monkeypatch):
    """No variable of this machine signs an engine in, and no local model server answers,
    unless a test says so."""
    from metabot.engines import get_engine

    for name in ("claude", "codex", "cursor"):
        for variable in get_engine(name).sign_in_present():
            monkeypatch.delenv(variable, raising=False)
    monkeypatch.setattr("metabot.roster.local_status", lambda url: "not reachable (is it on?)")


def _path_of(tmp_path: Path, monkeypatch, versions: dict[str, str]) -> Path:
    """A PATH holding these stand-ins and nothing else; each logs how it was started."""
    folder = tmp_path / "bin"
    folder.mkdir(exist_ok=True)
    log = tmp_path / "started.log"
    for command, version in versions.items():
        script = folder / command
        script.write_text(
            f'#!/bin/sh\necho "{command} $*" >> "{log}"\necho "{version}"\n', encoding="utf-8"
        )
        script.chmod(0o755)
    monkeypatch.setenv("PATH", str(folder))
    return log


def _started(log: Path) -> list[str]:
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def _configure(home: Path, **settings) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.json").write_text(json.dumps(settings), encoding="utf-8")


def _key() -> Path:
    return Path(os.environ["ORCHA_KEY_PATH"])


def _main(text: str) -> str:
    return text.split("<main", 1)[1].split("</main>", 1)[0]


def _choices(text: str) -> dict[str, dict[str, str]]:
    """The engine screen's rows: each engine's radio, state and billing, by its name."""
    rows = {}
    for row in re.findall(r'<label class="choice">(.*?)</label>', text, re.S):
        name = re.search(r"<code>([a-z-]+)</code>", row).group(1)
        state = re.search(r'<span class="(ok|none|warn|bad)">([^<]+)</span>', row)
        rows[name] = {
            "radio": "disabled" if " disabled" in row else "on",
            "cls": state.group(1),
            "state": state.group(2),
            "billing": re.search(r'<span class="muted">([^<]+)</span>', row).group(1),
            "checked": " checked" in row,
        }
    return rows


def _rows(text: str) -> dict[str, tuple[str, str, str]]:
    """The check's rows: what was looked at, its state's colour and word, and its line."""
    found = re.findall(
        r'<tr><th>(.*?)</th><td><span class="(\w+)">([^<]+)</span></td><td>(.*?)</td></tr>',
        text,
        re.S,
    )
    plain = lambda value: html.unescape(re.sub(r"<[^>]+>", "", value))  # noqa: E731
    return {plain(what): (cls, word, plain(line)) for what, cls, word, line in found}


def _ask(client, guard, **fields) -> str:
    form = {"prompt": "what is in the notes?", "approval": "ask", "engine": "openai", **fields}
    response = _post(client, guard, "/setup/ask", **form)
    assert response.status_code == 303, response.text
    location = response.headers["location"]
    assert location.startswith("/live/")
    return location.removeprefix("/live/")


# ── 1. welcome, on a first start ────────────────────────────────────────────


def test_a_first_start_opens_on_the_welcome(tmp_path):
    with ui(tmp_path) as (client, _guard):
        page = client.get("/")
    assert page.status_code == 200
    assert f"<h1>{WELCOME}</h1>" in page.text
    assert '<p class="lead">Every run ends in a receipt you can check offline.</p>' in page.text
    assert '<a class="button primary" href="/setup/key">Set up</a>' in page.text
    assert "What should MetaBot do?" not in page.text


def test_once_set_up_home_is_the_task_box_and_the_welcome_waits_at_setup(tmp_path, home):
    _configure(home)
    with ui(tmp_path) as (client, _guard):
        assert "What should MetaBot do?" in client.get("/").text
        assert f"<h1>{WELCOME}</h1>" in client.get("/setup").text
        assert '<a href="/setup" aria-current="page">Setup</a>' in client.get("/setup/ask").text


def test_each_screen_has_the_shell_its_step_and_one_clear_action(tmp_path, monkeypatch):
    _path_of(tmp_path, monkeypatch, {"claude": "2.1.310 (Claude Code)"})
    with ui(tmp_path) as (client, _guard):
        for path in SCREENS:
            text = client.get(path).text
            assert '<header class="top">' in text and '<svg class="mark"' in text, path
            assert "<script" not in text and " style=" not in text, path
            assert re.search(r'<p class="kicker">Step [1-7] of 7</p>', text), path
            assert _main(text).count('primary"') == 1, path


# ── 2. the signing key ──────────────────────────────────────────────────────


def test_the_key_is_made_by_its_form_and_shown_by_its_signer_alone(tmp_path):
    key = _key()
    with ui(tmp_path) as (client, guard):
        page = client.get("/setup/key").text
        assert "<h1>Make your signing key.</h1>" in page
        assert "It signs your receipts. If a receipt is changed, it reads INVALID." in page
        assert 'method="post" action="/setup/key"' in page
        assert f'name="csrf" value="{guard.csrf}"' in _main(page)
        assert '<button class="primary">Create key</button>' in page
        assert not os.path.lexists(key)
        made = _post(client, guard, "/setup/key")
        assert made.status_code == 303 and made.headers["location"] == "/setup/key"
        assert stat.S_IMODE(key.stat().st_mode) == 0o600
        shown = client.get("/setup/key").text
        home_page = client.get("/").text
    assert '<span class="ok">Ready</span>' in shown
    assert re.search(r"<code>did:orcha:agent:key-[0-9a-f]{16}</code>", shown)
    assert '<a class="button primary" href="/setup/engine">Continue</a>' in shown
    assert "Create key" not in shown
    assert "What should MetaBot do?" in home_page  # a key made: set up, so home is the task box
    _never_shown(key, [page, shown, home_page])


def _never_shown(key: Path, pages: list[str]) -> None:
    """The key's secret, in each form it could be written, is in none of ``pages``."""
    from emerge.record import load_signing_key

    raw = load_signing_key(key).private_bytes_raw()
    forms = {
        key.read_text(encoding="utf-8").strip(),
        raw.hex(),
        base64.b64encode(raw).decode(),
        base64.urlsafe_b64encode(raw).decode().rstrip("="),
    }
    for text in pages:
        for form in forms:
            assert form not in text


def test_a_post_from_elsewhere_makes_no_key(tmp_path):
    with ui(tmp_path) as (client, guard):
        assert _post(client, guard, "/setup/key", csrf="wrong").status_code == 403
        assert client.post("/setup/key", data={"csrf": guard.csrf}).status_code == 403
    assert not os.path.lexists(_key())


def test_a_key_that_does_not_read_is_shown_and_never_replaced(tmp_path):
    key = _key()
    key.write_text("not a key\n", encoding="utf-8")
    key.chmod(0o600)
    with ui(tmp_path) as (client, guard):
        page = client.get("/setup/key").text
        refused = _post(client, guard, "/setup/key")
    assert '<span class="bad">Does not read</span>' in page and "Create key" not in page
    assert refused.status_code == 400 and "cannot read the signing key" in refused.text
    assert key.read_text(encoding="utf-8") == "not a key\n"


# ── 3. the engine ───────────────────────────────────────────────────────────


def test_the_engine_screen_names_each_engine_by_its_command(tmp_path, monkeypatch):
    log = _path_of(tmp_path, monkeypatch, {"claude": "2.1.310", "codex": "codex-cli 0.154.0"})
    with ui(tmp_path) as (client, _guard):
        page = client.get("/setup/engine").text
    assert "<h1>Which engine runs your work?</h1>" in page
    assert "You can change it for any run." in page
    rows = _choices(page)
    assert list(rows) == ["claude", "codex", "cursor", "openai"]
    for name in ("claude", "codex"):
        row = rows[name]
        assert (row["state"], row["cls"], row["radio"]) == ("ready", "ok", "on")
        assert rows[name]["billing"] == "your plan"
    for name in ("cursor", "openai"):
        assert (rows[name]["state"], rows[name]["cls"]) == ("not set up", "none")
        assert rows[name]["radio"] == "disabled"
    assert rows["openai"]["billing"] == "local"
    assert rows["claude"]["checked"]  # the default a run takes: the first CLI here
    assert 'method="get" action="/setup/check"' in page
    assert '<button class="primary">Use this engine</button>' in page
    assert _started(log) == []  # looked for on PATH, never started


def test_an_engine_a_variable_signs_in_says_so_by_the_name_alone(tmp_path, monkeypatch):
    _path_of(tmp_path, monkeypatch, {"claude": "2.1.310"})
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-value-never-shown")
    with ui(tmp_path) as (client, _guard):
        page = client.get("/setup/engine").text
    assert _choices(page)["claude"]["billing"] == "API key: ANTHROPIC_API_KEY is set here"
    assert "sk-value-never-shown" not in page


def test_a_local_model_server_that_answers_is_ready_and_local(tmp_path, monkeypatch):
    _path_of(tmp_path, monkeypatch, {})
    monkeypatch.setattr("metabot.roster.local_status", lambda url: "reachable")
    with ui(tmp_path) as (client, _guard):
        rows = _choices(client.get("/setup/engine").text)
    assert (rows["openai"]["state"], rows["openai"]["billing"]) == ("ready", "local")
    assert rows["openai"]["checked"]


def test_with_no_engine_ready_the_screen_says_so_and_offers_the_check(tmp_path, monkeypatch):
    _path_of(tmp_path, monkeypatch, {})
    with ui(tmp_path) as (client, _guard):
        page = client.get("/setup/engine").text
    assert NO_ENGINE in page
    assert "Use this engine" not in page
    assert '<a class="button primary" href="/setup/check?run=1">Run check</a>' in page


# ── 4. the check ────────────────────────────────────────────────────────────


def test_the_check_runs_when_asked_and_says_ready_or_the_step(tmp_path, monkeypatch):
    log = _path_of(tmp_path, monkeypatch, {"claude": "2.1.310 (Claude Code)"})
    with ui(tmp_path) as (client, guard):
        before = client.get("/setup/check?engine=claude").text
        assert _started(log) == []
        unmade = client.get("/setup/check?engine=claude&run=1").text
        _post(client, guard, "/setup/key")
        ready = client.get("/setup/check?engine=claude&run=1").text
    assert "<h1>Check the setup.</h1>" in before and "<table" not in _main(before)
    assert '<input type="hidden" name="engine" value="claude">' in before
    assert '<button class="primary">Run check</button>' in before
    assert _rows(unmade)["Signing key"][:2] == ("warn", "Needs a step")
    assert "Continue" not in unmade
    rows = _rows(ready)
    assert rows["Signing key"][:2] == ("ok", "Ready")
    assert re.search(r"signs as did:orcha:agent:key-[0-9a-f]{16}", rows["Signing key"][2])
    assert rows["claude"] == ("ok", "Ready", "2.1.310 (Claude Code), on PATH")
    assert rows["codex"] == ("none", "Not set up", "codex is not on PATH")
    assert rows["cursor"] == ("none", "Not set up", "cursor-agent is not on PATH")
    assert rows["openai"][:2] == ("none", "Not set up")
    assert '<span class="ok">Ready</span> Runs can start on <code>claude</code>.' in ready
    assert '<a class="button primary" href="/setup/login?engine=claude">Continue</a>' in ready
    assert "MetaBot never opens an engine&#x27;s sign-in." in ready
    assert _started(log) == ["claude --version", "claude --version"]


def test_a_pick_is_never_saved_and_the_check_shows_the_command_that_saves_it(
    tmp_path, monkeypatch, home
):
    _path_of(tmp_path, monkeypatch, {"claude": "2.1.310", "codex": "codex-cli 0.154.0"})
    with ui(tmp_path) as (client, guard):
        _post(client, guard, "/setup/key")
        picked = client.get("/setup/check?engine=codex&run=1").text
        default = client.get("/setup/check?engine=claude&run=1").text
    line = "in a terminal: <code>metabot init --engine codex</code>"
    assert '<span class="ok">Ready</span> Runs can start on <code>codex</code>.' in picked
    assert line in picked and "metabot init --engine" not in default  # claude: the default
    assert not (home / "config.json").exists()


def test_the_engine_picked_but_missing_is_the_step(tmp_path, monkeypatch):
    _path_of(tmp_path, monkeypatch, {"claude": "2.1.310"})
    with ui(tmp_path) as (client, guard):
        _post(client, guard, "/setup/key")
        page = client.get("/setup/check?engine=codex&run=1").text
    cls, word, line = _rows(page)["codex"]
    assert (cls, word) == ("warn", "Needs a step")
    assert line == "codex is not on PATH: install it, or pick another engine"
    assert '<span class="warn">Needs a step</span>' in page and "Continue" not in page
    assert '<button class="primary">Run check</button>' in page


def test_with_no_engine_ready_the_check_says_so(tmp_path, monkeypatch):
    _path_of(tmp_path, monkeypatch, {})
    with ui(tmp_path) as (client, _guard):
        page = client.get("/setup/check?run=1").text
    assert NO_ENGINE in page


def test_the_check_says_whether_an_endpoint_key_is_set_never_its_value(tmp_path, monkeypatch, home):
    _path_of(tmp_path, monkeypatch, {})
    hosted = {"base_url": "https://models.example/v1", "model": "m1", "key_env": "HOSTED_KEY"}
    _configure(home, endpoints={"hosted": hosted})
    monkeypatch.delenv("HOSTED_KEY", raising=False)
    with ui(tmp_path) as (client, _guard):
        unset = client.get("/setup/check?run=1").text
        monkeypatch.setenv("HOSTED_KEY", "value-never-shown")
        held = client.get("/setup/check?run=1").text
    assert _rows(unset)["endpoint hosted"] == (
        "warn",
        "Needs a step",
        "its key variable HOSTED_KEY is not set: export HOSTED_KEY=… (never shown here)",
    )
    assert _rows(held)["endpoint hosted"] == (
        "ok",
        "Ready",
        "its key variable HOSTED_KEY is set (never shown)",
    )
    assert "value-never-shown" not in held


def test_a_login_folder_is_looked_at_never_opened(tmp_path, monkeypatch, home):
    _path_of(tmp_path, monkeypatch, {})
    folder = tmp_path / "logins" / "work"
    folder.mkdir(parents=True, mode=0o700)
    login = {"engine": "claude", "home": str(folder), "max_at_once": 1, "on_sign_in": "refuse"}
    _configure(home, logins={"work": login})
    with ui(tmp_path) as (client, _guard):
        empty = client.get("/setup/check?run=1").text
        (folder / ".credentials.json").write_text("{}", encoding="utf-8")
        signed = client.get("/setup/check?run=1").text
        folder.chmod(0o755)
        open_to_others = client.get("/setup/check?run=1").text
    cls, word, line = _rows(empty)["login work"]
    assert (cls, word) == ("warn", "Needs a step")
    assert line.endswith(f"sign in once, in a terminal: CLAUDE_CONFIG_DIR={folder} claude")
    assert _rows(signed)["login work"] == ("ok", "Ready", "a sign-in file is there (not opened)")
    cls, word, line = _rows(open_to_others)["login work"]
    assert (cls, word) == ("warn", "Needs a step") and f"chmod 700 {folder}" in line


# ── 5. a login, which can be skipped ────────────────────────────────────────


def _vendor_note() -> tuple[list[str], list[str], list[str]]:
    """What ``metabot login add`` prints after its intro: headings, bullets and links."""
    from metabot.logins import VENDOR_NOTE

    headings, bullets, links = [], [], []
    for line in VENDOR_NOTE.splitlines()[1:]:
        if line.startswith("  - "):
            bullets.append(line[4:])
        elif line.startswith("    "):
            bullets[-1] += " " + line.strip()
        elif line.startswith("  "):
            links.append(line.strip())
        else:
            headings.append(line.rstrip(":"))
    return headings, bullets, links


def test_the_login_screen_shows_the_commands_and_what_login_add_prints(tmp_path):
    with ui(tmp_path) as (client, _guard):
        page = client.get("/setup/login?engine=claude").text
    assert "<h1>Add one of your own accounts, kept apart.</h1>" in page
    body = (
        "For example, work and personal. Each login has its own folder, and a run uses one "
        "login. You can also use an API key."
    )
    assert body in page
    headings, bullets, links = _vendor_note()
    assert len(headings) == 3 and len(bullets) >= 6 and len(links) == 2
    for text in [*headings, *bullets]:
        assert html.escape(text) in page, text
    for link in links:
        assert f"<code>{html.escape(link)}</code>" in page and f'href="{link}"' not in page
    assert "metabot login add work --engine claude --home " in page
    assert "CLAUDE_CONFIG_DIR=" in page and "mkdir -m 700 " in page
    assert '<a class="button primary" href="/setup/ask?engine=claude">Skip for now</a>' in page
    assert "<form" not in _main(page)  # the web adds no login (PD-9)


def test_an_engine_that_takes_no_login_says_so(tmp_path):
    with ui(tmp_path) as (client, _guard):
        page = client.get("/setup/login?engine=cursor").text
    assert "<code>cursor</code> takes no login" in page
    assert "metabot login add" not in page
    assert '<a class="button primary" href="/setup/ask?engine=cursor">Skip for now</a>' in page


# ── 6. the first ask ────────────────────────────────────────────────────────


def test_the_ask_screen_is_one_box_one_choice_and_run(tmp_path):
    with ui(tmp_path) as (client, guard):
        page = client.get("/setup/ask?engine=openai").text
    assert "<h1>Ask about your work.</h1>" in page
    assert '<label for="setup-prompt">What should MetaBot look at?</label>' in page
    assert '<textarea id="setup-prompt" name="prompt"' in page
    choices = re.findall(r'<input type="radio" name="approval" value="(\w+)"( checked)?>', page)
    assert choices == [("read_only", ""), ("ask", " checked"), ("allow_write", "")]
    for said in (
        "<strong>Read only</strong>: the engine reads and changes nothing.",
        "<strong>Ask before writing</strong>: each write waits for a yes. This is the default.",
        "<strong>Allow writes</strong>: writes run without asking, and an allowed script "
        "runs whole. A destructive call still waits.",
        "A call MetaBot classes destructive waits for a person to approve it.",
    ):
        assert said in page
    assert '<input type="hidden" name="engine" value="openai">' in page
    assert f'name="csrf" value="{guard.csrf}"' in _main(page)
    assert f"<code>{html.escape(str(tmp_path))}</code>" in page  # where it runs
    assert '<button class="primary">Run</button>' in page


class Records(Asks):
    """Asks about one write, as ``Asks`` does, and keeps each job it was given."""

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.jobs: list = []

    async def run(self, job, *, board=None, approve):
        self.jobs.append(job)
        async for event in super().run(job, board=board, approve=approve):
            yield event


@pytest.mark.parametrize(
    ("approval", "read_only", "asks"),
    [("read_only", True, True), ("ask", False, True), ("allow_write", False, False)],
)
def test_each_approval_runs_as_its_flag(tmp_path, approval, read_only, asks):
    engine = Records()
    with ui(tmp_path, engine) as (client, guard):
        run_id = _ask(client, guard, approval=approval)
        text = _until(client, run_id, lambda t: _asked(t) or _ended(t))
        assert _asked(text) is asks
    assert [job.read_only for job in engine.jobs] == [read_only]
    verdicts = {v["check"]: v for v in _journal(run_id)["verdicts"]}
    if asks:
        assert "scope_allow:call-1" not in verdicts
    else:
        assert verdicts["scope_allow:call-1"]["detail"] == "allow_write"


def test_allow_writes_still_stops_a_destructive_call(tmp_path):
    with ui(tmp_path, Asks(cls="destructive", tool="Bash")) as (client, guard):
        run_id = _ask(client, guard, approval="allow_write")
        text = _until(client, run_id, _asked)
    assert "This call is destructive. Type <b>yes</b> to allow it" in text


@pytest.mark.parametrize(
    ("fields", "said"),
    [
        ({"prompt": "   "}, "the task is empty"),
        ({"approval": "everything"}, "choose read only, ask before writing or allow writes"),
        ({"engine": "nope"}, "unknown engine"),
    ],
)
def test_an_ask_that_cannot_run_is_refused_on_its_screen(tmp_path, fields, said):
    form = {"prompt": "look", "approval": "ask", "engine": "openai", **fields}
    with ui(tmp_path) as (client, guard):
        response = _post(client, guard, "/setup/ask", **form)
    assert response.status_code == 400
    assert said in response.text and "<h1>Ask about your work.</h1>" in response.text


def test_an_ask_from_elsewhere_starts_nothing(tmp_path):
    engine = Records()
    form = {"prompt": "look", "approval": "allow_write", "engine": "openai"}
    with ui(tmp_path, engine) as (client, guard):
        assert _post(client, guard, "/setup/ask", csrf="wrong", **form).status_code == 403
        assert client.post("/setup/ask", data={"csrf": guard.csrf, **form}).status_code == 403
    assert engine.jobs == []


# ── 7. the receipt ──────────────────────────────────────────────────────────


def test_a_setup_run_ends_on_its_receipt(tmp_path):
    with ui(tmp_path, Answers()) as (client, guard):
        run_id = _ask(client, guard)
        live = _until(client, run_id, _ended)
        page = client.get(f"/setup/receipt/{run_id}?engine=openai").text
    link = f'<a class="button primary" href="/setup/receipt/{run_id}?engine=openai">'
    assert link + "See your receipt</a>" in live
    assert "<h1>Done. Here is your receipt.</h1>" in page
    assert f"Check it any time, offline: <code>metabot receipts verify {run_id}</code>" in page
    assert '<span class="ok">valid</span>' in page
    assert '<a class="button primary" href="/">Open Home</a>' in page
    assert '<a class="button" href="/setup/ask?engine=openai">Run another</a>' in page


def test_a_run_started_elsewhere_in_the_page_has_no_setup_receipt_link(tmp_path):
    with ui(tmp_path, Answers()) as (client, guard):
        run_id = _start(client, guard, tmp_path)
        live = _until(client, run_id, _ended)
    assert "See your receipt" not in live


def test_a_changed_receipt_reads_invalid_on_its_screen(tmp_path):
    with ui(tmp_path, Answers()) as (client, guard):
        run_id = _ask(client, guard)
        _until(client, run_id, _ended)
        receipt = receipts_dir() / f"{run_id}.json"
        data = json.loads(receipt.read_text(encoding="utf-8"))
        data["signature"] = ("A" if data["signature"][0] != "A" else "B") + data["signature"][1:]
        receipt.write_text(json.dumps(data), encoding="utf-8")
        page = client.get(f"/setup/receipt/{run_id}").text
    assert '<span class="bad">INVALID</span>' in page
    assert "This receipt reads INVALID. It was changed after the run." in page


def test_a_receipt_screen_for_no_such_run(tmp_path):
    with ui(tmp_path) as (client, _guard):
        assert client.get("/setup/receipt/no-such-run").status_code == 404
        assert client.get("/setup/receipt/.hidden").status_code == 400


def test_a_run_that_never_began_has_no_receipt_to_show():
    from metabot.web_setup import receipt_page

    page = receipt_page("n0nce", "run-1", "no receipt", None, "openai")
    assert "Done." not in page and '<span class="none">no receipt</span>' in page
    assert "This run never began, so nothing was signed." in page


# ── the whole walk ──────────────────────────────────────────────────────────


def _walk(client, guard, engine: str) -> list[str]:
    pages = [client.get(path).text for path in ("/", "/setup", "/setup/key")]
    _post(client, guard, "/setup/key")
    for path in (
        "/setup/key",
        "/setup/engine",
        f"/setup/check?engine={engine}",
        f"/setup/check?engine={engine}&run=1",
        f"/setup/login?engine={engine}",
        f"/setup/ask?engine={engine}",
    ):
        pages.append(client.get(path).text)
    run_id = _ask(client, guard, engine=engine)
    pages.append(_until(client, run_id, _ended))
    pages.append(client.get(f"/setup/receipt/{run_id}?engine={engine}").text)
    return pages


def test_the_walk_writes_no_settings_and_never_shows_the_key(tmp_path, monkeypatch, home):
    _path_of(tmp_path, monkeypatch, {"claude": "2.1.310"})
    with ui(tmp_path, Answers()) as (client, guard):
        pages = _walk(client, guard, "claude")
    assert not (home / "config.json").exists()  # PD-9: config stays CLI-written
    _never_shown(_key(), pages)


def test_the_walk_opens_nothing_an_engine_signs_in_with(tmp_path, monkeypatch, home, claude_config):
    _path_of(tmp_path, monkeypatch, {"claude": "2.1.310", "codex": "codex-cli 0.154.0"})
    codex_home = tmp_path / "codex-home"
    login = tmp_path / "logins" / "work"
    for folder, name in ((claude_config, ".credentials.json"), (codex_home, "auth.json")):
        folder.mkdir(mode=0o700, exist_ok=True)
        (folder / name).write_text("{}", encoding="utf-8")
    login.mkdir(parents=True, mode=0o700)
    (login / ".credentials.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    spec = {"engine": "claude", "home": str(login), "max_at_once": 1, "on_sign_in": "refuse"}
    _configure(home, logins={"work": spec})
    watched = [claude_config.resolve(), codex_home.resolve(), login.resolve()]
    opened: list[str] = []

    def watch(real):
        def call(file, *args, **kwargs):
            if isinstance(file, (str, bytes, os.PathLike)):
                where = Path(os.fsdecode(file)).resolve()
                if any(where.is_relative_to(folder) for folder in watched):
                    opened.append(str(where))
            return real(file, *args, **kwargs)

        return call

    monkeypatch.setattr(builtins, "open", watch(builtins.open))
    monkeypatch.setattr(io, "open", watch(io.open))
    monkeypatch.setattr(os, "open", watch(os.open))
    with ui(tmp_path, Answers()) as (client, guard):
        pages = _walk(client, guard, "claude")
    assert opened == []
    assert any("a sign-in file is there (not opened)" in text for text in pages)
