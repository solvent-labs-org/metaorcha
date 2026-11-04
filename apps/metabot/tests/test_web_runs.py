"""Runs started in the web UI: their asks answered in the page, their receipts sealed."""

from __future__ import annotations

import asyncio
import contextlib
import html
import json
import re
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from metabot.events import Final, Notice, Started, ToolCall, ToolResult
from metabot.paths import receipts_dir, run_path
from metabot.receipts import journal_matches, list_index, verify_file
from metabot.web_app import make_app
from metabot.web_guard import COOKIE, Guard

PORT = 47003
TOKEN = "t" * 43
FORM = re.compile(r'action="/asks/(ask-[0-9a-f]{16})"')
NAMED = re.compile(r"\((ask-[0-9a-f]{16})\)")
REFRESH = '<meta http-equiv="refresh"'
PAYLOADS = ["<script>alert(1)</script>", '"><img src=x onerror=alert(1)>']


class Asks:
    """Asks the gate about one call, then ends saying what it was told."""

    name = "fake"
    tool_prefix = "fake"
    skip_tools = ()

    def __init__(self, cls="write", tool="Edit", args=None, before=None, say=None) -> None:
        self.cls, self.tool = cls, tool
        self.args = args if args is not None else {"path": "a.txt"}
        self.before = before  # a threading.Event the run waits for before it asks
        self.say = say

    async def run(self, job, *, board=None, approve):
        yield Started("s-1", job.model)
        if self.say is not None:
            yield Notice(self.say)
        if self.before is not None:
            await asyncio.to_thread(self.before.wait, 10)
        yield ToolCall("call-1", self.tool, self.args)
        msg = {"op": "approve", "call_id": "call-1", "tool": f"fake/{self.tool}", "cls": self.cls}
        reply = await approve({**msg, "preview": json.dumps(self.args)})
        yield ToolResult("call-1", "ran" if reply["allow"] else "declined", reply["allow"])
        yield Final(self.say or ("allowed" if reply["allow"] else "declined"), "s-1")


class Sleeps:
    """Starts, then waits a minute; stopping it cancels the wait."""

    name = "fake"
    tool_prefix = "fake"
    skip_tools = ()

    async def run(self, job, *, board=None, approve):
        yield Started("s-1", job.model)
        await asyncio.sleep(60)
        yield Final("woke", "s-1")


class Answers:
    """Answers at once, with no tool call."""

    name = "fake"
    tool_prefix = "fake"
    skip_tools = ()

    async def run(self, job, *, board=None, approve):
        yield Started("s-1", job.model)
        yield Final("calc.py is not here.", "s-1")


class Chatty(Asks):
    """Says ``count`` long lines, then asks: a trace longer than a page holds."""

    def __init__(self, count: int) -> None:
        super().__init__()
        self.count = count

    async def run(self, job, *, board=None, approve):
        yield Started("s-1", job.model)
        for n in range(self.count):
            yield Notice(f"line {n:04d} " + "x" * 120)
        yield ToolCall("call-1", self.tool, self.args)
        msg = {"op": "approve", "call_id": "call-1", "tool": f"fake/{self.tool}", "cls": self.cls}
        reply = await approve({**msg, "preview": json.dumps(self.args)})
        yield ToolResult("call-1", "ran" if reply["allow"] else "declined", reply["allow"])
        yield Final("allowed" if reply["allow"] else "declined", "s-1")


@contextlib.contextmanager
def ui(tmp_path: Path, *engines, wrap=None, **kw) -> Iterator[tuple[TestClient, Guard]]:
    """The UI as ``metabot ui`` serves it; leaving the block stops it."""
    guard = Guard(TOKEN, PORT)
    left = list(engines)
    app = make_app(guard, start_dir=tmp_path, make_engine=lambda name: left.pop(0), **kw)
    app = wrap(app) if wrap is not None else app
    base = f"http://127.0.0.1:{PORT}"
    with TestClient(app, base_url=base, follow_redirects=False) as client:
        client.cookies.set(COOKIE, TOKEN)
        yield client, guard


def _post(client: TestClient, guard: Guard, path: str, csrf: str | None = None, **fields):
    data = {"csrf": guard.csrf if csrf is None else csrf, **fields}
    return client.post(path, data=data, headers={"origin": guard.origin})


def _start(client: TestClient, guard: Guard, tmp_path: Path, **fields) -> str:
    form = {"prompt": "do the task", "engine": "openai", "folder": str(tmp_path), **fields}
    page = _post(client, guard, "/chat", **form)
    assert page.status_code == 303, page.text
    run_id = page.headers["location"].removeprefix("/live/")
    assert page.headers["location"] == f"/live/{run_id}"
    return run_id


