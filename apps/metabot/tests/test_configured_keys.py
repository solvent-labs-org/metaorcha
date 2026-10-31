"""Every key the config names is taken out of every record, as a connection's token is,
whichever engine or endpoint ran: a run's steps, prompt and answer, its checks, a seal's
task, an office's record, the answers one desk hands the next, a desk's commit, and a
routine's firings. Only the record changes: no variable is taken from any environment."""

from __future__ import annotations

import json
import os

from fake_chat import answer
from metabot.cli import main
from metabot.config import Config, save_config
from metabot.paths import metabot_home, run_path

KEY = "sk-spare-0123456789abcdef"
HOSTED = "https://hosted.example.test/v1"


def _everything_kept() -> str:
    """Every record MetaBot keeps, as text. The openai engine's conversation for a resume
    (``sessions/``) is its working copy of what the model was sent, not a record."""
    texts = []
    for folder, _, files in os.walk(metabot_home()):
        if os.path.relpath(folder, metabot_home()).split(os.sep)[0] == "sessions":
            continue
        for name in files:
            with open(os.path.join(folder, name), "rb") as f:
                texts.append(f.read().decode("utf-8", "replace"))
    return "\n".join(texts)


def test_a_key_the_config_names_never_reaches_a_runs_record(chat, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SPARE_KEY", KEY)  # an endpoint's key; this run does not call it
    fake = chat([answer(f"the key is {KEY}")])
    hosted = {"base_url": HOSTED, "model": "big", "key_env": "SPARE_KEY"}
    save_config(Config(openai_base_url=fake.url, endpoints={"hosted": hosted}))
    assert main(["ask", "-e", "openai", "--json", "--cwd", str(tmp_path), f"remember {KEY}"]) == 0
    run_id = json.loads(capsys.readouterr().out)["run_id"]
    kept = _everything_kept()
    assert KEY not in kept
    meta = json.loads((run_path(run_id) / "journal.json").read_text(encoding="utf-8"))["meta"]
    assert meta["prompt"] == "remember [REDACTED:SPARE_KEY]"
    assert meta["final"] == "the key is [REDACTED:SPARE_KEY]"
    assert os.environ["SPARE_KEY"] == KEY  # only the record changes


def test_the_default_key_never_reaches_a_ladder_on_another_endpoint_or_its_seal(
    chat, tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("DEFAULT_KEY", KEY)  # the openai settings' key; the rung calls "box"
    fake = chat([answer(f"the key is {KEY}")])
    save_config(
        Config(
            openai_base_url=HOSTED,
            openai_key_env="DEFAULT_KEY",
            endpoints={"box": {"base_url": fake.url, "model": "tiny"}},
            ladder=[{"engine": "openai", "endpoint": "box"}],
        )
    )
    assert main(["ask", "--ladder", "--json", "--cwd", str(tmp_path), f"remember {KEY}"]) == 0
    ladder = json.loads(capsys.readouterr().out)["ladder"]["ladder_id"]
    assert KEY not in _everything_kept()
    seal = json.loads((run_path(ladder) / "journal.json").read_text(encoding="utf-8"))
    assert seal["meta"]["task"] == "remember [REDACTED:DEFAULT_KEY]"


def test_a_checks_run_never_keeps_a_key_the_config_names(chat, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SPARE_KEY", KEY)
    fake = chat([answer("done")])
    hosted = {"base_url": HOSTED, "model": "big", "key_env": "SPARE_KEY"}
    save_config(
        Config(
            openai_base_url=fake.url,
            endpoints={"hosted": hosted},
            ladder=[{"engine": "openai", "model": "tiny"}],
        )
    )
    argv = ["ask", "--ladder", "--check", "printenv SPARE_KEY", "--json", "--cwd", str(tmp_path)]
    assert main([*argv, "say done"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ladder"]["rungs"][0]["check"] == "pass"  # the check ran and printed the key
    kept = _everything_kept()
    assert KEY not in kept and "[REDACTED:SPARE_KEY]" in kept


def test_a_key_variable_no_environment_can_hold_stops_no_run(tmp_path, monkeypatch):
    from metabot.config import configured_keys
    from test_content_commitment import _run

    monkeypatch.setenv("SPARE_KEY", KEY)
    hosted = {"base_url": HOSTED, "model": "big", "key_env": "SPARE_KEY"}
    settings = {"openai_key_env": "\ud800", "endpoints": {"hosted": hosted}}  # a lone surrogate
    (metabot_home() / "config.json").write_text(json.dumps(settings), encoding="utf-8")
    assert configured_keys(Config(**settings)) == {"SPARE_KEY": KEY}
    result = _run(tmp_path, prompt=f"remember {KEY}")
    meta = json.loads((run_path(result.run_id) / "journal.json").read_text(encoding="utf-8"))
    assert meta["meta"]["prompt"] == "remember [REDACTED:SPARE_KEY]"


def test_two_secrets_under_one_name_are_both_taken_out():
    from metabot.connections import merged_secrets
    from metabot.receipts import scrub

    secrets = merged_secrets({"GH_TOKEN": "tok-one"}, {"GH_TOKEN": "tok-two", "X": "tok-x"})
    assert sorted(secrets.values()) == ["tok-one", "tok-two", "tok-x"]
    said = scrub("tok-one tok-two tok-x", secrets)
    assert "tok-" not in said and said.count("[REDACTED:GH_TOKEN") == 2


# ── an office, its desks' commits, and a routine's firings ──────────────────


def _hosted_key(monkeypatch) -> None:
    monkeypatch.setenv("SPARE_KEY", KEY)
    hosted = {"base_url": HOSTED, "model": "big", "key_env": "SPARE_KEY"}
    save_config(Config(endpoints={"hosted": hosted}))


def _desks(plan=lambda job: "PLAN", fail=None):
    from test_office_run import DeskEngine, _build

    return {
        "claude": DeskEngine("claude", act=plan, fail=fail),
        "codex": DeskEngine("codex", act=_build),
        "cursor": DeskEngine("cursor", act=lambda job: "REVIEW: fine"),
    }


async def test_a_desks_answer_is_kept_and_handed_on_without_a_key_it_holds(tmp_path, monkeypatch):
    from test_office_run import _relay, _repo

    _hosted_key(monkeypatch)
    engines = _desks(plan=lambda job: f"PLAN: use {KEY}")
    result = await _relay(_repo(tmp_path), engines)
    assert result.ok and result.rows[0].answer == "PLAN: use [REDACTED:SPARE_KEY]"
    note = metabot_home() / "desks" / result.office_run_id / "build" / ".metabot/inbox/plan.md"
    assert note.read_text(encoding="utf-8") == "PLAN: use [REDACTED:SPARE_KEY]"
    assert KEY not in _everything_kept()  # its artefact, the office record, every handoff


async def test_an_office_record_keeps_a_desks_error_without_a_key_it_holds(tmp_path, monkeypatch):
    from metabot.office_run import office_journal_path
    from test_office_run import _relay, _repo

    _hosted_key(monkeypatch)
    engines = _desks(fail=f"claude: error_during_execution: I used {KEY}")
    result = await _relay(_repo(tmp_path), engines)
    assert not result.ok and "[REDACTED:SPARE_KEY]" in (result.rows[0].error or "")
    assert KEY not in office_journal_path(result.office_run_id).read_text(encoding="utf-8")
    assert KEY not in _everything_kept()


async def test_a_desks_commit_names_the_task_without_a_key_it_holds(tmp_path, monkeypatch):
    from test_office_run import _git, _relay, _repo

    _hosted_key(monkeypatch)
    root = _repo(tmp_path)
    result = await _relay(root, _desks(), task=f"deploy with {KEY}")
    message = _git(root, "log", "-1", "--format=%B", result.rows[1].branch)
    assert KEY not in message and "build: deploy with [REDACTED:SPARE_KEY]" in message


async def test_a_firing_keeps_its_runs_error_without_a_key_it_holds(tmp_path, monkeypatch):
    from metabot.events import ErrorEvent, Final, Started
    from metabot.routines import fire, save_routine
    from test_routines import _spec, _tools

    class Failing:
        name = "fake"
        tool_prefix = "fake"
        skip_tools = ()

        async def run(self, job, *, board=None, approve):
            yield Started("s-1", job.model)
            yield ErrorEvent(f"fake: it said {KEY}")
            yield Final("", "s-1")

    _hosted_key(monkeypatch)
    rid = save_routine(_spec(tmp_path, connections=[]), list_tools=_tools)
    firing = await fire(rid, make_engine=lambda name: Failing())
    assert firing.reason and "[REDACTED:SPARE_KEY]" in firing.reason
    assert KEY not in _everything_kept()  # the firings, and the run's own record


async def test_a_firing_whose_engine_would_not_start_keeps_no_key_it_names(tmp_path, monkeypatch):
    from metabot.routines import fire, save_routine
    from test_routines import _spec, _tools

    def broken(name):
        raise RuntimeError(f"cannot start with {KEY}")

    _hosted_key(monkeypatch)
    rid = save_routine(_spec(tmp_path, connections=[]), list_tools=_tools)
    firing = await fire(rid, make_engine=broken)
    assert firing.state == "error" and "[REDACTED:SPARE_KEY]" in (firing.reason or "")
    assert KEY not in _everything_kept()


async def test_the_switchboards_log_keeps_no_key_the_settings_name(tmp_path, monkeypatch):
    from metabot.connections import ConnectionStore
    from test_switchboard import Harness, _talkative

    _hosted_key(monkeypatch)
    store = ConnectionStore()
    _talkative(store)
    log_path = tmp_path / "switchboard.log"

    async def refuse(conn):
        raise RuntimeError(f"the server said: no model at {KEY}")

    with open(log_path, "a", encoding="utf-8") as errlog:
        h = Harness(store)
        h.board.errlog = errlog
        monkeypatch.setattr(h.board, "_open", refuse)
        async with h.board as board:
            assert board.tools() == []
    text = log_path.read_text(encoding="utf-8")
    assert KEY not in text and KEY not in board.skipped["loud"]
    assert "connection loud skipped: the server said: no model at [REDACTED:SPARE_KEY]" in text
