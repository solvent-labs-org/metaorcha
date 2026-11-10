"""``metabot propose``: rules choose a workflow's shape, and write a draft a person approves."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from metabot.cli import main
from metabot.config import Config, load_config, save_config
from metabot.models import save_probe
from metabot.propose import propose
from metabot.workflows import ABOUT_LIMIT, here_problems, parse_workflow

LOCAL = "http://127.0.0.1:11434/v1"
NOTHING = {"claude": None, "codex": None, "cursor": None}
CLAUDE = {**NOTHING, "claude": "/bin/claude"}
BANNED = re.compile(r"\b(first|only|nobody|trustless|web3|auto|autonomous)\b", re.I)


def _installed(monkeypatch, found: dict) -> None:
    monkeypatch.setattr("metabot.roster.available", lambda: dict(found))


def _here(monkeypatch, found: dict, *, reachable: bool) -> None:
    """What ``workflow check`` sees here: these CLIs, and the local server up or not."""
    monkeypatch.setattr("metabot.roster.here", lambda cfg: (dict(found), reachable))
    monkeypatch.setattr("metabot.ladder.here", lambda cfg: (dict(found), reachable))


def _cli(capsys, *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def _office(folder: Path, desks: dict | None = None, checks: bool = True) -> str:
    gate: dict = {"owner": ["verdict"], "engineer": ["tests"]}
    if checks:
        gate["checks"] = {"tests": "pytest -q"}
    data = {
        "name": "shop",
        "gates": {"ready": gate},
        "desks": desks or {"build": {"role": "build.md", "harness": "claude"}},
    }
    text = yaml.safe_dump(data, sort_keys=False)
    (folder / "office.yaml").write_text(text, encoding="utf-8")
    (folder / "build.md").write_text("You build.\n", encoding="utf-8")
    return hashlib.sha256(text.encode()).hexdigest()


def _rows(out: str, label: str) -> list[str]:
    return [line[9:] for line in out.splitlines() if line.startswith(f"{label:<8} ")]


# ── the shape, and the rule that chose it ───────────────────────────────────


def test_a_build_task_is_a_ladder_with_no_cap_invented(tmp_path, monkeypatch, capsys):
    _installed(monkeypatch, CLAUDE)
    code, out, err = _cli(capsys, "propose", "--folder", str(tmp_path), "Fix the weekday bug.")
    assert code == 0, err
    assert _rows(out, "shape") == ["ladder"]
    rules = _rows(out, "rule")
    assert rules[0] == 'build: the task says "Fix"'
    assert any("runs as a ladder" in r for r in rules)
    assert any("max_cost is left empty for you" in r for r in rules)
    assert _rows(out, "runs on") == ["ladder: rung 1 sub claude"]
    assert _rows(out, "missing") == []
    p = propose("Fix the weekday bug.", tmp_path, Config())
    assert p.data["name"] == "fix-the-weekday-bug" and p.data["cwd"] == str(tmp_path)
    assert p.data["steps"] == [
        {"name": "ladder", "kind": "ladder", "prompt": "Fix the weekday bug.", "checks": [],
         "allow_write": False, "allow": [], "max_cost": None},
    ]  # fmt: skip
    assert "next     metabot workflow check FILE" in out
    assert "next     metabot workflow approve fix-the-weekday-bug" in out


def test_a_review_task_is_one_read_only_ask_on_the_review_role(tmp_path, capsys):
    roster = {"review": {"engine": "codex", "model": "gpt-5", "login": "work"}}
    save_config(Config(roster=roster))
    code, out, err = _cli(capsys, "propose", "--folder", str(tmp_path), "Review", "my", "patch")
    assert code == 0, err
    assert _rows(out, "shape") == ["ask"]
    assert _rows(out, "rule")[0] == 'review: the task says "Review"'
    assert any(
        "reads and changes nothing, so the step is read_only" in r for r in _rows(out, "rule")
    )
    assert _rows(out, "runs on") == ["ask: codex/gpt-5, role review, login work"]
    [step] = propose("Review my patch", tmp_path, Config(roster=roster)).data["steps"]
    assert step == {
        "name": "ask", "kind": "ask", "prompt": "Review my patch", "role": "review",
        "engine": "codex", "model": "gpt-5", "login": "work", "read_only": True,
        "allow_write": False, "allow": [], "max_cost": None,
    }  # fmt: skip


def test_a_role_with_no_engine_here_falls_back_and_says_so(tmp_path):
    p = propose("audit the login flow", tmp_path, Config(roster={"build": {"engine": "claude"}}))
    [step] = p.data["steps"]
    assert (step["role"], step["engine"], step["read_only"]) == ("build", "claude", True)
    assert "the review role has no engine here, so the build role instead" in p.rules


def test_no_engine_here_is_named_as_missing(tmp_path, capsys):
    code, out, _err = _cli(capsys, "propose", "--folder", str(tmp_path), "explain this error")
    assert code == 0
    [step] = propose("explain this error", tmp_path, Config()).data["steps"]
    assert step["role"] == "fast" and "engine" not in step
    [missing] = _rows(out, "missing")
    assert "no engine for the fast role here" in missing
    assert "ask: -, role fast, no login" in _rows(out, "runs on")


def test_a_folder_with_an_office_is_an_office_run_of_its_desks(tmp_path, monkeypatch, capsys):
    _installed(monkeypatch, CLAUDE)
    sha = _office(tmp_path)
    code, out, err = _cli(capsys, "propose", "--folder", str(tmp_path), "Ship the release.")
    assert code == 0, err
    assert _rows(out, "shape") == ["office"]
    rules = _rows(out, "rule")
    assert any("the folder holds an office.yaml (office shop)" in r for r in rules)
    assert any("the office's gates name 1 check, so the step runs them" in r for r in rules)
    assert _rows(out, "runs on") == ["office: desk build on claude, no login"]
    [step] = propose("Ship the release.", tmp_path, Config()).data["steps"]
    assert step == {
        "name": "office", "kind": "office", "prompt": "Ship the release.", "dir": str(tmp_path),
        "office_sha256": sha, "checks": True, "allow_write": False, "allow": [],
        "max_cost": None,
    }  # fmt: skip


def test_an_office_is_not_proposed_for_a_quick_look(tmp_path, monkeypatch):
    _installed(monkeypatch, CLAUDE)
    _office(tmp_path)
    p = propose("summarize the thread", tmp_path, Config())
    assert p.shape == "ask"


def test_an_office_that_does_not_read_is_missing_and_no_office_is_proposed(tmp_path, monkeypatch):
    _installed(monkeypatch, CLAUDE)
    (tmp_path / "office.yaml").write_text("name: shop\n", encoding="utf-8")
    p = propose("build it", tmp_path, Config())
    assert p.shape == "ladder"
    assert any(r.startswith("the folder's office.yaml does not read") for r in p.rules)
    assert any(r.endswith("so no office run is proposed") for r in p.rules)


# ── private work stays on this machine ──────────────────────────────────────


@pytest.mark.parametrize(
    "setup",
    ["private role leaves", "office of cloud desks", "ladder of cloud rungs", "nothing here"],
)
def test_a_private_task_is_never_proposed_onto_an_engine_that_leaves(tmp_path, monkeypatch, setup):
    _installed(monkeypatch, {"claude": "/bin/claude", "codex": "/bin/codex", "cursor": None})
    cfg = Config()
    if setup == "private role leaves":
        cfg = Config(roster={"private": {"engine": "claude"}, "build": {"engine": "codex"}})
    elif setup == "office of cloud desks":
        _office(tmp_path)
    elif setup == "ladder of cloud rungs":
        cfg = Config(ladder=[{"engine": "claude"}, {"engine": "codex", "model": "gpt-5"}])
    else:
        _installed(monkeypatch, NOTHING)
    p = propose("fix the confidential contract", tmp_path, cfg)
    assert p.shape == "ladder" and p.data["name"] == "private-task"
    [step] = p.data["steps"]
    assert step["role"] == "private" and "engine" not in step
    for name in ("claude", "codex", "cursor"):
        assert name.encode() not in p.raw  # the draft names no engine that leaves
    assert all("skipped: this engine sends the work off this machine" in r for r in p.runs_on)
    assert any("no rung here keeps the work on this machine" in m for m in p.missing)
    assert "Before it runs: no rung here keeps the work on this machine" in p.data["about"]
    if setup == "office of cloud desks":
        assert any("not proposed onto it" in r for r in p.rules)
    assert parse_workflow(p.raw).steps[0].role == "private"


def test_workflow_check_refuses_a_private_ladder_with_no_rung_that_stays(
    tmp_path, monkeypatch, capsys
):
    found = {"claude": "/bin/claude", "codex": "/bin/codex", "cursor": None}
    _installed(monkeypatch, found)
    _here(monkeypatch, found, reachable=False)
    save_config(Config(ladder=[{"engine": "claude"}, {"engine": "codex", "model": "gpt-5"}]))
    p = propose("fix the confidential contract", tmp_path, load_config())
    [problem] = here_problems(parse_workflow(p.raw))
    assert "no rung here keeps the work on this machine" in problem
    draft = tmp_path / "draft.json"
    draft.write_bytes(p.raw)
    code, _out, err = _cli(capsys, "workflow", "check", str(draft))
    assert code != 0 and "no rung here keeps the work on this machine" in err + _out


def test_workflow_check_passes_a_private_ladder_with_a_rung_that_stays(tmp_path, monkeypatch):
    _installed(monkeypatch, CLAUDE)
    _here(monkeypatch, CLAUDE, reachable=True)
    save_config(Config(openai_base_url=LOCAL, openai_model="qwen2.5:3b"))
    save_probe(LOCAL, "qwen2.5:3b", "native", "called read_file")
    p = propose("keep this private: fix it", tmp_path, load_config())
    assert here_problems(parse_workflow(p.raw)) == []


def test_a_private_task_runs_on_the_probed_local_model(tmp_path, monkeypatch):
    _installed(monkeypatch, CLAUDE)
    cfg = Config(openai_base_url=LOCAL, openai_model="qwen2.5:3b")
    save_probe(LOCAL, "qwen2.5:3b", "native", "called read_file")
    p = propose("keep this private: fix it", tmp_path, cfg)
    assert p.runs_on == (
        "ladder: rung 1 local openai/qwen2.5:3b",
        "ladder: rung 2 sub claude, skipped: this engine sends the work off this machine",
    )
    assert p.missing == ()
    assert any("its last probe answered" in r for r in p.rules)


def test_the_local_model_is_not_taken_as_here_without_a_probe(tmp_path):
    p = propose("keep this private", tmp_path, Config(openai_base_url=LOCAL))
    assert p.runs_on == ()
    assert any("no rung here keeps the work on this machine" in m for m in p.missing)


# ── money: a cap is the person's ────────────────────────────────────────────


def test_a_paid_engine_needs_a_cap_and_the_draft_says_so(tmp_path, capsys):
    cfg = Config(
        openai_base_url="https://api.example.invalid/v1", roster={"review": {"engine": "openai"}}
    )
    save_config(cfg)
    code, out, _err = _cli(capsys, "propose", "--folder", str(tmp_path), "--json", "review it")
    assert code == 0
    data = json.loads(out)
    [step] = data["draft"]["steps"]
    assert step["engine"] == "openai" and step["max_cost"] is None
    [missing] = data["missing"]
    assert missing.startswith("max_cost in step ask: it runs on a paid engine")
    assert 'set "max_cost" in the step' in missing
    assert "max_cost" in data["draft"]["about"] and "set" in data["draft"]["about"]


def test_a_ladder_with_a_paid_rung_needs_a_cap(tmp_path):
    cfg = Config(
        openai_base_url="https://api.example.invalid/v1", openai_model="big",
        ladder=[{"engine": "openai"}],
    )  # fmt: skip
    p = propose("add a flag", tmp_path, cfg)
    assert p.data["steps"][0]["max_cost"] is None
    assert any(
        m.startswith("max_cost in step ladder: it may run on a paid rung") for m in p.missing
    )


# ── where the draft goes ────────────────────────────────────────────────────


def test_out_writes_the_draft_and_never_over_a_file(tmp_path, monkeypatch, capsys):
    _installed(monkeypatch, CLAUDE)
    out_file = tmp_path / "draft.json"
    code, out, err = _cli(
        capsys, "propose", "--folder", str(tmp_path), "--out", str(out_file), "fix it"
    )
    assert code == 0, err
    raw = out_file.read_bytes()
    assert raw == propose("fix it", tmp_path, Config()).raw
    assert os.stat(out_file).st_mode & 0o777 == 0o600
    sha = hashlib.sha256(raw).hexdigest()
    assert f"draft    {out_file}, sha256 {sha}, not kept and not approved" in out
    assert f"next     metabot workflow check {out_file}" in out
    assert f"next     metabot workflow add {out_file}" in out
    code, _out, err = _cli(
        capsys, "propose", "--folder", str(tmp_path), "--out", str(out_file), "build it"
    )
    assert code == 2 and "is there already" in err
    assert out_file.read_bytes() == raw
    link = tmp_path / "link.json"
    link.symlink_to(tmp_path / "nowhere.json")
    code, _out, err = _cli(capsys, "propose", "--folder", str(tmp_path), "--out", str(link), "x")
    assert code == 2 and not (tmp_path / "nowhere.json").exists()


def test_out_is_never_in_metabots_home(tmp_path, home, capsys):
    home.mkdir(parents=True, exist_ok=True)
    target = home / "receipts" / "wfa-draft.json"
    target.parent.mkdir()
    code, _out, err = _cli(capsys, "propose", "--folder", str(tmp_path), "--out", str(target), "x")
    assert code == 2 and "MetaBot's home" in err
    assert not target.exists()


def test_without_out_the_draft_is_printed_whole(tmp_path, monkeypatch, capsys):
    _installed(monkeypatch, CLAUDE)
    code, out, _err = _cli(capsys, "propose", "--folder", str(tmp_path), "fix it")
    assert code == 0
    head, _, rest = out.partition("draft    not written; --out FILE writes it:\n")
    assert rest
    draft = rest.split("\nnext     ", 1)[0] + "\n"
    assert draft.encode() == propose("fix it", tmp_path, Config()).raw


def test_json_prints_one_object_with_the_same_facts(tmp_path, monkeypatch, capsys):
    _installed(monkeypatch, CLAUDE)
    out_file = tmp_path / "d.json"
    code, out, _err = _cli(
        capsys, "propose", "--json", "--folder", str(tmp_path), "--out", str(out_file), "fix it"
    )
    assert code == 0
    data = json.loads(out)
    p = propose("fix it", tmp_path, Config())
    assert data == {
        "shape": "ladder",
        "name": "fix-it",
        "rules": list(p.rules),
        "runs_on": ["ladder: rung 1 sub claude"],
        "missing": [],
        "draft": json.loads(p.raw),
        "sha256": hashlib.sha256(p.raw).hexdigest(),
        "path": str(out_file),
        "next": [
            f"metabot workflow check {out_file}",
            f"metabot workflow add {out_file}",
            "metabot workflow approve fix-it",
        ],
    }


# ── the same task, the same bytes ───────────────────────────────────────────


def test_the_same_task_and_config_give_the_same_bytes(tmp_path, monkeypatch, capsys):
    _installed(monkeypatch, CLAUDE)
    (tmp_path / "o").mkdir()
    _office(tmp_path / "o")
    outs = []
    for n in (1, 2):
        target = tmp_path / f"d{n}.json"
        for folder in (tmp_path, tmp_path / "o"):
            for task in ("fix the bug", "review it", "keep it private"):
                code, out, _err = _cli(capsys, "propose", "--json", "--folder", str(folder), task)
                assert code == 0
                outs.append((n, folder, task, out))
        _cli(capsys, "propose", "--folder", str(tmp_path / "o"), "--out", str(target), "ship")
    first = {(f, t): o for n, f, t, o in outs if n == 1}
    assert all(first[(f, t)] == o for n, f, t, o in outs if n == 2)
    assert (tmp_path / "d1.json").read_bytes() == (tmp_path / "d2.json").read_bytes()


# ── names, refusals, words ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("task", "name"),
    [
        ("Fix the weekday bug.", "fix-the-weekday-bug"),
        ("add a --json flag to the list command now", "add-a-json-flag-to"),
        ("ñandú", "proposed"),
        ("deadbeefcafe0123", "task-deadbeefcafe0123"),
    ],
)
def test_the_name_comes_from_the_tasks_words(tmp_path, task, name):
    p = propose(task, tmp_path, Config())
    assert p.data["name"] == name
    parse_workflow(p.raw)


def test_an_empty_task_a_long_one_or_a_folder_that_is_not_one_is_refused(tmp_path, capsys):
    assert _cli(capsys, "propose", "--folder", str(tmp_path), " ")[0] == 2
    assert _cli(capsys, "propose", "--folder", str(tmp_path / "no"), "fix")[0] == 2
    code, _out, err = _cli(capsys, "propose", "--folder", str(tmp_path), "x" * 50_001)
    assert code == 2 and "longer than" in err


def test_the_printed_lines_keep_the_public_copy_rules(tmp_path, monkeypatch, capsys):
    _installed(monkeypatch, CLAUDE)
    save_probe(LOCAL, "qwen2.5:3b", "none", "no tool calls")
    save_config(Config(openai_base_url=LOCAL, roster={"review": {"engine": "openai"}}))
    (tmp_path / "o").mkdir()
    _office(tmp_path / "o")
    for folder in (tmp_path, tmp_path / "o"):
        for task in ("fix it", "review it", "keep it private", "explain it", "hello"):
            _code, out, err = _cli(capsys, "propose", "--folder", str(folder), task)
            said = out.split("draft    ")[0] + err
            assert not BANNED.search(said), said
            assert not re.search(r"(?<![\w./:-])Orcha(?![\w-])", said)


# ── review fixes: the office the run reads, the draft check reads ───────────


def _git(repo: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, env=env, check=True
    )
    return proc.stdout


def _repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.name", "MetaBot Test")
    _git(path, "config", "user.email", "metabot-test@example.invalid")
    return path


def _commit(repo: Path) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "office")


def _propose_without_git(monkeypatch, *argv: str) -> int:
    def no_git(*_args, **_kwargs):
        raise AssertionError("propose ran git")

    with monkeypatch.context() as m:
        m.setattr("metabot.gitcmd.git", no_git)
        return main(["propose", *argv])


def test_in_a_linked_worktree_the_draft_names_the_office_the_run_reads(
    tmp_path, monkeypatch, capsys
):
    _installed(monkeypatch, CLAUDE)
    repo = _repo(tmp_path / "repo")
    sha = _office(repo)
    _commit(repo)
    _git(repo, "worktree", "add", "-q", "-b", "side", str(tmp_path / "wt"))
    wt = tmp_path / "wt"
    text = (wt / "office.yaml").read_text(encoding="utf-8").replace("shop", "shop2")
    (wt / "office.yaml").write_text(text, encoding="utf-8")
    out_file = tmp_path / "wt.json"
    assert (
        _propose_without_git(monkeypatch, "--folder", str(wt), "--out", str(out_file), "fix it")
        == 0
    )
    capsys.readouterr()
    [step] = json.loads(out_file.read_bytes())["steps"]
    assert (step["dir"], step["office_sha256"]) == (str(repo.resolve()), sha)
    code = main(["workflow", "check", str(out_file)])
    out, err = capsys.readouterr()
    assert code == 0 and "it can run here" in out, err


def test_an_office_in_a_subfolder_reads_its_roles_from_the_project(tmp_path, monkeypatch, capsys):
    _installed(monkeypatch, CLAUDE)
    repo = _repo(tmp_path / "repo")
    (repo / "roles").mkdir()
    (repo / "roles" / "build.md").write_text("You build.\n", encoding="utf-8")
    (repo / "app").mkdir()
    _office(repo / "app", {"build": {"role": "../roles/build.md", "harness": "claude"}})
    _commit(repo)
    out_file = tmp_path / "app.json"
    argv = ("--folder", str(repo / "app"), "--out", str(out_file), "--json", "fix the bug")
    assert _propose_without_git(monkeypatch, *argv) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["shape"] == "office" and data["missing"] == []
    assert "Before it runs" not in data["draft"]["about"]
    code = main(["workflow", "check", str(out_file)])
    out, err = capsys.readouterr()
    assert code == 0 and "it can run here" in out, err


def test_a_draft_too_large_for_a_workflow_is_refused_and_not_written(tmp_path, monkeypatch, capsys):
    _installed(monkeypatch, CLAUDE)
    task = "fix " + "\x01" * 49_990  # each one is six bytes in JSON
    monkeypatch.setattr("sys.stdin", io.StringIO(task))
    out_file = tmp_path / "big.json"
    code, out, err = _cli(capsys, "propose", "--folder", str(tmp_path), "--out", str(out_file), "-")
    assert code == 2 and "larger than" in err and out == ""
    assert not out_file.exists()
    monkeypatch.setattr("sys.stdin", io.StringIO(task))
    code, out, err = _cli(capsys, "propose", "--folder", str(tmp_path), "-")
    assert code == 2 and "larger than" in err and out == ""


@pytest.mark.parametrize("depth", [0, 1])
def test_out_is_never_the_home_or_a_folder_that_holds_it(tmp_path, monkeypatch, capsys, depth):
    holder = tmp_path / "state"
    kept = holder / "metabot"
    monkeypatch.setenv("METABOT_HOME", str(kept))
    if depth == 0:
        holder.mkdir()  # the home is not made yet, the folder that holds it is
    target = kept if depth == 0 else holder
    code, _out, err = _cli(capsys, "propose", "--folder", str(tmp_path), "--out", str(target), "x")
    assert code == 2 and "MetaBot's home" in err
    assert not target.exists()


def test_the_cap_note_stays_in_the_about_however_long_the_rest_is(tmp_path, capsys):
    cfg = Config(
        openai_base_url="https://api.example.invalid/v1", openai_model="big",
        roster={"build": {"engine": "openai", "model": "big"}},
    )  # fmt: skip
    desks = {f"d{n}": {"role": f"r{n}{'x' * 200}.md", "harness": "any"} for n in range(12)}
    _office(tmp_path, desks)
    p = propose("fix it", tmp_path, cfg)
    assert p.shape == "office"
    assert any(
        m.startswith("max_cost in step office: desk d0 runs on a paid engine") for m in p.missing
    )
    about = p.data["about"]
    assert len(about) <= ABOUT_LIMIT and about.endswith("…")
    assert "max_cost in step office" in about and 'set "max_cost"' in about
    parse_workflow(p.raw)


def test_a_paid_desk_needs_a_cap(tmp_path):
    cfg = Config(
        openai_base_url="https://api.example.invalid/v1",
        roster={"build": {"engine": "openai", "model": "big"}},
    )
    _office(tmp_path, {"build": {"role": "build.md", "harness": "any"}})
    p = propose("fix it", tmp_path, cfg)
    assert p.shape == "office"
    assert [m for m in p.missing if "max_cost" in m] == [
        "max_cost in step office: desk build runs on a paid engine (openai, billed by use); "
        'set "max_cost" in the step to the most one run may spend, in dollars, such as "2.00"'
    ]
    assert not any("no step here runs on a paid engine" in r for r in p.rules)


def test_rungs_past_the_attempts_do_not_run_and_ask_no_cap(tmp_path, monkeypatch):
    _installed(monkeypatch, CLAUDE)
    cfg = Config(
        openai_base_url="https://api.example.invalid/v1", openai_model="big",
        ladder=[{"engine": "claude"}, {"engine": "openai"}], ladder_attempts=1,
    )  # fmt: skip
    p = propose("add a flag", tmp_path, cfg)
    assert p.runs_on == (
        "ladder: rung 1 sub claude",
        "ladder: rung 2 paid openai/big, past the 1 rung(s) one task runs",
    )
    assert not any("max_cost" in m for m in p.missing)


def test_a_terminal_gets_the_draft_with_its_controls_escaped(tmp_path, monkeypatch, capsys):
    _installed(monkeypatch, CLAUDE)
    task = "fix it \x9b2J ‮dlrow"
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    code, out, _err = _cli(capsys, "propose", "--folder", str(tmp_path), task)
    assert code == 0
    assert "\x9b" not in out and "‮" not in out
    assert "\\x9b2J" in out and "\\u202edlrow" in out


@pytest.mark.parametrize("where", ["task", "folder", "out"])
def test_a_name_that_is_not_utf8_is_refused_cleanly(tmp_path, monkeypatch, capsys, where):
    _installed(monkeypatch, CLAUDE)
    odd = os.fsdecode(b"caf\xe9")
    folder, out_file, task = tmp_path, tmp_path / "d.json", "fix it"
    if where == "task":
        task = f"fix {odd} bug"
    elif where == "folder":
        folder = tmp_path / odd
        folder.mkdir()
    else:
        out_file = tmp_path / f"{odd}.json"
    for extra in ((), ("--json",)):
        argv = ("propose", "--folder", str(folder), "--out", str(out_file), *extra, task)
        code, out, err = _cli(capsys, *argv)
        assert code == 2 and "not UTF-8" in err and out == "", err
        assert not out_file.exists()


@pytest.mark.parametrize("setup", ["rung login", "rung endpoint", "desk login"])
def test_what_does_not_read_is_said_in_the_public_copy(tmp_path, monkeypatch, capsys, setup):
    _installed(monkeypatch, {**CLAUDE, "cursor": "/bin/cursor"})
    if setup == "rung login":
        save_config(Config(ladder=[{"engine": "openai", "model": "m", "login": "work"}]))
    elif setup == "rung endpoint":
        save_config(Config(ladder=[{"engine": "claude", "endpoint": "x"}]))
    else:
        save_config(Config(logins={"work": {"engine": "claude", "home": str(tmp_path / "l")}}))
        _office(tmp_path, {"build": {"role": "build.md", "harness": "cursor", "login": "work"}})
    code, out, err = _cli(capsys, "propose", "--folder", str(tmp_path), "fix the bug")
    assert code == 0
    assert _rows(out, "missing")
    said = out.split("draft    ")[0] + err
    assert not BANNED.search(said), said
    draft = json.loads(propose("fix the bug", tmp_path, load_config()).raw)
    assert not BANNED.search(draft["about"]), draft["about"]
