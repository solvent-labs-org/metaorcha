"""What an engine or a model says, nested past what Python can parse, reads as output that
does not read: the run goes on, or ends with an error it records, never with a traceback.
A bridge is another person's program, and a hosted model's server is another party's."""

from __future__ import annotations

import json

from fake_chat import answer, text_reply
from metabot.cli import main
from metabot.config import Config, save_config

DEEP = "[" * 100_000 + "]" * 100_000


def test_a_deep_line_from_an_engine_is_text():
    from metabot.engines.bridge import BridgeEngine
    from metabot.events import TextEvent

    engine = BridgeEngine("echo", {"command": ["echo"]})
    assert engine.parse_line(DEEP, {}) == [TextEvent(DEEP)]


def test_text_too_deep_to_read_holds_no_call():
    from metabot.toolcalls import calls_in_text, leading_calls, parse_args, step_in_text

    assert parse_args(DEEP) is None
    assert not calls_in_text(DEEP, ["read_file"])
    assert not leading_calls('{"name": "read_file", "arguments": {}} ' + DEEP, ["read_file"])
    assert step_in_text(DEEP, ["read_file"]) is None


def _ask(tmp_path, capsys) -> tuple[int, dict]:
    code = main(["ask", "-e", "openai", "-m", "tiny", "--json", "--cwd", str(tmp_path), "hi"])
    return code, json.loads(capsys.readouterr().out)


def test_a_model_that_answers_in_deep_brackets_has_answered(chat, tmp_path, capsys):
    fake = chat([text_reply(DEEP)])
    save_config(Config(openai_base_url=fake.url))
    code, out = _ask(tmp_path, capsys)
    assert code == 0 and out["final_text"] == DEEP and out["valid"] is True


def test_a_call_whose_arguments_are_too_deep_is_not_run(chat, tmp_path, capsys):
    fn = {"name": "read_file", "arguments": DEEP}
    message = {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "function": fn}]}
    fake = chat([{"choices": [{"message": message}]}, answer("done")])
    save_config(Config(openai_base_url=fake.url))
    code, out = _ask(tmp_path, capsys)
    assert code == 0 and out["final_text"] == "done" and out["valid"] is True


def test_a_server_reply_too_deep_to_read_ends_the_run_with_an_error(tmp_path, monkeypatch, capsys):
    from metabot.engines.openai_compat import OpenAIEngine

    async def deep_body(self, payload):
        return json.loads(DEEP)  # what decoding the server's body does

    monkeypatch.setattr(OpenAIEngine, "_post", deep_body)
    save_config(Config(openai_base_url="http://127.0.0.1:9/v1"))
    code, out = _ask(tmp_path, capsys)
    assert code == 1 and out["valid"] is True
    assert "openai: maximum recursion depth exceeded" in (out["error"] or "")
