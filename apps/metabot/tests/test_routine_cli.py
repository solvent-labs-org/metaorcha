"""``metabot routine`` end to end, and a firing's connections on every engine path."""

from __future__ import annotations

import io
import json
import os
import sys

from conftest import FAKE_MCP, FIXTURES
from fake_chat import answer, call
from fake_engines import wrap
from metabot.approvals import DenyAll
from metabot.cli import main
from metabot.config import Config, save_config
from metabot.connections import ConnectionStore
from metabot.engines.claude import ClaudeEngine
from metabot.engines.codex import CodexEngine
from metabot.engines.cursor import CursorEngine
from metabot.engines.openai_compat import OpenAIEngine
from metabot.gate import Policy
from metabot.routines import fire, firings, get_routine, routines
from metabot.runner import run_job


def _connection(name: str, tmp_path) -> str:
    count = tmp_path / f"{name}-count"
    conn = ConnectionStore().add(
        name=name,
        transport="stdio",
        command=[sys.executable, str(FAKE_MCP), "--count-file", str(count)],
    )
    return conn.id


def _save(tmp_path, *extra: str) -> int:
    return main(
        [
            "routine", "save", "triage", "--cron", "0 9 * * 1-5", "--tz", "Europe/London",
            "-e", "openai", "--cwd", str(tmp_path), *extra, "Triage", "the", "issues.",
        ]
    )  # fmt: skip


def test_save_checks_the_routine_and_says_what_runs_unasked(tmp_path, capsys):
    _connection("gh", tmp_path)
    assert _save(tmp_path, "--connection", "gh", "--allow", "gh__delete_repo") == 2
    said = capsys.readouterr().err
    assert "routine not saved: scope_allow: delete_repo is destructive" in said
    assert routines() == []
    assert _save(tmp_path, "--connection", "gh", "--allow", "gh__create_issue") == 0
    out = capsys.readouterr().out
    assert out.startswith("saved routine triage (rt-")
    assert "runs without asking: gh__create_issue (write)" in out
    assert "nobody is there to ask" in out
    assert _save(tmp_path, "--connection", "gh", "--off") == 0
    assert capsys.readouterr().out.startswith("updated routine triage")
    record = get_routine("triage")
    assert record["allows"] == {} and record["enabled"] is False
    assert record["goal"] == "Triage the issues." and record["tz"] == "Europe/London"


def test_a_name_shaped_like_a_routine_id_is_refused(tmp_path, capsys):
    assert _save(tmp_path) == 0
    rid = get_routine("triage")["id"]
    capsys.readouterr()
    for name in (rid, "rt-000000000000"):
        argv = ["routine", "save", name, "--cron", "0 9 * * *", "-e", "openai", "Other."]
        assert main([*argv, "--cwd", str(tmp_path)]) == 2
        assert "routine not saved: name: " in capsys.readouterr().err
    [only] = routines()
    assert only["id"] == rid and only["name"] == "triage" and only["goal"] == "Triage the issues."


def test_the_list_shows_a_schedule_of_another_type_as_never_due(tmp_path, home, capsys):
    assert _save(tmp_path) == 0
    table = json.loads((home / "routines.json").read_text())
    for record in table.values():
        record["cron"] = 5  # edited by hand
    (home / "routines.json").write_text(json.dumps(table))
    capsys.readouterr()
    assert main(["routine", "list"]) == 0
    row = capsys.readouterr().out
    assert row.startswith("triage") and "next -" in row


def test_list_enable_disable_and_remove(tmp_path, capsys, monkeypatch):
    assert main(["routine", "list"]) == 0
    assert capsys.readouterr().out.startswith("no routines")
    assert _save(tmp_path) == 0
    capsys.readouterr()
    assert main(["routine", "list"]) == 0
    row = capsys.readouterr().out
    assert row.startswith("triage") and " on " in row and "never fired" in row
    assert "0 9 * * 1-5 (Europe/London)" in row
    assert main(["routine", "disable", "triage"]) == 0
    assert get_routine("triage")["enabled"] is False
    assert main(["routine", "enable", "triage"]) == 0
    assert get_routine("triage")["enabled"] is True
    monkeypatch.setattr("sys.stdin", io.StringIO(""))  # not a terminal
    assert main(["routine", "remove", "triage"]) == 2
    assert "pass --yes" in capsys.readouterr().err
    assert main(["routine", "remove", "triage", "--yes"]) == 0
    assert routines() == []
    assert main(["routine", "run", "triage"]) == 2
    assert "no routine named 'triage'" in capsys.readouterr().err


def test_run_fires_now_and_firings_show_it(tmp_path, capsys, chat):
    fake = chat(script=[answer("All triaged.")])
    save_config(Config(openai_base_url=fake.url))
    assert _save(tmp_path) == 0
    capsys.readouterr()
    assert main(["routine", "run", "triage"]) == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == "All triaged."
    assert "sealed; receipt" in captured.err and "(valid)" in captured.err
    [firing] = firings()
    assert firing.state == "sealed" and firing.due is None
    assert main(["routine", "firings", "triage"]) == 0
    row = capsys.readouterr().out
    assert "triage" in row and "sealed" in row and "asked for" in row and firing.run_id in row


def test_run_says_when_the_answer_may_claim_a_declined_write(tmp_path, capsys, chat):
    write = call("write_file", {"path": "note.txt", "content": "x"}, "c1")
    # The answer claims a write that the gate declined.
    fake = chat(script=[write, answer("I wrote note.txt.")])
    save_config(Config(openai_base_url=fake.url))
    assert _save(tmp_path) == 0
    capsys.readouterr()
    assert main(["routine", "run", "triage"]) == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == "I wrote note.txt." and not (tmp_path / "note.txt").exists()
    caveat = "The answer may say otherwise; the receipt's steps are what happened."
    assert f"[metabot] 1 call was declined. {caveat}" in captured.err.splitlines()


