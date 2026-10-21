"""The ``openai`` engine with local models that call tools badly: repair, and constrained output."""

from __future__ import annotations

import json

from fake_chat import answer, call, refused, text_reply
from metabot.approvals import DenyAll
from metabot.config import Config
from metabot.engines.openai_compat import OpenAIEngine
from metabot.gate import Policy
from metabot.models import save_probe
from metabot.paths import run_dir
from metabot.receipts import verify_file
from metabot.runner import run_job


def _engine(fake, **kw) -> OpenAIEngine:
    return OpenAIEngine(base_url=fake.url, model="tiny", config=Config(), **kw)


async def _run(engine, tmp_path, **kw):
    return await run_job(
        engine=engine,
        prompt="go",
        cwd=tmp_path,
        policy=kw.pop("policy", Policy()),
        approver=DenyAll(),
        **kw,
    )


def _journal(result) -> dict:
    return json.loads((run_dir(result.run_id) / "journal.json").read_text())


def _steps(result) -> list[tuple[str, str, bool]]:
    return [(s["call_id"], s["tool"], s["success"]) for s in _journal(result)["steps"]]


READ = {"name": "read_file", "arguments": {"path": "README.md"}}


# ── repair, in the native mode ──────────────────────────────────────────────


async def test_a_call_written_as_text_is_run_and_recorded(chat, tmp_path):
    (tmp_path / "README.md").write_text("hello from the readme")
    fake = chat([text_reply(json.dumps(READ)), answer("done")])
    result = await _run(_engine(fake), tmp_path)
    assert result.ok, result.error
    assert result.final_text == "done"
    assert _steps(result) == [("text-0-0", "metabot/read_file", True)]
    assert "openai: read 1 tool call the model wrote as text" in _journal(result)["meta"]["notices"]
    assert verify_file(result.receipt).valid
    # the next request is well formed: the call as a call, its result as a tool message
    said, replied = fake.requests[1]["messages"][-2:]
    assert said == {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "text-0-0",
                "type": "function",
                "function": {"name": "read_file", "arguments": '{"path": "README.md"}'},
            }
        ],
    }
    assert replied == {
        "role": "tool",
        "tool_call_id": "text-0-0",
        "content": "hello from the readme",
    }


async def test_a_fenced_text_call_is_read_and_its_text_is_not_the_answer(chat, tmp_path):
    (tmp_path / "README.md").write_text("x")
    fenced = "```json\n" + json.dumps(READ) + "\n```"
    fake = chat([text_reply(fenced), answer("done")])
    result = await _run(_engine(fake), tmp_path)
    assert result.final_text == "done" and _steps(result)[0][1] == "metabot/read_file"


async def test_a_text_call_to_a_write_still_asks_the_gate(chat, tmp_path):
    write = {"name": "write_file", "arguments": {"path": "out.txt", "content": "hi"}}
    fake = chat([text_reply(json.dumps(write)), answer("could not")])
    result = await _run(_engine(fake), tmp_path)
    assert not (tmp_path / "out.txt").exists()
    assert _steps(result) == [("text-0-0", "metabot/write_file", False)]
    verdicts = json.loads(result.receipt.read_text())["verdicts"]
    assert {"check": "scope_approval:text-0-0", "result": "warn",
            "detail": "declined: no human present"} in verdicts  # fmt: skip


async def test_a_near_miss_name_runs_the_tool_it_means(chat, tmp_path):
    (tmp_path / "README.md").write_text("x")
    fake = chat([call("Read_File", {"path": "README.md"}, "c1"), answer("done")])
    result = await _run(_engine(fake), tmp_path)
    assert _steps(result) == [("c1", "metabot/read_file", True)]
    assert (
        "openai: the model asked for 'Read_File'; ran read_file"
        in _journal(result)["meta"]["notices"]
    )


async def test_fenced_arguments_are_read(chat, tmp_path):
    (tmp_path / "README.md").write_text("x")
    fn = {"name": "read_file", "arguments": '```json\n{"path": "README.md"}\n```'}
    message = {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "function": fn}]}
    fake = chat([{"choices": [{"message": message}]}, answer("done")])
    result = await _run(_engine(fake), tmp_path)
    assert _steps(result) == [("c1", "metabot/read_file", True)]


async def test_an_answer_that_is_an_unreadable_call_fails_the_run(chat, tmp_path):
    fake = chat([text_reply('{"name": "delete_all", "arguments": {"path": "/"}}')])
    result = await _run(_engine(fake), tmp_path)
    assert result.ok is False
    assert result.error == "openai: the model answered with a tool call MetaBot could not read"
    assert _steps(result) == []


async def test_prose_that_holds_json_stays_the_answer(chat, tmp_path):
    text = 'Use this: {"name": "read_file", "arguments": {"path": "a"}} in your config.'
    fake = chat([text_reply(text)])
    result = await _run(_engine(fake), tmp_path)
    assert result.ok, result.error
    assert result.final_text == text and _steps(result) == []


