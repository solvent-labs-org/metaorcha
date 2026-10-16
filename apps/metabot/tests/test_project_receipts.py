"""An opt-in copy of each receipt in the project; the copy in the home stays the one that counts."""

from __future__ import annotations

import errno
import json
import os
import stat
from pathlib import Path

import pytest

from conftest import FIXTURES
from fake_engines import wrap
from metabot import office_run
from metabot.cli import main
from metabot.config import Config, load_config, save_config
from metabot.engines.claude import ClaudeEngine
from metabot.paths import receipts_dir
from metabot.receipts import (
    PROJECT_RECEIPTS,
    copy_to_project,
    list_index,
    project_copy,
    verify_file,
)
from metabot.routines import fire, save_routine
from test_checks import _trusted
from test_office_run import TASK, DeskEngine, _build, _cli, _git
from test_routines import _spec, _tools

SECRET_PROMPT = "summarise the plan named zebra-quartz-7"


@pytest.fixture
def ask(monkeypatch, capsys):
    """``metabot ask`` with a stand-in claude CLI; the exit code, the run's index row, stderr."""
    monkeypatch.setenv("FAKE_FIXTURE", str(FIXTURES / "claude-echo.jsonl"))
    monkeypatch.setattr("metabot.commands.get_engine", lambda name: wrap(ClaudeEngine))

    def run(cwd: Path, *argv: str) -> tuple[int, dict | None, str]:
        before = len(list_index())
        code = main(["ask", "-e", "claude", "--cwd", str(cwd), *argv, SECRET_PROMPT])
        rows = list_index()
        err = capsys.readouterr().err
        return code, rows[-1] if len(rows) > before else None, err

    return run


def _project(tmp_path: Path) -> Path:
    """A git repository with a sub-folder to work in."""
    repo = tmp_path / "proj"
    (repo / "sub").mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    return repo


def _on() -> None:
    save_config(Config(project_receipts=True))


def test_off_by_default(tmp_path, ask):
    repo = _project(tmp_path)
    assert load_config().project_receipts is False
    code, row, err = ask(repo / "sub")
    assert code == 0 and row is not None
    assert not (repo / ".orcha").exists() and not (repo / "sub" / ".orcha").exists()
    assert "project copy" not in err


def test_copy_goes_to_the_git_top_level(tmp_path, ask, capsys):
    repo = _project(tmp_path)
    _on()
    code, row, err = ask(repo / "sub")
    assert code == 0
    copy = repo / PROJECT_RECEIPTS / f"{row['run_id']}.json"
    assert copy.read_bytes() == Path(row["receipt"]).read_bytes()
    assert stat.S_IMODE(copy.stat().st_mode) == 0o600
    assert f"[metabot] project copy {copy}" in err
    capsys.readouterr()
    assert main(["receipts", "verify", str(copy)]) == 0
    assert capsys.readouterr().out.splitlines()[0] == f"VALID  {copy}"


def test_journal_never_copied(tmp_path, ask):
    repo = _project(tmp_path)
    _on()
    code, row, _ = ask(repo)
    assert code == 0
    found = sorted(str(p.relative_to(repo)) for p in (repo / ".orcha").rglob("*"))
    assert found == [".orcha/receipts", f".orcha/receipts/{row['run_id']}.json"]
    assert SECRET_PROMPT not in (repo / PROJECT_RECEIPTS / f"{row['run_id']}.json").read_text()


def test_outside_a_repo_uses_cwd(tmp_path, ask):
    plain = tmp_path / "plain"
    plain.mkdir()
    _on()
    code, row, _ = ask(plain)
    assert code == 0
    assert (plain / PROJECT_RECEIPTS / f"{row['run_id']}.json").is_file()


def test_home_copy_stays_canonical(tmp_path, ask, capsys):
    repo = _project(tmp_path)
    _on()
    code, row, _ = ask(repo)
    home_copy = receipts_dir() / f"{row['run_id']}.json"
    assert Path(row["receipt"]) == home_copy and "project_receipt" not in row
    (repo / PROJECT_RECEIPTS / f"{row['run_id']}.json").unlink()
    assert main(["receipts", "verify", row["run_id"]]) == 0  # the home copy, with its journal


def test_the_flag_wins_over_the_config(tmp_path, ask):
    repo = _project(tmp_path)
    code, row, _ = ask(repo, "--project-receipt")
    assert (repo / PROJECT_RECEIPTS / f"{row['run_id']}.json").is_file()
    _on()
    code, row, _ = ask(repo, "--no-project-receipt")
    assert not (repo / PROJECT_RECEIPTS / f"{row['run_id']}.json").exists()


def test_json_names_the_copy(tmp_path, ask, capsys):
    repo = _project(tmp_path)
    _on()
    main(["ask", "-e", "claude", "--cwd", str(repo), "--json", "-q", SECRET_PROMPT])
    out = json.loads(capsys.readouterr().out)
    assert out["project_receipt"] == str(repo / PROJECT_RECEIPTS / f"{out['run_id']}.json")


