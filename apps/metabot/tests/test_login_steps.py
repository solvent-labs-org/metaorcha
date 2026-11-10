"""Logins on a ladder's rungs and an office's desks, and how many runs a login takes at once (S5).

A rung or a desk names a login the way a role does, and its run starts on that login's
folder. A login runs ``max_at_once`` runs at a time, across every MetaBot process here: a
run past that waits for a free one. A codex login runs one job at a time, because codex
keeps its sign-in in one file per folder, and its own guide says not to share that file
across concurrent jobs.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
import yaml

from conftest import FAKE_CLI, FIXTURES
from metabot import commands, runner
from metabot.approvals import DenyAll
from metabot.cli import main
from metabot.config import Config, ConfigError, load_config, login_of, save_config
from metabot.ladder import LadderError, Rung, read_ladder
from metabot.logins import login_slot
from metabot.office import load_office
from metabot.office_run import run_relay
from metabot.paths import metabot_home


def _folder(tmp_path: Path, name: str) -> Path:
    f = tmp_path / "logins" / name
    f.mkdir(parents=True)
    os.chmod(f, 0o700)
    return f


def _add(name: str, engine: str, home: Path, *more: str) -> int:
    return main(["login", "add", name, "--engine", engine, "--home", str(home), *more])


@pytest.fixture
def work(tmp_path: Path, capsys) -> Path:
    f = _folder(tmp_path, "claude-work")
    assert _add("qxwork", "claude", f) == 0
    capsys.readouterr()
    return f


@pytest.fixture
def side(tmp_path: Path, capsys) -> Path:
    f = _folder(tmp_path, "codex-side")
    assert _add("qxside", "codex", f) == 0
    capsys.readouterr()
    return f


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    d = tmp_path / "bin"
    d.mkdir()
    shim = d / "claude"
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_CLI}" "$@"\n')
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", str(d))
    monkeypatch.setattr(commands, "pick_approver", DenyAll)
    monkeypatch.setenv("FAKE_FIXTURE", str(FIXTURES / "claude-echo.jsonl"))
    seen = tmp_path / "seen.json"
    monkeypatch.setenv("FAKE_ARGV_OUT", str(seen))
    return seen


# ── a rung on a login ───────────────────────────────────────────────────────


def test_a_rung_names_a_login(work):
    [rung] = read_ladder([{"engine": "claude", "login": "qxwork"}], load_config())
    assert rung == Rung("claude", None, "sub", login="qxwork")
    assert rung.entry() == {"engine": "claude", "login": "qxwork"}


@pytest.mark.parametrize(
    ("item", "said"),
    [
        ({"engine": "claude", "login": "qxnone"}, "names a login the config does not have"),
        ({"engine": "codex", "login": "qxwork"}, "qxwork is a claude login, not one for codex"),
        ({"engine": "openai", "login": "qxwork"}, "a login is for claude or codex alone"),
        ({"engine": "claude", "login": 7}, "names a login the config does not have"),
    ],
)
def test_a_rung_with_a_login_that_does_not_fit_names_what_is_wrong(work, item, said):
    with pytest.raises(LadderError, match=said):
        read_ladder([item], load_config())


def test_a_ladder_rung_on_a_login_runs_on_its_folder(work, fake_claude, tmp_path):
    cfg = load_config()
    cfg.ladder = [{"engine": "claude", "login": "qxwork"}]
    save_config(cfg)
    assert main(["ask", "-q", "--ladder", "--cwd", str(tmp_path), "hi"]) == 0
    assert json.loads(fake_claude.read_text())["claude_config_dir"] == str(work)


# ── a desk on a login ───────────────────────────────────────────────────────


def _git(repo: Path, *args: str) -> None:
    import subprocess

    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    subprocess.run(["git", "-C", str(repo), *args], env=env, check=True, capture_output=True)


def _office(tmp_path: Path, desks: dict) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "MetaBot Test")
    _git(repo, "config", "user.email", "metabot-test@example.invalid")
    data = {"name": "shop", "gates": {"ready": {"owner": ["verdict"]}}, "desks": desks}
    (repo / "office.yaml").write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    (repo / "roles").mkdir()
    for name in desks:
        (repo / "roles" / f"{name}.md").write_text(f"You are the {name} desk.\n")
    (repo / "app.txt").write_text("v1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "office")
    return repo


class Made:
    """Stands in for get_engine: records what each desk's engine was made on."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    def __call__(self, name: str, login: str | None = None):
        from metabot.events import Final, Started

        self.calls.append((name, login))

        class Engine:
            tool_prefix = "fake"
            skip_tools = ()

            def __init__(self) -> None:
                self.name = name

            async def run(self, job, *, board=None, approve):
                yield Started(f"s-{name}", job.model)
                yield Final(f"{name} done", f"s-{name}")

        return Engine()