def _until(client: TestClient, run_id: str, test: Callable[[str], bool], wait: float = 10.0) -> str:
    deadline = time.monotonic() + wait
    while True:
        text = client.get(f"/live/{run_id}").text
        if test(text):
            return text
        assert time.monotonic() < deadline, text[-3000:]
        time.sleep(0.02)


def _asked(text: str) -> bool:
    return FORM.search(text) is not None


def _ended(text: str) -> bool:
    return "<th>state</th><td>running</td>" not in text


def _started(text: str) -> bool:
    return "[metabot] started" in text


def _journal(run_id: str) -> dict:
    return json.loads((run_path(run_id) / "journal.json").read_text(encoding="utf-8"))


def _verdict(run_id: str, call_id: str) -> dict:
    [verdict] = [
        v for v in _journal(run_id)["verdicts"] if v["check"] == f"scope_approval:{call_id}"
    ]
    return verdict


def _sealed(run_id: str) -> bool:
    return verify_file(receipts_dir() / f"{run_id}.json").valid and journal_matches(run_id)


# ── answering in the page ───────────────────────────────────────────────────


def test_a_run_started_in_the_page_asks_there_and_records_the_answer(tmp_path):
    with ui(tmp_path, Asks()) as (client, guard):
        run_id = _start(client, guard, tmp_path)
        text = _until(client, run_id, _asked)
        assert "fake/Edit" in text and "a.txt" in text and "Allow" in text
        ask_id = FORM.search(text).group(1)
        answered = _post(client, guard, f"/asks/{ask_id}", allow="1")
        assert answered.status_code == 303 and answered.headers["location"] == f"/live/{run_id}"
        text = _until(client, run_id, _ended)
        assert "<td>done</td>" in text and ">valid<" in text and "allowed" in text
    assert _sealed(run_id)
    verdict = _verdict(run_id, "call-1")
    assert verdict["result"] == "pass" and verdict["detail"].startswith("did:orcha:agent:")
    assert _journal(run_id)["meta"]["final"] == "allowed"


def test_a_decline_in_the_page_is_recorded(tmp_path):
    with ui(tmp_path, Asks()) as (client, guard):
        run_id = _start(client, guard, tmp_path)
        ask_id = FORM.search(_until(client, run_id, _asked)).group(1)
        assert _post(client, guard, f"/asks/{ask_id}", allow="0").status_code == 303
        text = _until(client, run_id, _ended)
    assert _sealed(run_id)
    assert _verdict(run_id, "call-1") == {
        "check": "scope_approval:call-1",
        "result": "warn",
        "detail": "declined",
    }
    said = (
        "1 call was declined. The answer may say otherwise; the receipt's steps are what happened."
    )
    assert html.escape(said, quote=True) in text  # beside the answer on the live page


def test_the_page_says_when_it_answered_about_a_file_here_without_a_tool_call(tmp_path):
    (tmp_path / "calc.py").write_text("")
    with ui(tmp_path, Answers()) as (client, guard):
        run_id = _start(client, guard, tmp_path, prompt="what does calc.py do?")
        text = _until(client, run_id, _ended)
    said = "calc.py is here, but this run made no tool calls, so the receipt shows no read of it."
    assert html.escape(said, quote=True) in text


def test_a_destructive_call_needs_the_word_yes(tmp_path):
    engine = Asks(cls="destructive", tool="Bash", args={"command": "rm -rf build"})
    with ui(tmp_path, engine) as (client, guard):
        run_id = _start(client, guard, tmp_path)
        text = _until(client, run_id, _asked)
        assert 'name="word"' in text and "rm -rf build" in text
        ask_id = FORM.search(text).group(1)
        for word in ("", "y", "yes please"):
            refused = _post(client, guard, f"/asks/{ask_id}", allow="1", word=word)
            assert refused.status_code == 400 and "word yes" in refused.text, word
            assert f'href="/live/{run_id}"' in refused.text
        assert ask_id in client.get(f"/live/{run_id}").text  # it still waits
        assert _post(client, guard, f"/asks/{ask_id}", allow="1", word=" YES ").status_code == 303
        _until(client, run_id, _ended)
    assert _verdict(run_id, "call-1")["result"] == "pass"


def test_a_write_needs_no_word(tmp_path):
    with ui(tmp_path, Asks()) as (client, guard):
        run_id = _start(client, guard, tmp_path)
        text = _until(client, run_id, _asked)
        assert 'name="word"' not in text


