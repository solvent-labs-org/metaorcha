"""``metabot doctor --models``: whether a local model can call MetaBot's tools, and how."""

from __future__ import annotations

import json

from conftest import private_mode
from fake_chat import answer, call, text_reply
from metabot.cli import main
from metabot.config import Config, save_config
from metabot.models import cached_mode, probe_model, save_probe
from metabot.paths import metabot_home

WORD = "kestrel"


def _step(tool: str, **args) -> dict:
    return text_reply(json.dumps({"tool": tool, "arguments": args}))


async def test_a_model_that_calls_natively_passes_native(chat):
    fake = chat([call("read_file", {"path": "probe.txt"}), answer(f"The word is {WORD}.")])
    probe = await probe_model(fake.url, "tiny", word=WORD)
    assert probe.mode == "native"
    assert len(fake.requests) == 2
    assert "tools" in fake.requests[0]


async def test_a_model_that_writes_its_call_as_text_and_fails_by_schema_passes_repaired(chat):
    said = text_reply(json.dumps({"name": "read_file", "arguments": {"path": "probe.txt"}}))
    fake = chat([said, answer(WORD), text_reply("no idea")])  # native by text, then schema
    probe = await probe_model(fake.url, "tiny", word=WORD)
    assert probe.mode == "repaired"
    assert probe.detail == "wrote its call as text, and MetaBot read it"
    assert "response_format" in fake.requests[2]  # the schema was tried


async def test_a_model_that_writes_its_call_as_text_but_passes_by_schema_is_held_to_it(chat):
    said = text_reply(json.dumps({"name": "read_file", "arguments": {"path": "probe.txt"}}))
    fake = chat([
        said, answer(WORD),  # native, by text
        _step("read_file", path="probe.txt"), _step("answer", text=WORD),  # constrained
    ])  # fmt: skip
    probe = await probe_model(fake.url, "tiny", word=WORD)
    assert probe.mode == "json"
    assert probe.detail == "wrote its call as text, and passed held to a schema too"
    assert "response_format" in fake.requests[2]


async def test_a_model_that_calls_cleanly_by_itself_is_never_held_to_a_schema(chat):
    fake = chat([call("read_file", {"path": "probe.txt"}), answer(WORD)])
    assert (await probe_model(fake.url, "tiny", word=WORD)).mode == "native"
    assert len(fake.requests) == 2  # no schema attempt
    assert not any("response_format" in r for r in fake.requests)


async def test_a_model_that_fails_natively_but_passes_by_schema_needs_json(chat):
    fake = chat([
        answer("I cannot read files."),  # native: no call
        _step("read_file", path="probe.txt"),  # constrained
        _step("answer", text=WORD),
    ])  # fmt: skip
    probe = await probe_model(fake.url, "tiny", word=WORD)
    assert probe.mode == "json"
    assert "response_format" in fake.requests[1]


async def test_a_model_that_reads_but_never_says_the_word_is_not_native(chat):
    fake = chat([
        call("read_file", {"path": "probe.txt"}), answer("I read it."),
        _step("read_file", path="probe.txt"), _step("answer", text=f"It says {WORD}"),
    ])  # fmt: skip
    assert (await probe_model(fake.url, "tiny", word=WORD)).mode == "json"


async def test_a_model_that_fails_both_ways_is_none_and_says_why(chat):
    fake = chat([answer("no idea"), text_reply("still no idea")])
    probe = await probe_model(fake.url, "tiny", word=WORD)
    assert probe.mode == "none"
    assert probe.detail == "made no read of the file, as tools or by schema"


async def test_the_probe_never_writes_or_runs_a_command(chat):
    fake = chat([call("shell", {"command": "touch /tmp/x"}), answer("no")])
    await probe_model(fake.url, "tiny", word=WORD)
    sent = [t["function"]["name"] for t in fake.requests[0]["tools"]]
    assert sent == ["read_file", "list_dir"]  # a read-only job


async def test_a_remote_server_is_never_probed():
    probe = await probe_model("https://models.example.invalid/v1", "big", word=WORD)
    assert probe.mode == "unchecked"
    assert "remote" in probe.detail  # and the test guard saw no connection


def test_a_probe_is_kept_privately_per_server_and_model(home):
    save_probe("http://127.0.0.1:1/v1", "tiny", "json", "needs constrained output")
    assert cached_mode("http://127.0.0.1:1/v1", "tiny") == "json"
    assert cached_mode("http://127.0.0.1:1/v1/", "tiny") == "json"  # the same server
    assert cached_mode("http://127.0.0.1:2/v1", "tiny") is None
    assert cached_mode("http://127.0.0.1:1/v1", "other") is None
    path = metabot_home() / "models.json"
    assert private_mode(path) == 0o600


def test_a_cache_edited_into_nonsense_reads_as_no_probe(home):
    (metabot_home()).mkdir(parents=True, exist_ok=True)
    (metabot_home() / "models.json").write_text("{not json")
    assert cached_mode("http://127.0.0.1:1/v1", "tiny") is None
    (metabot_home() / "models.json").write_text(json.dumps({"x": {"mode": "rm -rf"}}))
    assert cached_mode("http://127.0.0.1:1/v1", "x") is None


def test_doctor_models_probes_each_model_the_server_lists(chat, capsys):
    script = [call("read_file", {"path": "probe.txt"}), answer("?")]  # a word it cannot know
    fake = chat(script, repeat=answer("no"), models=["tiny", "small"])
    save_config(Config(openai_base_url=fake.url))
    assert main(["doctor", "--models"]) == 0
    out = capsys.readouterr().out
    lines = [x for x in out.splitlines() if x.startswith("model ")]
    assert [x.split()[1] for x in lines] == ["tiny", "small"]
    assert all(" none: " in x for x in lines)
    assert cached_mode(fake.url, "small") == "none"


def test_doctor_models_takes_names(chat, capsys, monkeypatch):
    monkeypatch.setattr("metabot.models._word", lambda: WORD)
    fake = chat([call("read_file", {"path": "probe.txt"}), answer(WORD)])
    save_config(Config(openai_base_url=fake.url))
    assert main(["doctor", "--models", "tiny"]) == 0
    out = capsys.readouterr().out
    assert "model    tiny  native: called read_file and answered with the word" in out


def test_doctor_models_never_probes_a_remote_server(capsys):
    save_config(Config(openai_base_url="https://models.example.invalid/v1"))
    assert main(["doctor", "--models", "big"]) == 0
    assert "remote; not probed" in capsys.readouterr().out