async def test_a_text_call_to_a_tool_a_read_only_run_lacks_is_not_made(chat, tmp_path):
    write = {"name": "write_file", "arguments": {"path": "out.txt", "content": "hi"}}
    fake = chat([text_reply(json.dumps(write))])
    result = await run_job(
        engine=_engine(fake), prompt="go", cwd=tmp_path, policy=Policy(), approver=DenyAll(),
        read_only=True,
    )  # fmt: skip
    assert not (tmp_path / "out.txt").exists()
    assert result.ok is False and _steps(result) == []


# ── constrained output: tool_mode="json" ────────────────────────────────────


def _step(tool: str, **args) -> dict:
    return text_reply(json.dumps({"tool": tool, "arguments": args}))


async def test_json_mode_asks_for_one_step_by_schema_and_sends_no_tools(chat, tmp_path):
    fake = chat([_step("answer", text="4")])
    result = await _run(_engine(fake, tool_mode="json"), tmp_path)
    assert result.ok and result.final_text == "4"
    sent = fake.requests[0]
    assert "tools" not in sent
    fmt = sent["response_format"]
    assert fmt["type"] == "json_schema"
    branches = fmt["json_schema"]["schema"]["anyOf"]
    names = [b["properties"]["tool"]["const"] for b in branches]
    assert names == ["read_file", "list_dir", "write_file", "shell", "answer"]
    read = branches[0]["properties"]["arguments"]
    assert read["required"] == ["path"] and read["properties"]["path"]["type"] == "string"
    assert "read_file" in sent["messages"][0]["content"]  # the tools are listed in the prompt


async def test_json_mode_runs_a_step_and_feeds_its_result_back(chat, tmp_path):
    (tmp_path / "README.md").write_text("hello from the readme")
    fake = chat([_step("read_file", path="README.md"), _step("answer", text="done")])
    result = await _run(_engine(fake, tool_mode="json"), tmp_path)
    assert result.ok, result.error
    assert result.final_text == "done"
    assert _steps(result) == [("step-0", "metabot/read_file", True)]
    fed = fake.requests[1]["messages"][-1]
    assert fed["role"] == "user"
    assert fed["content"].startswith("Result of read_file:")
    assert "hello from the readme" in fed["content"]
    assert fed["content"].endswith(
        "The request: go\nReply with the next step, or with answer as soon as you can answer "
        "the request."
    )  # a small model keeps to what was asked
    assert verify_file(result.receipt).valid


async def test_json_mode_leaves_writers_out_of_a_read_only_run(chat, tmp_path):
    fake = chat([_step("answer", text="ok")])
    await run_job(
        engine=_engine(fake, tool_mode="json"), prompt="go", cwd=tmp_path, policy=Policy(),
        approver=DenyAll(), read_only=True,
    )  # fmt: skip
    branches = fake.requests[0]["response_format"]["json_schema"]["schema"]["anyOf"]
    assert [b["properties"]["tool"]["const"] for b in branches] == [
        "read_file",
        "list_dir",
        "answer",
    ]


async def test_json_mode_falls_back_once_when_the_server_refuses_the_schema(chat, tmp_path):
    (tmp_path / "README.md").write_text("x")
    fake = chat([refused(), _step("read_file", path="README.md"), _step("answer", text="done")])
    result = await _run(_engine(fake, tool_mode="json"), tmp_path)
    assert result.ok, result.error
    assert "response_format" in fake.requests[0]
    assert all("response_format" not in r for r in fake.requests[1:])
    assert len(fake.requests) == 3
    notices = _journal(result)["meta"]["notices"]
    assert any("refused the response schema" in n for n in notices)


async def test_json_mode_a_step_that_does_not_read_fails_the_run(chat, tmp_path):
    fake = chat([text_reply("I think the answer is 4")])
    result = await _run(_engine(fake, tool_mode="json"), tmp_path)
    assert result.ok is False
    assert result.error == "openai: the model's step did not read as a tool or an answer"


async def test_json_mode_a_second_refusal_is_an_error_not_a_loop(chat, tmp_path):
    fake = chat([refused(), refused()])
    result = await _run(_engine(fake, tool_mode="json"), tmp_path)
    assert result.ok is False and "answered 400" in result.error
    assert len(fake.requests) == 2


# ── the mode a model runs in ────────────────────────────────────────────────


async def test_a_model_the_probe_found_needs_json_runs_in_json_mode(chat, tmp_path):
    fake = chat([_step("answer", text="4")])
    save_probe(fake.url, "tiny", "json", "read the file and answered")
    await _run(_engine(fake), tmp_path)
    assert "response_format" in fake.requests[0] and "tools" not in fake.requests[0]


async def test_a_model_never_probed_runs_natively(chat, tmp_path):
    fake = chat([answer("4")])
    await _run(_engine(fake), tmp_path)
    assert "tools" in fake.requests[0] and "response_format" not in fake.requests[0]
