"""Skills for the engines that cannot load a plugin: two read tools on the switchboard."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from conftest import FAKE_MCP, FIXTURES
from fake_chat import answer, call
from fake_engines import wrap
from metabot.approvals import DenyAll
from metabot.cli import main
from metabot.config import Config
from metabot.connections import ConnectionStore
from metabot.engines.claude import ClaudeEngine
from metabot.engines.codex import CodexEngine
from metabot.engines.openai_compat import OpenAIEngine
from metabot.gate import Policy
from metabot.paths import run_dir
from metabot.plugins import add_marketplace, enable, install, plugin_skills
from metabot.runner import run_job
from metabot.switchboard import SKILLS_LIST, SKILLS_LOAD, Switchboard
from plugin_kit import make_market, make_plugin, write, yes


def _skill(tmp_path: Path) -> Path:
    skill = tmp_path / "view" / "skills" / "hello" / "SKILL.md"
    write(skill, "---\nname: hello\ndescription: Says hello.\n---\nSay hello to the person.\n")
    write(skill.parent / "ref.md", "a reference\n")
    write(tmp_path / "view" / "secret.txt", "not the skill's\n")
    return skill


class Board:
    def __init__(self, store: ConnectionStore, **kw) -> None:
        self.steps: list[dict] = []
        self.asked: list[dict] = []
        self.board = Switchboard(store, approve=self._approve, record=self._record, **kw)

    async def _approve(self, msg: dict) -> dict:
        self.asked.append(msg)
        return {"allow": False, "reason": "no"}

    async def _record(self, step: dict) -> None:
        self.steps.append(step)


def _text(result) -> str:
    return "\n".join(c.text for c in result.content)


async def test_skills_tools_are_read_and_return_skill_text(tmp_path):
    b = Board(ConnectionStore(), skills={"tools:hello": _skill(tmp_path)})
    async with b.board as board:
        tools = {t.name: t for t in board.tools()}
        assert set(tools) == {SKILLS_LIST, SKILLS_LOAD}
        assert all(t.description.startswith("[read]") for t in tools.values())
        assert tools[SKILLS_LOAD].input_schema["properties"]["name"]["enum"] == ["tools:hello"]
        assert board.has(SKILLS_LOAD) and not board.has("gh__list_issues")
        listed = await board.call(SKILLS_LIST, {})
        loaded = await board.call(SKILLS_LOAD, {"name": "tools:hello"})
        ref = await board.call(SKILLS_LOAD, {"name": "tools:hello", "file": "ref.md"})
        escape = await board.call(SKILLS_LOAD, {"name": "tools:hello", "file": "../../secret.txt"})
        ghost = await board.call(SKILLS_LOAD, {"name": "ghost"})
    assert _text(listed) == "tools:hello: Says hello."
    assert "Say hello to the person." in _text(loaded) and not loaded.is_error
    assert _text(ref) == "a reference\n"
    assert escape.is_error and "not the skill's" not in _text(escape)
    assert ghost.is_error and SKILLS_LIST in _text(ghost)
    assert b.asked == []  # reads never ask
    assert [s["tool"] for s in b.steps] == ["metabot/skills_list"] + ["metabot/skills_load"] * 4
    assert [s["success"] for s in b.steps] == [True, True, True, False, False]


async def test_without_skills_the_board_has_no_skills_tools():
    b = Board(ConnectionStore())
    async with b.board as board:
        assert board.tools() == []
        result = await board.call(SKILLS_LIST, {})
    assert result.is_error


async def test_with_connections_off_the_board_opens_none(tmp_path):
    store = ConnectionStore()
    store.add(name="gh", transport="stdio", command=[sys.executable, str(FAKE_MCP)])
    b = Board(store, skills={"tools:hello": _skill(tmp_path)}, connections=False)
    async with b.board as board:
        assert {t.name for t in board.tools()} == {SKILLS_LIST, SKILLS_LOAD}
        assert board.routes == {}


def test_a_connection_cannot_be_named_skills():
    with pytest.raises(ValueError, match="skills"):
        ConnectionStore().add(name="skills", transport="stdio", command=["x"])


def test_plugin_skills_names_each_skill_by_its_plugin(tmp_path):
    a = make_plugin(tmp_path / "a", name="tools", skills=("hello", "bye"))
    b = make_plugin(tmp_path / "b", name="other", skills=("hello",))
    skills = plugin_skills([a, b])
    assert sorted(skills) == ["other:hello", "tools:bye", "tools:hello"]
    assert skills["other:hello"] == b / "skills" / "hello" / "SKILL.md"


def _enabled_tools(tmp_path: Path) -> Path:
    market = make_market(tmp_path, "tools")
    make_plugin(market / "plugins" / "tools", skills=("hello",))
    add_marketplace("m", market)
    install("tools@m", yes)
    return enable("tools").view


async def test_the_local_engine_loads_a_skill_through_the_board(chat, tmp_path):
    view = _enabled_tools(tmp_path)
    fake = chat(script=[call(SKILLS_LOAD, {"name": "tools:hello"}, "c1"), answer("done")])
    result = await run_job(
        engine=OpenAIEngine(base_url=fake.url, model="tiny", config=Config()),
        prompt="greet",
        cwd=tmp_path,
        policy=Policy(),
        approver=DenyAll(),
        plugin_dirs=[view],
    )
    assert result.ok, result.error
    offered = {t["function"]["name"] for t in fake.requests[0]["tools"]}
    assert {SKILLS_LIST, SKILLS_LOAD} <= offered
    reply = next(m for m in fake.requests[1]["messages"] if m["role"] == "tool")
    assert "# hello" in reply["content"]
    steps = json.loads((run_dir(result.run_id) / "journal.json").read_text())["steps"]
    assert [(s["tool"], s["success"]) for s in steps] == [("metabot/skills_load", True)]


def _argv_of(tmp_path: Path, monkeypatch, engine_cls, fixture: str, *flags: str) -> list[str]:
    argv_out = tmp_path / "argv.json"
    monkeypatch.setenv("FAKE_FIXTURE", str(FIXTURES / fixture))
    monkeypatch.setenv("FAKE_ARGV_OUT", str(argv_out))
    monkeypatch.setattr("metabot.commands.get_engine", lambda name: wrap(engine_cls))
    assert main(["ask", "-e", engine_cls.name, "-q", *flags, "hi"]) == 0
    return json.loads(argv_out.read_text())["argv"]


def test_claude_loads_the_plugin_itself_and_gets_no_board_for_it(tmp_path, monkeypatch):
    view = _enabled_tools(tmp_path)
    argv = _argv_of(tmp_path, monkeypatch, ClaudeEngine, "claude-echo.jsonl")
    assert argv[argv.index("--plugin-dir") + 1] == str(view)
    assert "--mcp-config" not in argv


def _served(argv: list[str]) -> str:
    return next(a for a in argv if a.startswith("mcp_servers.metabot.args="))


def test_codex_gets_the_board_for_skills_and_connections_stay_out_when_asked(
    tmp_path, monkeypatch, home
):
    view = _enabled_tools(tmp_path)
    argv = _argv_of(tmp_path, monkeypatch, CodexEngine, "codex-echo.jsonl")
    assert '"serve"' in _served(argv) and '"--no-connections"' in _served(argv)
    [skills_file] = list((home / "runs").glob("*/skills.json"))
    assert json.loads(skills_file.read_text()) == {
        "tools:hello": str(view / "skills" / "hello" / "SKILL.md")
    }
    ConnectionStore().add(name="gh", transport="stdio", command=[sys.executable, str(FAKE_MCP)])
    argv = _argv_of(tmp_path, monkeypatch, CodexEngine, "codex-echo.jsonl")
    assert '"--no-connections"' not in _served(argv)
    argv = _argv_of(tmp_path, monkeypatch, CodexEngine, "codex-echo.jsonl", "--no-connections")
    assert '"--no-connections"' in _served(argv)


async def test_serve_reads_the_skills_file_and_opens_no_connection_when_told(tmp_path):
    import os

    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    from metabot.control import ControlServer, socket_path
    from metabot.decisions import Decider
    from metabot.paths import metabot_home
    from metabot.switchboard import write_skills

    ConnectionStore().add(name="gh", transport="stdio", command=[sys.executable, str(FAKE_MCP)])
    run = tmp_path / "run"
    run.mkdir(mode=0o700)
    write_skills(run, {"tools:hello": _skill(tmp_path)})
    sock = socket_path(run)
    steps: list[dict] = []
    decider = Decider(Policy(), DenyAll(), "did:orcha:agent:key-0000000000000000")
    server = ControlServer(sock, on_approve=decider, on_step=steps.append)
    await server.start()
    try:
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "metabot", "mcp", "serve", "--home", str(metabot_home()),
                  "--run-dir", str(run), "--control", str(sock), "--no-connections"],
            env={"PYTHONPATH": os.environ.get("PYTHONPATH", "")},
        )  # fmt: skip
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            names = sorted(t.name for t in (await session.list_tools()).tools)
            loaded = await session.call_tool(SKILLS_LOAD, {"name": "tools:hello"})
    finally:
        await server.close()
    assert names == [SKILLS_LIST, SKILLS_LOAD]  # no gh__ tools: the connection never opened
    assert "Say hello to the person." in loaded.content[0].text
    assert [(s["tool"], s["success"]) for s in steps] == [("metabot/skills_load", True)]
