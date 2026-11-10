"""The routing core's boundary: ``metabot propose`` drafts a workflow, and that is all it does.

Models propose, rules decide, and the proposer is rules alone. It never starts an engine, a
process or a network connection; it never writes an approval, a firing, a routine,
``config.json`` or a login; without ``--out`` it writes nothing at all, and with it never over
a file that is there. Its draft is read by the same check a file written by hand is.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import urllib.request
from pathlib import Path

import pytest
import yaml

from metabot.cli import main
from metabot.config import Config, save_config
from metabot.models import save_probe
from metabot.propose import propose
from metabot.workflows import FORMAT, WorkflowError, here_problems, parse_workflow

LOCAL = "http://127.0.0.1:11434/v1"
TASKS = ("fix the weekday bug", "review my patch", "keep the contract private", "hello")


def _office(folder: Path) -> None:
    data = {
        "name": "shop",
        "gates": {"ready": {"owner": ["tests"], "checks": {"tests": "pytest -q"}}},
        "desks": {"build": {"role": "build.md", "harness": "any", "login": "work"}},
    }
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "office.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    (folder / "build.md").write_text("You build.\n", encoding="utf-8")


@pytest.fixture
def machine(tmp_path, home, monkeypatch) -> Path:
    """A machine with an engine CLI on PATH, a probed local model, a roster with a login,
    and an office: every way a proposal could reach for an engine is set up."""
    monkeypatch.setattr(
        "metabot.roster.available", lambda: {"claude": "/bin/claude", "codex": None, "cursor": None}
    )
    save_probe(LOCAL, "qwen2.5:3b", "native", "called read_file")
    save_config(
        Config(
            openai_base_url=LOCAL,
            roster={"review": {"engine": "claude", "login": "work"}, "build": {"engine": "claude"}},
            logins={"work": {"engine": "claude", "home": str(tmp_path / "login")}},
        )
    )
    _office(tmp_path / "office")
    return tmp_path


def _tree(root: Path) -> dict[str, tuple[int, bytes | None]]:
    """Every path under ``root``: its mode, and a file's bytes."""
    out: dict[str, tuple[int, bytes | None]] = {}
    if not root.exists():
        return out
    for path in sorted(root.rglob("*")):
        st = path.lstat()
        data = path.read_bytes() if path.is_file() and not path.is_symlink() else None
        out[str(path.relative_to(root))] = (st.st_mode, data)
    return out


def _runs(machine: Path, capsys, *extra: str) -> None:
    for folder in (machine, machine / "office"):
        for task in TASKS:
            for json_flag in ((), ("--json",)):
                code = main(["propose", "--folder", str(folder), *json_flag, *extra, task])
                assert code == 0, capsys.readouterr().err
    capsys.readouterr()


# ── (a) it starts nothing and contacts nothing ──────────────────────────────


def test_propose_never_starts_an_engine_a_process_or_a_network_connection(
    machine, monkeypatch, capsys
):
    reached: list[str] = []

    def canary(what):
        def call(*args, **kwargs):
            reached.append(what)
            raise AssertionError(f"propose reached {what}")

        return call

    monkeypatch.setattr(subprocess.Popen, "_execute_child", canary("a process"))
    monkeypatch.setattr(os, "system", canary("os.system"))
    monkeypatch.setattr(os, "posix_spawn", canary("os.posix_spawn"))
    monkeypatch.setattr(os, "posix_spawnp", canary("os.posix_spawnp"))
    monkeypatch.setattr(os, "fork", canary("os.fork"))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", canary("an asyncio process"))
    monkeypatch.setattr(socket.socket, "connect", canary("a connection"))
    monkeypatch.setattr(socket.socket, "connect_ex", canary("a connection"))
    monkeypatch.setattr(socket, "create_connection", canary("a connection"))
    monkeypatch.setattr(socket, "getaddrinfo", canary("a lookup"))
    monkeypatch.setattr(urllib.request, "urlopen", canary("a request"))
    # what would reach for them: the roster's live look at the local server, git, an engine
    monkeypatch.setattr("metabot.roster.here", canary("the local model server"))
    monkeypatch.setattr("metabot.roster.local_status", canary("the local model server"))
    monkeypatch.setattr("metabot.gitcmd.git", canary("git"))
    monkeypatch.setattr("metabot.engines.get_engine", canary("an engine"))
    _runs(machine, capsys)
    for n, task in enumerate(TASKS):
        out = machine / f"draft-{n}.json"
        assert main(["propose", "--folder", str(machine / "office"), "--out", str(out), task]) == 0
    assert reached == []


