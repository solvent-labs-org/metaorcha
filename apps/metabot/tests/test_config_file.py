"""A config.json edited by hand into something MetaBot cannot read is named, never a traceback."""

from __future__ import annotations

import re
from dataclasses import fields

import pytest

from metabot.cli import main
from metabot.config import SETTING_TYPES, Config, ConfigError, load_config
from metabot.paths import metabot_home

BROKEN = [
    (b"{not json", "Expecting property name"),
    (b"\xff\xfe", "codec can't decode"),
    (b"[1, 2]", "it holds no table of settings"),
    (b'"claude"', "it holds no table of settings"),
    (b'{"default_engine": 5}', "default_engine is not text or null"),
    (b'{"models": "haiku"}', "models is not a table of engine to model name"),
    (b'{"models": {"claude": 5}}', "models is not a table of engine to model name"),
    (b'{"models": {"claude": "--dangerously-skip-permissions"}}', "models is not a table"),
    (b'{"openai_base_url": 3}', "openai_base_url is not text"),
    (b'{"openai_model": null}', "openai_model is not text"),
    (b'{"openai_key_env": []}', "openai_key_env is not text or null"),
    (b'{"codex_ignore_user_config": "no"}', "codex_ignore_user_config is not true or false"),
    (b'{"fan_cap": "many"}', "fan_cap is not a whole number"),
    (b'{"fan_cap": true}', "fan_cap is not a whole number"),
    (b'{"project_receipts": "yes"}', "project_receipts is not true or false"),
]


def _config_file():
    return metabot_home() / "config.json"


@pytest.mark.parametrize(("raw", "says"), BROKEN)
def test_a_config_file_that_does_not_read_is_named(raw, says):
    path = _config_file()
    path.write_bytes(raw)
    with pytest.raises(ConfigError, match=f"^cannot read {re.escape(str(path))}: ") as caught:
        load_config()
    assert says in str(caught.value)
    assert path.read_bytes() == raw  # never written over


def test_a_config_path_that_is_a_folder_is_named():
    _config_file().mkdir()
    with pytest.raises(ConfigError, match="^cannot read "):
        load_config()


def test_every_setting_has_a_type_it_is_read_as():
    # the roster is left out on purpose: a hand-edited roster that is not a table counts as none
    assert set(SETTING_TYPES) | {"roster"} == {f.name for f in fields(Config)}


def test_a_key_it_does_not_know_and_a_roster_of_another_shape_still_read():
    _config_file().write_text(
        '{"later": {"any": 1}, "roster": [], "fan_cap": 9, "default_engine": null, "models": {}}',
        encoding="utf-8",
    )
    cfg = load_config()
    assert (cfg.fan_cap, cfg.default_engine, cfg.models) == (9, None, {})
    assert main(["roster"]) == 0  # a roster that is not a table counts as none


def test_every_command_names_the_config_file(capsys, monkeypatch):
    path = _config_file()
    path.write_text("{not json", encoding="utf-8")
    replies = iter(["hello"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(replies))
    for argv in (
        ["ask", "-e", "openai", "-q", "hi"],
        ["ask", "-q", "hi"],
        ["chat", "-e", "openai"],
        ["engines"],
        ["init"],
        ["config", "show"],
        ["config", "set", "fan_cap", "2"],
        ["roster"],
        ["roster", "set", "build", "--engine", "codex"],
        ["suggest", "fix the failing test"],
    ):
        code = main(argv)
        _, err = capsys.readouterr()
        assert code == 1, argv
        assert err.startswith(f"metabot: cannot read {path}: "), (argv, err)
        assert "Traceback" not in err
    assert path.read_text(encoding="utf-8") == "{not json"  # no command wrote over it


def test_config_set_names_a_bad_key_or_value_once(capsys):
    assert main(["config", "set", "nope", "1"]) == 2
    said = capsys.readouterr().err
    assert said == "metabot: no setting 'nope'; one of: project_receipts, fan_cap\n"
    assert main(["config", "set", "fan_cap", "9"]) == 2
    assert capsys.readouterr().err == "metabot: fan_cap takes a whole number from 1 to 5\n"


def test_fan_cap_rejects_a_digit_outside_ascii(capsys):
    assert main(["config", "set", "fan_cap", "²"]) == 2
    assert capsys.readouterr().err == "metabot: fan_cap takes a whole number from 1 to 5\n"
