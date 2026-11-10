"""Logins (S1): a login names one of the person's own claude or codex sign-in folders.

MetaBot points an engine at the folder and never opens anything in it: it looks at the
folder with ``stat`` alone, and prints the line the person runs to sign in themselves.
"""

from __future__ import annotations

import builtins
import io
import json
import os
from pathlib import Path

import pytest

from metabot.cli import main
from metabot.config import Config, ConfigError, Login, load_config, login_of, save_config


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    """A sign-in folder as a vendor CLI makes one: owner-only, outside any project."""
    f = tmp_path / "logins" / "claude-work"
    f.mkdir(parents=True)
    os.chmod(f, 0o700)
    return f


def logins() -> dict:
    return load_config().logins


# ── add ─────────────────────────────────────────────────────────────────────


def test_add_keeps_the_login_and_prints_the_line_to_sign_in_with(folder, capsys):
    assert main(["login", "add", "work", "--engine", "claude", "--home", str(folder)]) == 0
    assert logins() == {
        "work": {"engine": "claude", "home": str(folder), "max_at_once": 1, "on_sign_in": "refuse"}
    }
    out = capsys.readouterr().out
    assert f"CLAUDE_CONFIG_DIR={folder} claude" in out
    assert "metabot login check work" in out


def test_a_codex_login_is_signed_in_with_its_own_variable(folder, capsys):
    assert main(["login", "add", "home", "--engine", "codex", "--home", str(folder)]) == 0
    assert f"CODEX_HOME={folder} codex login" in capsys.readouterr().out


def test_a_folder_with_a_space_is_quoted_in_the_sign_in_line(tmp_path, capsys):
    spaced = tmp_path / "my logins" / "work"
    spaced.mkdir(parents=True)
    os.chmod(spaced, 0o700)
    assert main(["login", "add", "work", "--engine", "claude", "--home", str(spaced)]) == 0
    assert f"CLAUDE_CONFIG_DIR='{spaced}' claude" in capsys.readouterr().out


def test_max_at_once_is_kept(folder):
    argv = ["login", "add", "work", "--engine", "claude", "--home", str(folder)]
    assert main([*argv, "--max-at-once", "2"]) == 0
    assert logins()["work"]["max_at_once"] == 2


@pytest.mark.parametrize("n", ["0", "6", "-1"])
def test_max_at_once_outside_one_to_five_is_refused(folder, n, capsys):
    argv = ["login", "add", "work", "--engine", "claude", "--home", str(folder)]
    assert main([*argv, "--max-at-once", n]) == 2
    assert "1 to 5" in capsys.readouterr().err
    assert logins() == {}


@pytest.mark.parametrize(
    "name", ["Work", "me@example.com", "a.b", "default", "", "work\n", "w" * 33, "wörk"]
)
def test_a_name_that_is_not_a_short_lower_case_word_is_refused(folder, name, capsys):
    # no "@" and no ".": a login's name can never be an email address
    assert main(["login", "add", name, "--engine", "claude", "--home", str(folder)]) == 2
    assert logins() == {}


@pytest.mark.parametrize("engine", ["cursor", "openai", "nope"])
def test_only_claude_and_codex_have_logins(folder, engine, capsys):
    assert main(["login", "add", "work", "--engine", engine, "--home", str(folder)]) == 2
    assert "claude or codex" in capsys.readouterr().err
    assert logins() == {}


def test_a_relative_folder_is_refused(folder, monkeypatch, capsys):
    monkeypatch.chdir(folder.parent)
    assert main(["login", "add", "work", "--engine", "claude", "--home", folder.name]) == 2
    assert "absolute" in capsys.readouterr().err
    assert logins() == {}


def test_a_folder_that_is_not_there_is_refused_with_the_line_that_makes_it(tmp_path, capsys):
    missing = tmp_path / "nope"
    assert main(["login", "add", "work", "--engine", "claude", "--home", str(missing)]) == 2
    assert f"mkdir -m 700 {missing}" in capsys.readouterr().err
    assert not missing.exists()  # it is never made for the person
    assert logins() == {}


def test_a_file_is_not_a_folder(tmp_path, capsys):
    f = tmp_path / "file"
    f.write_text("")
    assert main(["login", "add", "work", "--engine", "claude", "--home", str(f)]) == 2
    assert "not a folder" in capsys.readouterr().err


@pytest.mark.parametrize("mode", [0o750, 0o705, 0o755])
def test_a_folder_others_can_enter_is_refused_with_the_line_that_closes_it(folder, mode, capsys):
    os.chmod(folder, mode)
    assert main(["login", "add", "work", "--engine", "claude", "--home", str(folder)]) == 2
    assert f"chmod 700 {folder}" in capsys.readouterr().err
    assert logins() == {}


