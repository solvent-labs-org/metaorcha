"""A file nested past what Python can parse reads as one that does not read: each reader
gives its own answer for a broken file, never a traceback. Anyone who has the home can edit
what MetaBot keeps there, and a project's own files come with the project."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from metabot.cli import main
from metabot.paths import metabot_home, run_path

DEEP = "[" * 100_000 + "]" * 100_000
RUN = "mb-20260101T000000Z-aaaaaa"


def _deep(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(DEEP, encoding="utf-8")
    return path


def _deep_journal() -> None:
    _deep(run_path(RUN) / "journal.json")


# ── what a person sees ──────────────────────────────────────────────────────


def test_receipts_show_says_there_is_no_such_run_on_a_deep_journal(capsys):
    _deep_journal()
    assert main(["receipts", "show", RUN]) == 2
    assert f"no run {RUN!r}" in capsys.readouterr().err


def test_receipts_show_takes_a_deep_office_record_as_no_office_run(capsys):
    from metabot.office_run import load_record, office_journal_path

    _deep(office_journal_path(RUN))
    assert load_record(RUN) is None
    assert main(["receipts", "show", RUN]) == 2


def test_the_run_page_says_it_cannot_read_a_deep_journal(tmp_path):
    from test_web_pages import _client

    _deep_journal()
    response = _client(tmp_path).get(f"/runs/{RUN}")
    assert response.status_code == 200
    assert "cannot read this run" in response.text


# ── each reader ─────────────────────────────────────────────────────────────


def _claude_settings(tmp_path):
    from metabot.engines.claude import _read_settings

    return _read_settings(_deep(tmp_path / "settings.json"))


def _cursor_servers(tmp_path):
    from metabot.engines.cursor import project_declares_servers

    _deep(tmp_path / ".cursor" / "mcp.json")
    return project_declares_servers(tmp_path)


def _openai_session(tmp_path):
    from metabot.config import Config
    from metabot.engines.openai_compat import OpenAIEngine

    engine = OpenAIEngine(config=Config())
    _deep(engine._session_path("s-1"))
    return engine._load_session("s-1")


def _ladder_record(tmp_path):
    from metabot.ladder import ladder_path, load_ladder

    _deep(ladder_path(RUN))
    return load_ladder(RUN)


def _model_probes(tmp_path):
    from metabot.models import _read
    from metabot.paths import models_path

    _deep(models_path())
    return _read()


def _run_caveat(tmp_path):
    from metabot.receipts import run_caveat

    _deep_journal()
    return run_caveat(RUN)


def _receipt_verdicts(tmp_path):
    from metabot.receipts import verdicts_of

    return verdicts_of(_deep(tmp_path / "receipt.json"))


def _firing_journal(tmp_path):
    from metabot.routine_commands import _journal

    _deep_journal()
    return _journal(RUN)


def _firing_error(tmp_path):
    from metabot.routines import _run_error

    _deep_journal()
    return _run_error(RUN)


def _index_rows(tmp_path):
    from metabot.receipts import _index_path, list_index

    row = json.dumps({"run_id": RUN})
    _index_path().write_text(f"{DEEP}\n{row}\n", encoding="utf-8")
    return [r["run_id"] for r in list_index()]


def _firing_rows(tmp_path):
    from metabot.routines import FIRINGS, firings

    (metabot_home() / FIRINGS).write_text(f"{DEEP}\n", encoding="utf-8")
    return firings()


QUIET = {
    "claude settings": (_claude_settings, {}),
    "cursor servers": (_cursor_servers, True),  # a file that does not read may declare some
    "openai session": (_openai_session, []),
    "ladder record": (_ladder_record, None),
    "model probes": (_model_probes, {}),
    "run caveat": (_run_caveat, None),
    "receipt verdicts": (_receipt_verdicts, []),
    "firing journal": (_firing_journal, {}),
    "firing error": (_firing_error, None),
    "run index row": (_index_rows, [RUN]),  # the deep row is passed over
    "firing row": (_firing_rows, []),
}


@pytest.mark.parametrize("case", QUIET.values(), ids=QUIET.keys())
def test_a_reader_takes_a_deep_file_as_one_that_does_not_read(tmp_path, case):
    read, expected = case
    assert read(tmp_path) == expected


def _config(tmp_path):
    from metabot.config import _path, load_config

    _deep(_path())
    load_config()


def _connections(tmp_path):
    from metabot.connections import ConnectionStore

    store = ConnectionStore()
    _deep(store.path)
    store.active()


def _plugin_state(tmp_path):
    from metabot.plugins import _state

    _deep(metabot_home() / "plugins.json")
    _state("plugins.json")


def _bridge_spec(tmp_path):
    from metabot.plugins import BRIDGES, _bridge_specs

    _deep(tmp_path / BRIDGES / "deep.yaml")
    _bridge_specs(tmp_path)


def _trust_record(tmp_path):
    from metabot.checks import recorded, trust_path

    office = SimpleNamespace(root=tmp_path)
    _deep(trust_path(office))
    recorded(office)


def _routines(tmp_path):
    from metabot.routines import ROUTINES, routines

    _deep(metabot_home() / ROUTINES)
    routines()


def _errors():
    from metabot.checks import CheckError
    from metabot.config import ConfigError
    from metabot.connections import ConnectionFileError
    from metabot.plugins import PluginError

    return {
        "config": (_config, ConfigError),
        "connections": (_connections, ConnectionFileError),
        "plugin state": (_plugin_state, PluginError),
        "bridge spec": (_bridge_spec, PluginError),
        "trust record": (_trust_record, CheckError),
        "routines": (_routines, ValueError),  # what every reader of routines catches
    }


LOUD = _errors()


@pytest.mark.parametrize("case", LOUD.values(), ids=LOUD.keys())
def test_a_reader_that_stops_on_a_broken_file_stops_on_a_deep_one_saying_which(tmp_path, case):
    read, error = case
    with pytest.raises(error):
        read(tmp_path)


def test_a_command_with_a_deep_config_says_which_file_does_not_read(capsys):
    from metabot.config import _path

    _deep(_path())
    assert main(["roster"]) == 1
    assert f"cannot read {_path()}" in capsys.readouterr().err
