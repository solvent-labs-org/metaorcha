"""``metabot workflow new``, ``check``, ``show`` and ``list``: the store, from the command line."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from metabot.cli import main
from metabot.config import Config, save_config
from metabot.workflows import FORMAT, load_workflow, resolve, stored_workflows


def _cli(capsys, *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def _file(tmp_path, name: str = "date-fix", **top) -> str:
    data = {
        "format": FORMAT,
        "name": name,
        "cwd": str(tmp_path),
        "steps": [{"name": "build", "kind": "ask", "prompt": "Fix it.", "engine": "openai"}],
        **top,
    }
    path = tmp_path / f"{name}.workflow.json"
    path.write_text(json.dumps(data, indent=1), encoding="utf-8")
    return str(path)


def test_new_makes_a_one_step_workflow_from_the_ask_flags(tmp_path, capsys):
    code, out, err = _cli(
        capsys, "workflow", "new", "date-fix", "-e", "openai", "-m", "tiny", "--cwd",
        str(tmp_path), "--allow", "Edit", "--max-cost", "1.50", "Fix", "the", "weekday", "bug.",
    )  # fmt: skip
    assert code == 0, err
    wf = load_workflow("date-fix")
    assert f"stored workflow date-fix {wf.sha256}" in out
    assert "not approved" in out and "next: metabot workflow show date-fix" in out
    data = json.loads(wf.raw)
    assert data == {
        "format": FORMAT,
        "name": "date-fix",
        "cwd": str(tmp_path),
        "steps": [
            {"name": "ask", "kind": "ask", "prompt": "Fix the weekday bug.", "engine": "openai",
             "model": "tiny", "allow": ["Edit"], "max_cost": "1.50"},
        ],
    }  # fmt: skip


def test_new_makes_a_ladder_or_an_office_step(tmp_path, capsys):
    code, _out, err = _cli(
        capsys, "workflow", "new", "climb", "--ladder", "--check", "pytest -q", "--cwd",
        str(tmp_path), "Make", "it", "pass.",
    )  # fmt: skip
    assert code == 0, err
    [step] = load_workflow("climb").steps
    assert (step.kind, step.ladder_checks) == ("ladder", ("pytest -q",))
    office = tmp_path / "office"
    office.mkdir()
    yaml_text = (
        "name: shop\ngates: {ready: {owner: [verdict]}}\n"
        "desks: {build: {harness: claude, role: build.md}}\n"
    )
    (office / "office.yaml").write_text(yaml_text, encoding="utf-8")
    (office / "build.md").write_text("You build.\n", encoding="utf-8")
    code, _out, err = _cli(
        capsys, "workflow", "new", "desks", "--office", str(office), "--desks", "build",
        "--until-checks", "--rounds", "2", "--cwd", str(tmp_path), "Ship", "it.",
    )  # fmt: skip
    assert code == 0, err
    [step] = load_workflow("desks").steps
    assert step.kind == "office" and step.desks == ("build",) and step.rounds == 2
    assert step.office_sha256 == hashlib.sha256(yaml_text.encode()).hexdigest()


@pytest.mark.parametrize(
    ("argv", "said"),
    [
        (["--ladder", "--login", "work"], "--login goes with an ask step"),
        (["--check", "pytest"], "--check goes with --ladder"),
        (["--ladder", "--office", "/x"], "--ladder or --office, not both"),
        (["--desks", "a"], "--desks goes with --office"),
        (["--cron", "0 9 * * *"], "ceiling: a workflow with a schedule needs one"),
        (["--firings", "0"], "ceiling.firings"),
        (["--read-only", "--allow-write"], "read_only"),
    ],
)
def test_new_refuses_flags_that_do_not_make_a_workflow(tmp_path, capsys, argv, said):
    code, _out, err = _cli(capsys, "workflow", "new", "w", "--cwd", str(tmp_path), *argv, "Go.")
    assert code == 2 and said in err
    assert stored_workflows() == []


def test_new_with_a_file_stores_its_exact_bytes(tmp_path, capsys):
    path = _file(tmp_path)
    raw = Path(path).read_bytes()
    code, out, err = _cli(capsys, "workflow", "add", path)
    assert code == 0, err
    assert resolve("date-fix") == hashlib.sha256(raw).hexdigest()
    assert load_workflow("date-fix").raw == raw
    assert "stored workflow date-fix" in out


def test_add_refuses_a_file_that_is_not_a_workflow_and_keeps_nothing(tmp_path, capsys):
    path = tmp_path / "w.json"
    path.write_text(json.dumps({"format": FORMAT, "name": "w"}))
    code, _out, err = _cli(capsys, "workflow", "add", str(path))
    assert code == 2 and "workflow not kept: cwd: an absolute path" in err
    code, _out, err = _cli(capsys, "workflow", "add", str(tmp_path / "missing.json"))
    assert code == 2 and "cannot read" in err
    assert stored_workflows() == []


def test_new_with_flags_between_the_name_and_the_prompt(tmp_path, capsys):
    code, _out, err = _cli(
        capsys, "workflow", "new", "w", "--cwd", str(tmp_path), "-e", "openai", "Say", "hi."
    )
    assert code == 0, err
    assert load_workflow("w").steps[0].prompt == "Say hi."


def test_a_new_version_says_the_one_it_replaces(tmp_path, capsys):
    assert _cli(capsys, "workflow", "add", _file(tmp_path))[0] == 0
    old = resolve("date-fix")
    code, out, _err = _cli(capsys, "workflow", "add", _file(tmp_path, about="v2"))
    assert code == 0
    assert f"date-fix now finds this version; {old[:12]} stays, by its sha256" in out


def test_check_says_where_a_file_is_wrong(tmp_path, capsys):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"format": FORMAT, "name": "x", "cwd": "rel", "steps": []}))
    code, _out, err = _cli(capsys, "workflow", "check", str(path))
    assert code == 1 and "not a workflow: cwd: an absolute path" in err


def test_check_a_good_file_names_its_sha256_and_what_stops_it_here(tmp_path, capsys):
    path = _file(tmp_path)
    code, out, err = _cli(capsys, "workflow", "check", path)
    assert code == 0, err
    sha = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    assert f"workflow date-fix {sha}" in out and "it can run here" in out
    assert f"next: metabot workflow add {path}" in out
    assert stored_workflows() == []  # a check stores nothing
    gone = _file(tmp_path, name="gone", cwd=str(tmp_path / "nowhere"))
    code, out, err = _cli(capsys, "workflow", "check", gone)
    assert code == 1 and "cannot run here" in err and "nowhere is not a folder" in err


def test_check_reads_a_stored_workflow_again(tmp_path, capsys):
    assert _cli(capsys, "workflow", "add", _file(tmp_path))[0] == 0
    code, out, _err = _cli(capsys, "workflow", "check", "date-fix")
    assert code == 0 and "it can run here" in out
    load_workflow("date-fix").path.write_text("{}")
    code, _out, err = _cli(capsys, "workflow", "check", "date-fix")
    assert code == 1 and "changed since it was stored" in err


def test_show_names_what_each_step_runs_on(tmp_path, capsys):
    save_config(Config(openai_base_url="http://127.0.0.1:9/v1", openai_model="tiny"))
    data = {
        "format": FORMAT,
        "name": "nightly",
        "about": "Nightly fix loop.",
        "cwd": str(tmp_path),
        "steps": [
            {"name": "build", "kind": "ask", "prompt": "Fix it.", "engine": "openai",
             "allow": ["Edit"], "max_cost": "2"},
            {"name": "climb", "kind": "ladder", "prompt": "Pass.", "checks": ["pytest -q"],
             "read_only": True},
        ],
        "schedule": {"cron": "0 9 * * 1-5", "tz": "Europe/London"},
        "ceiling": {"firings": 40, "expires": "2099-12-31", "month_cap": "20"},
    }  # fmt: skip
    (tmp_path / "w.json").write_text(json.dumps(data))
    assert _cli(capsys, "workflow", "add", str(tmp_path / "w.json"))[0] == 0
    code, out, err = _cli(capsys, "workflow", "show", "nightly")
    assert code == 0, err
    wf = load_workflow("nightly")
    assert f"sha256    {wf.sha256}" in out and "Nightly fix loop." in out
    assert "build (ask): openai, model tiny, endpoint 127.0.0.1:9, no login" in out
    assert "each write asks; runs without asking: Edit; money cap $2.00 a run" in out
    assert (
        "climb (ladder):" in out
        and "check: pytest -q" in out
        and "reads and changes nothing" in out
    )
    assert "0 9 * * 1-5 (Europe/London)" in out and out.count(" 09:00 ") == 5
    assert "ceiling   40 firings; expires 2099-12-31; $20.00 a month" in out
    assert "runs until you change it, turn it off, or a ceiling is reached" in out
    assert "serve fires it once a routine names it: metabot workflow schedule nightly" in out


def test_show_raw_prints_the_exact_bytes(tmp_path, capsys):
    path = _file(tmp_path)
    assert _cli(capsys, "workflow", "add", path)[0] == 0
    code, out, _err = _cli(capsys, "workflow", "show", "--raw", "date-fix")
    assert code == 0 and out == Path(path).read_text(encoding="utf-8")


def test_list_shows_each_version_and_which_one_its_name_finds(tmp_path, capsys):
    code, out, _err = _cli(capsys, "workflow", "list")
    assert code == 0 and out.startswith("no workflows; make one with: metabot workflow new")
    assert _cli(capsys, "workflow", "add", _file(tmp_path))[0] == 0
    old = resolve("date-fix")
    assert _cli(capsys, "workflow", "add", _file(tmp_path, about="v2"))[0] == 0
    new = resolve("date-fix")
    code, out, _err = _cli(capsys, "workflow", "list")
    rows = out.splitlines()
    assert code == 0 and len(rows) == 2
    assert rows[0].startswith("date-fix") and old[:12] in rows[0] and "older version" in rows[0]
    assert new[:12] in rows[1] and "older version" not in rows[1] and "1 step" in rows[1]


@pytest.mark.parametrize("action", ["show", "check"])
def test_a_workflow_that_is_not_there_is_named(capsys, action):
    code, _out, err = _cli(capsys, "workflow", action, "nothing")
    assert code == 2 and "no workflow named 'nothing'" in err
