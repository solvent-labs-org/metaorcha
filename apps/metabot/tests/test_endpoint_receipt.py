"""Where the prompt went: a run on a chat-completions server signs which server it was."""

from __future__ import annotations

import json

import pytest

from fake_chat import FakeChat, answer
from metabot.approvals import DenyAll
from metabot.config import Config
from metabot.engines.openai_compat import OpenAIEngine
from metabot.gate import Policy
from metabot.ladder import read_ladder, rung_engine
from metabot.paths import run_path
from metabot.receipts import journal_matches, verify_file
from metabot.runner import run_job


@pytest.fixture
def server():
    srv = FakeChat([answer("done")])
    yield srv
    srv.close()


async def _meta(engine, tmp_path) -> dict:
    result = await run_job(
        engine=engine,
        prompt="tidy the notes",
        cwd=tmp_path,
        policy=Policy(),
        approver=DenyAll(),
        model=engine.model,
    )
    assert result.ok, result.error
    assert verify_file(result.receipt).valid and journal_matches(result.run_id)
    journal = json.loads((run_path(result.run_id) / "journal.json").read_text(encoding="utf-8"))
    return journal["meta"]


async def test_a_rung_on_a_named_endpoint_signs_its_name_and_address(server, tmp_path):
    cfg = Config(endpoints={"home": {"base_url": server.url, "model": "small"}})
    rung = read_ladder([{"engine": "openai", "endpoint": "home"}], cfg)[0]
    meta = await _meta(rung_engine(rung, cfg), tmp_path)
    assert meta["endpoint"] == {"name": "home", "base_url": server.url}


async def test_a_run_on_the_default_server_signs_its_address(server, tmp_path):
    cfg = Config(openai_base_url=server.url, openai_model="small")
    meta = await _meta(OpenAIEngine(config=cfg), tmp_path)
    assert meta["endpoint"] == {"base_url": server.url}


@pytest.mark.parametrize(
    ("given", "signed"),
    [
        ("http://user:secret@127.0.0.1:8080/v1", "http://127.0.0.1:8080/v1"),
        ("https://hosted.example.test/v1?key=sk-1#x", "https://hosted.example.test/v1"),
        ("http://[::1]:11434/v1/", "http://[::1]:11434/v1"),
    ],
)
def test_the_signed_address_never_carries_a_secret(given, signed):
    engine = OpenAIEngine(base_url=given, model="m", config=Config())
    assert engine.receipt_meta == {"endpoint": {"base_url": signed}}