def test_an_unanswered_ask_is_declined_after_the_timeout(tmp_path):
    with ui(tmp_path, Asks(), ask_timeout_s=0.1) as (client, guard):
        run_id = _start(client, guard, tmp_path)
        text = _until(client, run_id, _ended)
        assert "no answer in time" in text
        ask_id = NAMED.search(text).group(1)
        late = _post(client, guard, f"/asks/{ask_id}", allow="1")
        assert late.status_code == 410 and f'href="/live/{run_id}"' in late.text
    assert _sealed(run_id)
    assert _verdict(run_id, "call-1")["detail"] == "declined"


def test_an_ask_answered_twice_keeps_the_first_answer(tmp_path):
    with ui(tmp_path, Asks()) as (client, guard):
        run_id = _start(client, guard, tmp_path)
        ask_id = FORM.search(_until(client, run_id, _asked)).group(1)
        assert _post(client, guard, f"/asks/{ask_id}", allow="1").status_code == 303
        again = _post(client, guard, f"/asks/{ask_id}", allow="0")
        assert again.status_code == 409 and "allowed already" in again.text
        _until(client, run_id, _ended)
        assert _post(client, guard, f"/asks/{ask_id}", allow="0").status_code == 409
    assert _verdict(run_id, "call-1")["result"] == "pass"


def test_an_unknown_ask_is_not_found(tmp_path):
    with ui(tmp_path) as (client, guard):
        assert _post(client, guard, "/asks/ask-0000000000000000", allow="1").status_code == 404


def test_the_live_page_refreshes_only_while_nothing_is_asked(tmp_path):
    go = threading.Event()
    with ui(tmp_path, Asks(before=go)) as (client, guard):
        run_id = _start(client, guard, tmp_path)
        text = client.get(f"/live/{run_id}").text
        assert REFRESH in text and not _asked(text)
        go.set()
        text = _until(client, run_id, _asked)
        assert REFRESH not in text
        ask_id = FORM.search(text).group(1)
        _post(client, guard, f"/asks/{ask_id}", allow="1")
        text = _until(client, run_id, _ended)
        assert REFRESH not in text


def _trace(text: str) -> str:
    return text[text.index("<h2>Trace</h2>") :]


def test_the_live_page_shows_the_newest_trace_lines(tmp_path):
    # 400 lines of 140 characters: more than the page holds, fewer than the run keeps
    with ui(tmp_path, Chatty(400)) as (client, guard):
        run_id = _start(client, guard, tmp_path)
        text = _until(client, run_id, _asked)
        ask_id = FORM.search(text).group(1)
        trace = _trace(text)
        assert "line 0399" in trace and f"waits for you ({ask_id})" in trace
        assert "line 0000" not in trace
        first = int(re.search(r"line (\d{4})", trace).group(1))
        # the started line and every line before the first one shown
        assert f"{first + 1} earlier lines are not shown." in trace
        _post(client, guard, f"/asks/{ask_id}", allow="1")
        assert "fake/Edit allowed in the page" in _trace(_until(client, run_id, _ended))


# ── stopping ────────────────────────────────────────────────────────────────


def test_stop_seals_the_run_cancelled(tmp_path):
    with ui(tmp_path, Sleeps()) as (client, guard):
        run_id = _start(client, guard, tmp_path)
        _until(client, run_id, _started)
        assert REFRESH in client.get(f"/live/{run_id}").text
        stopped = _post(client, guard, f"/live/{run_id}/stop")
        assert stopped.status_code == 303 and stopped.headers["location"] == f"/live/{run_id}"
        text = _until(client, run_id, _ended)
        assert "<td>stopped</td>" in text and ">valid<" in text and REFRESH not in text
    assert _sealed(run_id) and _journal(run_id)["meta"]["status"] == "cancelled"


def test_closing_the_ui_declines_every_ask_and_seals_every_run(tmp_path):
    with ui(tmp_path, Asks(), Sleeps()) as (client, guard):
        asking = _start(client, guard, tmp_path)
        _until(client, asking, _asked)
        sleeping = _start(client, guard, tmp_path)
        _until(client, sleeping, _started)
    for run_id in (asking, sleeping):  # the UI has stopped
        assert _sealed(run_id) and _journal(run_id)["meta"]["status"] == "cancelled"
    assert _verdict(asking, "call-1")["detail"] == "declined: cancelled"