# ── (b) and (c) it writes nothing but the draft it was asked for ────────────


def test_without_out_propose_writes_nothing_at_all(machine, capsys):
    before = _tree(machine)
    _runs(machine, capsys)
    assert _tree(machine) == before  # the home, its config, logins, receipts, the office


def test_with_no_home_at_all_propose_makes_none(tmp_path, home, capsys):
    folder = tmp_path / "work"
    folder.mkdir()
    for task in TASKS:
        assert main(["propose", "--folder", str(folder), task]) == 0
    assert not home.exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["work"]
    assert list(folder.iterdir()) == []


def test_with_out_propose_writes_the_draft_and_nothing_else(machine, home, capsys):
    before = _tree(machine)
    out = machine / "draft.json"
    assert main(["propose", "--folder", str(machine / "office"), "--out", str(out), "fix it"]) == 0
    after = _tree(machine)
    assert set(after) - set(before) == {"draft.json"}
    assert {k: v for k, v in after.items() if k != "draft.json"} == before
    for kept in ("receipts", "workflows", "routines", "firings", "logins"):
        assert not (home / kept).exists(), kept  # no approval, store entry, routine or firing
    raw = out.read_bytes()
    assert main(["propose", "--folder", str(machine), "--out", str(out), "review it"]) == 2
    assert out.read_bytes() == raw
    assert _tree(machine) == after


# ── (d) its draft is read as a hand-written file is ─────────────────────────


def _hand_written(cwd: Path, step: dict) -> bytes:
    data = {"format": FORMAT, "name": "by-hand", "cwd": str(cwd), "steps": [step]}
    return json.dumps(data, indent=1).encode()


def test_a_draft_passes_the_check_a_hand_written_file_passes(tmp_path, capsys):
    save_config(Config(roster={"review": {"engine": "openai", "model": "tiny"}}))
    p = propose(
        "review my patch",
        tmp_path,
        Config(roster={"review": {"engine": "openai", "model": "tiny"}}),
    )
    hand = _hand_written(
        tmp_path,
        {"name": "ask", "kind": "ask", "prompt": "review my patch", "role": "review",
         "engine": "openai", "model": "tiny", "read_only": True},
    )  # fmt: skip
    drafted, written = parse_workflow(p.raw), parse_workflow(hand)
    assert drafted.steps == written.steps
    assert here_problems(drafted) == here_problems(written) == []
    for name, raw in (("draft.json", p.raw), ("hand.json", hand)):
        (tmp_path / name).write_bytes(raw)
        code = main(["workflow", "check", str(tmp_path / name)])
        out = capsys.readouterr().out
        assert code == 0 and "it can run here" in out


@pytest.mark.parametrize(
    ("change", "field"),
    [
        (lambda s: s.update(allow=["Edit"]), "steps[0]"),  # read_only takes no allow
        (lambda s: s.update(max_cost="0"), "steps[0].max_cost"),
        (lambda s: s.update(fan=True), "steps[0].fan"),
        (lambda s: s.update(role="re view"), "steps[0].role"),
    ],
)
def test_a_draft_edited_to_break_a_rule_fails_as_a_hand_written_file_does(
    tmp_path, capsys, change, field
):
    cfg = Config(roster={"review": {"engine": "openai", "model": "tiny"}})
    data = json.loads(propose("review my patch", tmp_path, cfg).raw)
    hand = json.loads(
        _hand_written(
            tmp_path,
            {"name": "ask", "kind": "ask", "prompt": "review my patch", "role": "review",
             "engine": "openai", "model": "tiny", "read_only": True},
        )
    )  # fmt: skip
    errors = []
    for doc in (data, hand):
        change(doc["steps"][0])
        raw = json.dumps(doc).encode()
        with pytest.raises(WorkflowError) as caught:
            parse_workflow(raw)
        errors.append((caught.value.field, caught.value.reason))
        (tmp_path / "edited.json").write_bytes(raw)
        assert main(["workflow", "check", str(tmp_path / "edited.json")]) == 1
        errors.append(capsys.readouterr().err)
    assert errors[0] == errors[2] and errors[0][0] == field
    assert errors[1] == errors[3]