async def test_a_desk_names_a_login_and_its_run_starts_on_it(work, tmp_path):
    repo = _office(
        tmp_path, {"plan": {"role": "roles/plan.md", "harness": "claude", "login": "qxwork"}}
    )
    made = Made()
    await run_relay(load_office(repo), "a task", approver=DenyAll(), make_engine=made)
    assert made.calls == [("claude", "qxwork")]


async def test_a_desk_without_a_login_starts_as_before(work, tmp_path):
    repo = _office(tmp_path, {"plan": {"role": "roles/plan.md", "harness": "claude"}})
    made = Made()
    await run_relay(load_office(repo), "a task", approver=DenyAll(), make_engine=made)
    assert made.calls == [("claude", None)]


async def test_an_any_desk_takes_the_build_role_s_login(work, tmp_path):
    cfg = load_config()
    cfg.roster = {"build": {"engine": "claude", "login": "qxwork"}}
    save_config(cfg)
    repo = _office(tmp_path, {"any": {"harness": "any"}})
    made = Made()
    await run_relay(
        load_office(repo),
        "a task",
        approver=DenyAll(),
        make_engine=made,
        roster={"build": {"engine": "claude", "login": "qxwork"}},
    )
    assert made.calls == [("claude", "qxwork")]


@pytest.mark.parametrize(
    ("desk", "said"),
    [
        ({"harness": "claude", "login": "qxnone"}, "there is no login 'qxnone'"),
        ({"harness": "codex", "login": "qxwork"}, "qxwork is a claude login, not one for codex"),
        ({"harness": "cursor", "login": "qxwork"}, "a login is for claude or codex alone"),
    ],
)
def test_a_desk_whose_login_does_not_fit_cannot_run(work, tmp_path, desk, said):
    repo = _office(tmp_path, {"plan": {"role": "roles/plan.md", **desk}})
    [problem] = [d.problem for d in load_office(repo).desks.values()]
    assert problem is not None and said in problem


def test_a_desk_login_that_is_not_a_name_does_not_load(work, tmp_path):
    from metabot.office import OfficeError

    repo = _office(tmp_path, {"plan": {"role": "roles/plan.md", "harness": "claude", "login": 7}})
    with pytest.raises(OfficeError, match="desks.plan.login"):
        load_office(repo)


# ── how many at once ────────────────────────────────────────────────────────


def _login(name: str, engine: str, home: Path, at_once: int):
    from metabot.config import Login

    return Login(name, engine, str(home), at_once, "refuse")


async def test_a_login_runs_as_many_as_it_says_and_the_next_waits(tmp_path):
    login = _login("qxwork", "claude", tmp_path, 2)
    async with login_slot(login), login_slot(login):
        third = asyncio.ensure_future(_enter_and_leave(login))
        await asyncio.sleep(0.4)
        assert not third.done()  # both of its runs are taken
    await asyncio.wait_for(third, 5)


async def _enter_and_leave(login) -> None:
    async with login_slot(login, poll=0.05):
        pass


async def test_a_codex_login_runs_one_at_a_time_whatever_it_says(tmp_path):
    login = _login("qxside", "codex", tmp_path, 3)
    async with login_slot(login):
        second = asyncio.ensure_future(_enter_and_leave(login))
        await asyncio.sleep(0.4)
        assert not second.done()
    await asyncio.wait_for(second, 5)


async def test_a_wait_says_so_once(tmp_path):
    login = _login("qxwork", "claude", tmp_path, 1)
    said: list[str] = []
    async with login_slot(login):
        waiting = asyncio.ensure_future(_wait_saying(login, said))
        await asyncio.sleep(0.4)
    await asyncio.wait_for(waiting, 5)
    assert said == ["waited for a free run on the login qxwork (1 at once)"]


async def _wait_saying(login, said: list[str]) -> None:
    async with login_slot(login, poll=0.05, on_wait=said.append):
        pass


async def test_the_slots_live_in_metabot_s_home_never_in_the_login_s_folder(tmp_path):
    folder = _folder(tmp_path, "w")
    login = _login("qxwork", "claude", folder, 1)
    async with login_slot(login):
        locks = sorted(p.name for p in (metabot_home() / "locks").iterdir())
        assert locks == ["login-qxwork.0"]
        assert list(folder.iterdir()) == []
        assert oct((metabot_home() / "locks" / "login-qxwork.0").stat().st_mode & 0o777) == oct(
            0o600
        )