def test_the_home_folder_and_a_folder_that_holds_it_are_refused(tmp_path, monkeypatch, capsys):
    user_home = tmp_path / "user-home"
    user_home.mkdir()
    os.chmod(user_home, 0o700)
    for target in (user_home, tmp_path):
        os.chmod(target, 0o700)
        assert main(["login", "add", "work", "--engine", "claude", "--home", str(target)]) == 2
        assert "home folder" in capsys.readouterr().err
    assert logins() == {}


def test_a_folder_inside_metabot_s_own_home_is_refused(home, capsys):
    inside = home / "logins" / "work"
    inside.mkdir(parents=True)
    os.chmod(inside, 0o700)
    assert main(["login", "add", "work", "--engine", "claude", "--home", str(inside)]) == 2
    assert "MetaBot's own" in capsys.readouterr().err


def test_a_folder_inside_a_project_is_refused(tmp_path, capsys):
    project = tmp_path / "project"
    (project / ".git" / "objects").mkdir(parents=True)
    (project / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    inside = project / "logins" / "work"
    inside.mkdir(parents=True)
    os.chmod(inside, 0o700)
    assert main(["login", "add", "work", "--engine", "claude", "--home", str(inside)]) == 2
    assert f"inside the project {project}" in capsys.readouterr().err


@pytest.mark.parametrize("kind", ["folder", "file"])
def test_a_folder_that_is_a_project_is_refused_without_reading_its_git_file(folder, kind, capsys):
    dot = folder / ".git"
    if kind == "folder":
        dot.mkdir()
    else:  # as a linked worktree has; it is never opened
        dot.write_text("gitdir: /nowhere\n")
        os.chmod(dot, 0o000)
    assert main(["login", "add", "work", "--engine", "claude", "--home", str(folder)]) == 2
    assert "git repository" in capsys.readouterr().err


def test_a_name_already_taken_is_refused(folder, tmp_path, capsys):
    other = tmp_path / "other"
    other.mkdir(mode=0o700)
    assert main(["login", "add", "work", "--engine", "claude", "--home", str(folder)]) == 0
    assert main(["login", "add", "work", "--engine", "codex", "--home", str(other)]) == 2
    assert "metabot login remove work" in capsys.readouterr().err
    assert logins()["work"]["engine"] == "claude"


def test_one_folder_is_one_login(folder, capsys):
    assert main(["login", "add", "work", "--engine", "claude", "--home", str(folder)]) == 0
    link = folder.parent / "link"
    link.symlink_to(folder)
    assert main(["login", "add", "again", "--engine", "claude", "--home", str(link)]) == 2
    assert "login work" in capsys.readouterr().err
    assert set(logins()) == {"work"}


# ── the folder is never opened ──────────────────────────────────────────────


def test_no_file_in_a_login_folder_is_ever_opened(folder, monkeypatch, capsys):
    """The canary: add, list, check and remove look at the folder with stat alone."""
    (folder / ".credentials.json").write_text('{"token": "never read"}')
    os.chmod(folder / ".credentials.json", 0o000)  # opening it fails, should a spy miss it
    opened: list[str] = []
    root = str(folder)

    def spy(real):
        def wrapped(path, *a, **kw):
            text = os.fsdecode(path) if isinstance(path, (str, bytes, os.PathLike)) else ""
            if text.startswith(root):
                opened.append(text)
            return real(path, *a, **kw)

        return wrapped

    for owner, name in ((builtins, "open"), (io, "open"), (os, "open")):
        monkeypatch.setattr(owner, name, spy(getattr(owner, name)))
    monkeypatch.setattr(os, "scandir", spy(os.scandir))
    monkeypatch.setattr(os, "listdir", spy(os.listdir))

    main(["login", "add", "work", "--engine", "claude", "--home", str(folder)])
    main(["login", "list"])
    main(["login", "check"])
    main(["login", "remove", "work"])
    assert opened == []


# ── list, check, remove ─────────────────────────────────────────────────────


def test_list_names_each_login_and_where_it_points(folder, capsys):
    main(["login", "add", "work", "--engine", "claude", "--home", str(folder)])
    capsys.readouterr()
    assert main(["login", "list"]) == 0
    out = capsys.readouterr().out
    assert "work" in out and "claude" in out and str(folder) in out


def test_list_with_none_says_how_to_add_one(capsys):
    assert main(["login", "list"]) == 0
    assert "metabot login add" in capsys.readouterr().out


def test_check_says_a_sign_in_file_is_there_without_opening_it(folder, capsys):
    main(["login", "add", "work", "--engine", "claude", "--home", str(folder)])
    (folder / ".credentials.json").write_text("")
    capsys.readouterr()
    assert main(["login", "check", "work"]) == 0
    out = capsys.readouterr().out
    assert "work: ok" in out and "not opened" in out


def test_check_on_a_folder_with_no_sign_in_says_how_to_sign_in(folder, capsys):
    main(["login", "add", "work", "--engine", "codex", "--home", str(folder)])
    capsys.readouterr()
    assert main(["login", "check", "work"]) == 0  # the folder is fine; signing in is theirs
    assert f"CODEX_HOME={folder} codex login" in capsys.readouterr().out


def test_check_fails_on_a_folder_that_others_can_now_enter(folder, capsys):
    main(["login", "add", "work", "--engine", "claude", "--home", str(folder)])
    os.chmod(folder, 0o755)
    capsys.readouterr()
    assert main(["login", "check"]) == 1
    assert f"chmod 700 {folder}" in capsys.readouterr().out


def test_check_fails_on_a_folder_that_is_gone(folder, capsys):
    main(["login", "add", "work", "--engine", "claude", "--home", str(folder)])
    folder.rmdir()
    capsys.readouterr()
    assert main(["login", "check"]) == 1
    assert "not there" in capsys.readouterr().out


def test_check_of_a_login_that_does_not_exist(capsys):
    assert main(["login", "check", "nope"]) == 2
    assert "metabot login list" in capsys.readouterr().err


def test_remove_forgets_the_login_and_leaves_the_folder(folder, capsys):
    main(["login", "add", "work", "--engine", "claude", "--home", str(folder)])
    (folder / "kept").write_text("")
    assert main(["login", "remove", "work"]) == 0
    assert logins() == {}
    assert (folder / "kept").exists()
    assert "left as it is" in capsys.readouterr().out


def test_remove_of_a_login_that_does_not_exist(capsys):
    assert main(["login", "remove", "nope"]) == 2


# ── config.json and login_of ────────────────────────────────────────────────


def test_login_of_reads_a_login_and_default_is_the_inherited_environment(folder):
    cfg = Config(logins={"work": {"engine": "codex", "home": str(folder), "max_at_once": 2}})
    assert login_of(cfg, "work") == Login("work", "codex", str(folder), 2)
    assert login_of(cfg, "default") is None
    with pytest.raises(ConfigError, match="no login named 'nope'"):
        login_of(cfg, "nope")


def test_a_login_with_no_max_at_once_runs_one_at_a_time(folder):
    cfg = Config(logins={"work": {"engine": "claude", "home": str(folder)}})
    assert login_of(cfg, "work").max_at_once == 1


@pytest.mark.parametrize(
    "table",
    [
        [],
        {"Work": {"engine": "claude", "home": "/x"}},
        {"default": {"engine": "claude", "home": "/x"}},
        {"work": {"engine": "cursor", "home": "/x"}},
        {"work": {"engine": "claude", "home": "x"}},
        {"work": {"engine": "claude"}},
        {"work": {"engine": "claude", "home": "/x", "token": "t"}},
        {"work": {"engine": "claude", "home": "/x", "max_at_once": 0}},
        {"work": {"engine": "claude", "home": "/x", "max_at_once": True}},
        {"work": {"engine": "claude", "home": "/x\x00y"}},
    ],
)
def test_a_hand_edited_logins_table_that_does_not_read_stops_the_command(home, table):
    save_config(Config())
    path = home / "config.json"
    data = json.loads(path.read_text())
    data["logins"] = table
    path.write_text(json.dumps(data))
    with pytest.raises(ConfigError, match="logins is not"):
        load_config()


# ── review 1 (11-02) ────────────────────────────────────────────────────────


def test_a_link_inside_a_project_to_a_good_folder_is_refused(folder, tmp_path, capsys):
    project = tmp_path / "project"
    (project / ".git" / "objects").mkdir(parents=True)
    (project / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    link = project / "creds"
    link.symlink_to(folder)  # a checkout could point it at the project's own files
    assert main(["login", "add", "work", "--engine", "claude", "--home", str(link)]) == 2
    assert "inside the project" in capsys.readouterr().err
    assert logins() == {}


@pytest.mark.parametrize("which", ["inside", "around"])
def test_a_login_folder_inside_or_around_another_is_refused(folder, which, capsys):
    assert main(["login", "add", "work", "--engine", "claude", "--home", str(folder)]) == 0
    if which == "inside":
        other = folder / "nested"
        other.mkdir(mode=0o700)
    else:
        other = folder.parent
        os.chmod(other, 0o700)
    assert main(["login", "add", "two", "--engine", "codex", "--home", str(other)]) == 2
    assert "login work" in capsys.readouterr().err
    assert set(logins()) == {"work"}
