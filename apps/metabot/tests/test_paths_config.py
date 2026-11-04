from __future__ import annotations

import os
import re
import stat

from conftest import private_mode
from metabot import config, paths
from metabot.cli import main


def test_home_is_private(home):
    h = paths.metabot_home()
    assert h == home
    assert private_mode(h) == 0o700


def test_a_home_named_by_a_relative_path_is_named_whole(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("METABOT_HOME", "rel-home")
    # before: rel-home, which a hook or a switchboard started in another folder looked
    # for there
    assert paths.metabot_home() == tmp_path / "rel-home"


def test_the_tests_guard_clears_a_stray_home_it_cannot_list(tmp_path):
    from conftest import _clear_stray

    stray = tmp_path / "no-test-home"
    (stray / "home" / "locked" / "deep").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o500)
    (stray / "home" / "out").symlink_to(outside)
    os.chmod(stray / "home" / "locked", 0)
    _clear_stray(stray)
    # before: rmtree stopped at the folder, so every test after was named and errored
    assert not stray.exists()
    assert stat.S_IMODE(outside.stat().st_mode) == 0o500  # a link is never followed


def test_a_private_folder_and_each_parent_it_makes_are_0700_whatever_the_umask(tmp_path):
    there = tmp_path / "there"
    there.mkdir()
    os.chmod(there, 0o300)  # its owner may make folders in it; no one else may read it
    old = os.umask(0o002)  # a umask that lets the group in
    try:
        leaf = paths.private_dir(there / "made" / "also" / "leaf")
        made = [there / "made", there / "made" / "also", leaf]
        assert [private_mode(p) for p in made] == [0o700] * 3
        assert private_mode(there) == 0o300  # a parent that was there already keeps its mode
    finally:
        os.umask(old)
        os.chmod(there, 0o700)  # readable again, so pytest can clear its temp folder


def test_write_private_mode(tmp_path):
    target = tmp_path / "sub" / "secret.txt"
    paths.write_private(target, "hello")
    assert target.read_text() == "hello"
    assert private_mode(target) == 0o600
    paths.write_private(target, "again")
    assert target.read_text() == "again"
    assert private_mode(target) == 0o600


def test_run_id_shape():
    a, b = paths.new_run_id(), paths.new_run_id()
    assert re.fullmatch(r"mb-\d{8}T\d{6}Z-[0-9a-f]{6}", a)
    assert a != b


def test_config_roundtrip():
    cfg = config.load_config()
    assert cfg.default_engine is None
    assert cfg.openai_base_url == "http://127.0.0.1:11434/v1"
    cfg.default_engine = "claude"
    cfg.models["claude"] = "haiku"
    config.save_config(cfg)
    again = config.load_config()
    assert again.default_engine == "claude"
    assert again.models == {"claude": "haiku"}
    assert private_mode(paths.metabot_home() / "config.json") == 0o600


def test_version_cli(capsys):
    assert main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == "metabot 0.3.0"


def test_run_dir_refuses_path_tricks():
    import pytest

    from metabot.paths import run_dir

    for bad in ["../x", "a/b", "", ".hidden", "x" * 200]:
        with pytest.raises(ValueError):
            run_dir(bad)
    assert run_dir("mb-20261011T100000Z-abc123").name == "mb-20261011T100000Z-abc123"