def test_a_refused_firing_exits_one_with_the_reason(tmp_path, capsys):
    _connection("gh", tmp_path)
    assert _save(tmp_path, "--connection", "gh") == 0
    ConnectionStore().revoke("gh")
    assert main(["routine", "run", "triage"]) == 1
    assert "routine not fired: connection gh is revoked" in capsys.readouterr().err


async def test_a_cli_engine_is_served_only_the_routines_connections(tmp_path, monkeypatch):
    gh = _connection("gh", tmp_path)
    _connection("other", tmp_path)
    assert _save(tmp_path, "--connection", "gh") == 0
    argv_out = tmp_path / "argv.json"
    monkeypatch.setenv("FAKE_FIXTURE", str(FIXTURES / "codex-echo.jsonl"))
    monkeypatch.setenv("FAKE_ARGV_OUT", str(argv_out))
    firing = await fire(get_routine("triage")["id"], make_engine=lambda name: wrap(CodexEngine))
    assert firing.state == "sealed", firing.reason
    served = _served(argv_out)
    assert f'"--only", "{gh}"' in served and '"--no-connections"' not in served


def _served(argv_out) -> str:
    argv = json.loads(argv_out.read_text())["argv"]
    return next(a for a in argv if a.startswith("mcp_servers.metabot.args="))


async def _fired_argv(tmp_path, monkeypatch, engine_cls, fixture: str) -> list[str]:
    assert _save(tmp_path) == 0
    argv_out = tmp_path / "argv.json"
    monkeypatch.setenv("FAKE_FIXTURE", str(FIXTURES / fixture))
    monkeypatch.setenv("FAKE_ARGV_OUT", str(argv_out))
    firing = await fire(get_routine("triage")["id"], make_engine=lambda name: wrap(engine_cls))
    assert firing.state == "sealed", firing.reason
    return json.loads(argv_out.read_text())["argv"]


async def test_a_firing_runs_codex_in_its_read_only_sandbox(tmp_path, monkeypatch):
    argv = await _fired_argv(tmp_path, monkeypatch, CodexEngine, "codex-echo.jsonl")
    assert 'sandbox_mode="read-only"' in argv  # its own writes pass no gate, and nobody is there
    assert 'sandbox_mode="workspace-write"' not in argv


async def test_a_firing_runs_cursor_in_ask_mode(tmp_path, monkeypatch):
    argv = await _fired_argv(tmp_path, monkeypatch, CursorEngine, "cursor-echo.jsonl")
    assert argv[argv.index("--mode") + 1] == "ask"


async def test_a_firing_leaves_claude_to_its_gate_hook(tmp_path, monkeypatch):
    argv = await _fired_argv(tmp_path, monkeypatch, ClaudeEngine, "claude-echo.jsonl")
    assert "pre-tool-use" in argv[argv.index("--settings") + 1]
    assert "--permission-mode" not in argv


async def test_a_firing_keeps_the_local_engines_tools_for_its_gate(tmp_path, chat):
    fake = chat(script=[answer("done")])
    save_config(Config(openai_base_url=fake.url))
    assert _save(tmp_path) == 0
    firing = await fire(get_routine("triage")["id"])
    assert firing.state == "sealed", firing.reason
    offered = {t["function"]["name"] for t in fake.requests[0]["tools"]}
    assert {"shell", "write_file"} <= offered  # the gate declines each write


async def test_an_empty_connection_list_opens_none_on_either_path(tmp_path, monkeypatch, chat):
    _connection("gh", tmp_path)
    argv_out = tmp_path / "argv.json"
    monkeypatch.setenv("FAKE_FIXTURE", str(FIXTURES / "codex-echo.jsonl"))
    monkeypatch.setenv("FAKE_ARGV_OUT", str(argv_out))
    common = {
        "prompt": "hi", "cwd": tmp_path, "policy": Policy(), "approver": DenyAll(),
        "switchboard": True, "connection_ids": [],
    }  # fmt: skip
    result = await run_job(engine=wrap(CodexEngine), **common)
    assert result.ok, result.error
    assert '"--no-connections"' in _served(argv_out) and '"--only"' not in _served(argv_out)
    fake = chat(script=[answer("done")])
    engine = OpenAIEngine(base_url=fake.url, model="tiny", config=Config())
    assert (await run_job(engine=engine, **common)).ok
    offered = {t["function"]["name"] for t in fake.requests[0]["tools"]}
    assert not any(n.startswith("gh__") for n in offered)


async def test_serve_opens_only_the_connections_it_is_given(tmp_path):
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    from metabot.approvals import DenyAll
    from metabot.control import ControlServer, socket_path
    from metabot.decisions import Decider
    from metabot.gate import Policy
    from metabot.paths import metabot_home

    gh = _connection("gh", tmp_path)
    _connection("other", tmp_path)
    run = tmp_path / "run"
    run.mkdir(mode=0o700)
    sock = socket_path(run)
    decider = Decider(Policy(), DenyAll(), "did:orcha:agent:key-0000000000000000")
    server = ControlServer(sock, on_approve=decider, on_step=lambda step: None)
    await server.start()
    try:
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "metabot", "mcp", "serve", "--home", str(metabot_home()),
                  "--run-dir", str(run), "--control", str(sock), "--only", gh],
            env={"PYTHONPATH": os.environ.get("PYTHONPATH", "")},
        )  # fmt: skip
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            names = sorted(t.name for t in (await session.list_tools()).tools)
    finally:
        await server.close()
    assert names and all(n.startswith("gh__") for n in names)
    assert not (tmp_path / "other-count").exists()