def test_the_ui_says_it_has_stopped_only_once_every_run_is_sealed(tmp_path):
    started: list[str] = []
    sealed_then: list[bool] = []

    def wrap(app):
        async def watched(scope, receive, send):
            async def watch(message):
                if message["type"] == "lifespan.shutdown.complete":
                    sealed_then.extend((receipts_dir() / f"{r}.json").is_file() for r in started)
                await send(message)

            await app(scope, receive, watch)

        return watched

    with ui(tmp_path, Asks(), Sleeps(), wrap=wrap) as (client, guard):
        started.append(_start(client, guard, tmp_path))
        _until(client, started[0], _asked)
        started.append(_start(client, guard, tmp_path))
        _until(client, started[1], _started)
    assert sealed_then == [True, True]


# ── what the page refuses ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("fields", "says"),
    [
        ({"folder": "no-such-folder"}, "is not a folder"),
        ({"engine": "nope"}, "unknown engine"),
        ({"role": "boss"}, "unknown role"),
        ({"model": "bad model!"}, "is not a model name"),
        ({"prompt": "   "}, "the task is empty"),
    ],
)
def test_the_chat_form_refuses_what_cannot_run(tmp_path, fields, says):
    with ui(tmp_path, Asks()) as (client, guard):
        form = {"prompt": "do the task", "engine": "openai", "folder": str(tmp_path), **fields}
        page = _post(client, guard, "/chat", **form)
        assert page.status_code == 400 and says in page.text
        assert "None yet." in client.get("/chat").text  # no run started
    assert list_index() == []


def test_the_chat_names_a_config_file_that_does_not_read(tmp_path, home):
    home.mkdir(mode=0o700, exist_ok=True)
    (home / "config.json").write_text("{not json", encoding="utf-8")
    with ui(tmp_path, Asks()) as (client, guard):
        form = {"prompt": "do the task", "engine": "openai", "folder": str(tmp_path)}
        page = _post(client, guard, "/chat", **form)
        assert page.status_code == 400
        assert html.escape(f"cannot read {home / 'config.json'}: ") in page.text
        assert "None yet." in client.get("/chat").text  # no run started
    assert list_index() == []


def test_a_form_from_elsewhere_changes_nothing(tmp_path):
    with ui(tmp_path, Asks()) as (client, guard):
        form = {"prompt": "do the task", "engine": "openai", "folder": str(tmp_path)}
        assert _post(client, guard, "/chat", csrf="x" * 43, **form).status_code == 403
        assert "None yet." in client.get("/chat").text
        run_id = _start(client, guard, tmp_path)
        ask_id = FORM.search(_until(client, run_id, _asked)).group(1)
        assert _post(client, guard, f"/asks/{ask_id}", csrf="", allow="1").status_code == 403
        assert _post(client, guard, f"/live/{run_id}/stop", csrf="x").status_code == 403
        assert _asked(client.get(f"/live/{run_id}").text)  # still waits, still running
        _post(client, guard, f"/asks/{ask_id}", allow="0")
        _until(client, run_id, _ended)


def test_a_live_page_for_a_run_not_started_here(tmp_path):
    with ui(tmp_path, Asks()) as (client, guard):
        run_id = _start(client, guard, tmp_path)
        ask_id = FORM.search(_until(client, run_id, _asked)).group(1)
        _post(client, guard, f"/asks/{ask_id}", allow="1")
        _until(client, run_id, _ended)
    with ui(tmp_path) as (client, guard):  # a new start of the UI knows the run only as kept
        kept = client.get(f"/live/{run_id}")
        assert kept.status_code == 303 and kept.headers["location"] == f"/runs/{run_id}"
        assert client.get("/live/mb-20990101T000000Z-aaaaaa").status_code == 404
        assert client.get("/live/bad id!").status_code == 400


# ── text from the model ─────────────────────────────────────────────────────


@pytest.mark.parametrize("payload", PAYLOADS)
def test_event_text_from_the_model_is_escaped(tmp_path, payload):
    engine = Asks(tool=payload, args={"path": payload}, say=payload)
    with ui(tmp_path, engine) as (client, guard):
        run_id = _start(client, guard, tmp_path, prompt=payload)
        text = _until(client, run_id, _asked)
        assert payload not in text and html.escape(payload, quote=True) in text
        ask_id = FORM.search(text).group(1)
        _post(client, guard, f"/asks/{ask_id}", allow="1")
        text = _until(client, run_id, _ended)
        assert payload not in text and html.escape(payload, quote=True) in text
        chat = client.get("/chat").text
        assert payload not in chat and html.escape(payload, quote=True) in chat