@pytest.mark.parametrize("planted", [".orcha", ".orcha/receipts"])
def test_a_link_in_the_project_never_sends_the_copy_elsewhere(tmp_path, ask, planted):
    repo = _project(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (repo / planted).parent.mkdir(parents=True, exist_ok=True)
    (repo / planted).symlink_to(elsewhere, target_is_directory=True)
    _on()
    code, row, err = ask(repo)
    assert code == 0 and row is not None  # the run stands; its home copy counts
    assert list(elsewhere.rglob("*")) == []
    assert "no copy of the receipt in" in err and "a folder on the way is a link" in err
    assert "the receipt in the MetaBot home counts" in err


def test_a_dangling_link_creates_nothing_where_it_points(tmp_path, ask):
    repo = _project(tmp_path)
    (repo / ".orcha").symlink_to(tmp_path / "nowhere", target_is_directory=True)
    _on()
    code, row, err = ask(repo)
    assert code == 0 and row is not None
    assert not (tmp_path / "nowhere").exists()
    assert "a folder on the way is a link" in err


def test_a_file_already_there_is_never_replaced(tmp_path):
    repo = _project(tmp_path)
    receipt = tmp_path / "mb-x.json"
    receipt.write_text('{"a": 1}\n', encoding="utf-8")
    (repo / PROJECT_RECEIPTS).mkdir(parents=True)
    (repo / PROJECT_RECEIPTS / "mb-x.json").write_text("theirs\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="a file of that name is there already"):
        copy_to_project(receipt, repo)
    assert (repo / PROJECT_RECEIPTS / "mb-x.json").read_text() == "theirs\n"


def test_a_folder_gone_by_the_end_is_said_not_raised(tmp_path):
    receipt = tmp_path / "mb-x.json"
    receipt.write_text('{"a": 1}\n', encoding="utf-8")
    gone = tmp_path / "gone"
    copy, why = project_copy(receipt, gone)
    assert copy is None and why == f"no copy of the receipt in {gone / PROJECT_RECEIPTS}: " + (
        os.strerror(errno.ENOENT)
    )
    assert not gone.exists()


def test_a_failed_copy_is_said_even_when_quiet(tmp_path, ask):
    repo = _project(tmp_path)
    (repo / ".orcha").write_text("not a folder\n", encoding="utf-8")
    _on()
    code, row, err = ask(repo, "-q")
    assert code == 0 and "a folder on the way is not a folder" in err


def test_config_set_and_show(capsys):
    assert main(["config", "set", "project_receipts", "true"]) == 0
    assert load_config().project_receipts is True
    assert main(["config", "set", "fan_cap", "2"]) == 0 and load_config().fan_cap == 2
    capsys.readouterr()
    assert main(["config", "show"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert any(line.split()[:2] == ["project_receipts", "true"] for line in out)
    assert any(line.split()[:2] == ["fan_cap", "2"] for line in out)
    assert main(["config", "set", "project_receipts", "maybe"]) == 2
    assert main(["config", "set", "fan_cap", "9"]) == 2
    assert main(["config", "set", "fan_cap", "-1"]) == 2
    assert main(["config", "set", "roster", "{}"]) == 2
    err = capsys.readouterr().err
    assert "takes true or false" in err and "one of: project_receipts, fan_cap" in err
    assert load_config().project_receipts is True and load_config().fan_cap == 2


def test_a_hand_edited_setting_that_is_not_true_or_false_stops_the_run(tmp_path, ask, home):
    repo = _project(tmp_path)
    save_config(Config())
    path = home / "config.json"
    path.write_text(json.dumps({**json.loads(path.read_text()), "project_receipts": "yes"}))
    code, row, err = ask(repo)
    assert code == 1 and row is None and "project_receipts is not true or false" in err
    assert not (repo / ".orcha").exists()  # never read as on


# ── office runs and routines ────────────────────────────────────────────────


def test_an_office_run_copies_into_the_checkout_not_the_worktrees(tmp_path, capsys, monkeypatch):
    root = _trusted(tmp_path, {"tests": "test -f feature.txt"})
    _on()
    engines = {"codex": DeskEngine("codex", act=_build)}
    monkeypatch.setattr(office_run, "get_engine", lambda name: engines[name])
    code, out, err = _cli(capsys, "office", "run", "--dir", str(root), "--checks", "-q", TASK)
    assert code == 0, err
    office_id = next(w for w in out.split() if w.startswith("mb-"))
    copies = sorted(p.name for p in (root / PROJECT_RECEIPTS).iterdir())
    assert copies == [f"{office_id}.build.checks.json", f"{office_id}.build.json"]
    assert f"  project copies: 2 receipts in {root / PROJECT_RECEIPTS}" in out.splitlines()
    branch = f"metabot/{office_id}/build"
    assert ".orcha" not in _git(root, "ls-tree", "-r", "--name-only", branch)
    wt = Path(engines["codex"].jobs[0].cwd)
    assert wt.is_dir() and not (wt / ".orcha").exists()


def test_an_office_run_says_once_when_it_could_not_copy(tmp_path, capsys, monkeypatch):
    root = _trusted(tmp_path, {"tests": "test -f feature.txt"})
    (root / ".orcha").symlink_to(tmp_path, target_is_directory=True)
    _on()
    engines = {"codex": DeskEngine("codex", act=_build)}
    monkeypatch.setattr(office_run, "get_engine", lambda name: engines[name])
    code, out, err = _cli(capsys, "office", "run", "--dir", str(root), "--checks", "-q", TASK)
    assert code == 0, err
    said = [line for line in out.splitlines() if "no copy of the receipt" in line]
    assert said == [
        f"  no copy of the receipt in {root / PROJECT_RECEIPTS}: a folder on the way is a link;"
        " the receipts in the MetaBot home count"
    ]  # the desk's receipt and its checks' receipt, said once
    assert "project copies" not in out
    assert not (tmp_path / "receipts").exists()


async def test_a_routine_copies_into_its_folder(tmp_path):
    rid = save_routine(_spec(tmp_path, connections=[]), list_tools=_tools)
    _on()
    firing = await fire(rid, make_engine=lambda name: DeskEngine("openai"))
    copy = tmp_path / PROJECT_RECEIPTS / Path(firing.receipt).name
    assert copy.read_bytes() == Path(firing.receipt).read_bytes()
    assert verify_file(copy).valid
    assert os.stat(copy).st_mode & 0o777 == 0o600
