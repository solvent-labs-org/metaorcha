"""Live runs on the engines installed here. Skipped unless ``METABOT_LIVE=1``.

They use the person's own engine logins and cost a little of their plan, so
they never run by default or in CI. Every run gets a scratch home and key
(the autouse fixture), never ``~/.metabot`` or ``~/.orcha/key``.

Run: ``METABOT_LIVE=1 env -u CLAUDECODE .venv/bin/python -m pytest -s tests/test_live.py``.
Each test prints one ``LIVE {...}`` line for the evidence file.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request

import pytest

from conftest import FAKE_MCP
from metabot.approvals import DenyAll
from metabot.config import load_config
from metabot.connections import ConnectionStore
from metabot.engines import available
from metabot.gate import Policy
from metabot.paths import run_dir
from metabot.receipts import journal_matches, verify_file
from metabot.runner import run_job

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("METABOT_LIVE") != "1", reason="live engines: set METABOT_LIVE=1"
    ),
]

PROMPT = "Run echo metabot-live and reply with its output."
MODELS = {"claude": "haiku", "codex": None, "cursor": None, "openai": None}
PREFIX = {"claude": "claude-code/", "codex": "codex/", "cursor": "cursor/", "openai": "metabot/"}


def _openai_up() -> bool:
    base = load_config().openai_base_url.rstrip("/")
    try:
        with urllib.request.urlopen(f"{base}/models", timeout=2) as resp:
            return resp.status == 200
    except OSError:
        return False


def _report(name: str, result, env: dict) -> None:
    print(
        "LIVE "
        + json.dumps(
            {
                "test": name,
                "ok": result.ok,
                "error": result.error,
                "final": (result.final_text or "")[:200],
                "steps": [(s["tool"], s["success"]) for s in env["steps"]],
                "verdicts": env["verdicts"],
                "valid": verify_file(result.receipt).valid,
                "journal_matches": journal_matches(result.run_id),
            }
        ),
        flush=True,
    )


@pytest.mark.parametrize("engine", ["claude", "codex", "cursor", "openai"])
async def test_engine_runs_and_seals(engine, tmp_path):
    if engine == "openai":
        if not _openai_up():
            pytest.skip("no local model server answering")
    elif not available().get(engine):
        pytest.skip(f"{engine} is not installed")
    result = await run_job(
        engine=engine,
        prompt=PROMPT,
        cwd=tmp_path,
        policy=Policy(allow_write=False),
        approver=DenyAll(),
        model=MODELS[engine],
    )
    env = json.loads(result.receipt.read_text())
    _report(f"engine:{engine}", result, env)
    assert verify_file(result.receipt).valid
    assert journal_matches(result.run_id)
    assert result.ok, result.error
    assert any(s["tool"].startswith(PREFIX[engine]) for s in env["steps"]), env["steps"]
    assert "metabot-live" in result.final_text


WALK_MODELS = {"claude": "haiku", "codex": None, "cursor": None, "openai": None}


@pytest.mark.parametrize("engine", ["claude", "codex", "cursor", "openai"])
async def test_connection_walk(engine, tmp_path, home):
    """A read goes through; a write with nobody to approve it never reaches the server.

    Also records whether the engine's sandbox confines the switchboard: the
    workspace is kept apart from MetaBot's home, so a switchboard log in the
    run folder means the switchboard wrote outside the workspace.
    """
    if engine == "openai":
        if not _openai_up():
            pytest.skip("no local model server answering")
    elif not available().get(engine):
        pytest.skip(f"{engine} is not installed")
    work = tmp_path / "work"
    work.mkdir()
    count, status = tmp_path / "count", tmp_path / "status.json"
    ConnectionStore().add(
        name="fake",
        transport="stdio",
        command=[sys.executable, str(FAKE_MCP), "--count-file", str(count),
                 "--status-file", str(status)],
    )  # fmt: skip
    result = await run_job(
        engine=engine,
        prompt=(
            "Use the fake connection's tools: list the issues, then create one issue titled x. "
            "Report what each call returned."
        ),
        cwd=work,
        policy=Policy(),
        approver=DenyAll(),
        model=WALK_MODELS[engine],
        switchboard=True,
    )
    env = json.loads(result.receipt.read_text())
    _report(f"connection-walk:{engine}", result, env)
    log = run_dir(result.run_id) / "switchboard.log"
    print(
        "LIVE "
        + json.dumps(
            {
                "test": f"switchboard-confinement:{engine}",
                "server_status": json.loads(status.read_text()) if status.exists() else None,
                "switchboard_log_outside_workspace": log.exists(),
                "home_inside_workspace": work in home.parents,
            }
        ),
        flush=True,
    )
    assert verify_file(result.receipt).valid
    assert journal_matches(result.run_id)
    calls = count.read_text().splitlines() if count.exists() else []
    assert "create_issue" not in calls  # nothing unapproved ever reaches the server
    by_cap = {s["tool"].split("#", 1)[1]: s["success"] for s in env["steps"] if "#" in s["tool"]}
    if "create_issue" in by_cap:
        assert by_cap["create_issue"] is False
        assert any(v["detail"] == "declined: no human present" for v in env["verdicts"])
    if engine != "openai":  # a small local model may not follow both steps; the gate still holds
        assert by_cap.get("list_issues") is True
        assert "create_issue" in by_cap