def test_another_process_holding_the_login_holds_this_one_back(tmp_path):
    """The slot is a lock on a file, so it holds across processes, not only within one."""
    import subprocess

    home = metabot_home()
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import fcntl, os, sys, time\n"
            f"d = {str(home / 'locks')!r}\n"
            "os.makedirs(d, mode=0o700, exist_ok=True)\n"
            "fd = os.open(os.path.join(d, 'login-qxwork.0'), os.O_RDWR | os.O_CREAT, 0o600)\n"
            "fcntl.flock(fd, fcntl.LOCK_EX)\n"
            "print('held', flush=True)\n"
            "sys.stdin.readline()\n",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "held"
        login = _login("qxwork", "claude", tmp_path, 1)

        async def attempt() -> bool:
            try:
                await asyncio.wait_for(_enter_and_leave(login), 0.5)
            except TimeoutError:
                return False
            return True

        assert asyncio.run(attempt()) is False
    finally:
        assert holder.stdin is not None
        holder.stdin.write("\n")
        holder.stdin.close()
        holder.wait(10)
        if holder.stdout is not None:
            holder.stdout.close()
    assert asyncio.run(asyncio.wait_for(_enter_and_leave(login), 5)) is None


async def test_a_run_on_a_login_holds_a_slot_while_its_engine_runs(work, monkeypatch, tmp_path):
    from fake_engines import wrap
    from metabot.engines.claude import ClaudeEngine
    from metabot.gate import Policy

    held: list[str] = []
    real = runner.login_slot

    def recording(login, **kw):
        held.append(login.name)
        return real(login, **kw)

    monkeypatch.setattr(runner, "login_slot", recording)
    monkeypatch.setenv("FAKE_FIXTURE", str(FIXTURES / "claude-echo.jsonl"))
    engine = wrap(ClaudeEngine)
    engine.login = login_of(load_config(), "qxwork")
    await runner.run_job(
        engine=engine, prompt="hi", cwd=tmp_path, policy=Policy(), approver=DenyAll()
    )
    assert held == ["qxwork"]


async def test_a_run_with_no_login_takes_no_slot(monkeypatch, tmp_path):
    from fake_engines import wrap
    from metabot.engines.claude import ClaudeEngine
    from metabot.gate import Policy

    monkeypatch.setattr(runner, "login_slot", lambda *a, **k: pytest.fail("took a slot"))
    monkeypatch.setenv("FAKE_FIXTURE", str(FIXTURES / "claude-echo.jsonl"))
    await runner.run_job(
        engine=wrap(ClaudeEngine), prompt="hi", cwd=tmp_path, policy=Policy(), approver=DenyAll()
    )


# ── a codex login says one at a time ───────────────────────────────────────


def test_login_add_refuses_more_than_one_at_once_for_codex(tmp_path, capsys):
    assert _add("qxside", "codex", _folder(tmp_path, "c"), "--max-at-once", "2") == 2
    err = capsys.readouterr().err
    assert "a codex login runs one job at a time" in err
    assert "qxside" not in load_config().logins


def test_login_set_refuses_more_than_one_at_once_for_codex(side, capsys):
    assert main(["login", "set", "qxside", "--max-at-once", "2"]) == 2
    assert "a codex login runs one job at a time" in capsys.readouterr().err
    assert load_config().logins["qxside"]["max_at_once"] == 1


def test_a_hand_edited_codex_login_with_more_at_once_does_not_load(tmp_path):
    path = metabot_home() / "config.json"
    save_config(Config())
    data = json.loads(path.read_text())
    data["logins"] = {
        "qxside": {"engine": "codex", "home": str(_folder(tmp_path, "c")), "max_at_once": 2}
    }
    path.write_text(json.dumps(data))
    with pytest.raises(ConfigError, match="logins"):
        load_config()


def test_a_ladder_with_a_refusing_login_rung_never_starts(
    work, fake_claude, tmp_path, monkeypatch, capsys
):
    cfg = load_config()
    cfg.ladder = [{"engine": "claude", "login": "qxwork"}]
    save_config(cfg)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret-value")
    assert main(["ask", "-q", "--ladder", "--cwd", str(tmp_path), "hi"]) == 2
    err = capsys.readouterr().err
    assert "ANTHROPIC_API_KEY is set here" in err
    assert "Traceback" not in err
    assert not fake_claude.exists()
