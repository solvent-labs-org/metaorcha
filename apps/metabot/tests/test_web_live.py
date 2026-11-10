"""A run's live page: its state in a word, what waits for you, and the updates it gets."""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time

from starlette.testclient import TestClient

from metabot import web_app
from metabot.events import Started
from metabot.paths import new_run_id
from metabot.web_guard import COOKIE, Guard
from metabot.web_pages import live_page
from metabot.web_runs import Broker, LiveRun
from metabot.web_theme import STATIC_CACHE
from test_office_run import DESKS, _repo
from test_web_office import _start as _start_office
from test_web_office import office_ui
from test_web_runs import (
    FORM,
    PORT,
    REFRESH,
    TOKEN,
    Answers,
    Asks,
    Sleeps,
    _asked,
    _ended,
    _post,
    _start,
    _started,
    _until,
    ui,
)

SCRIPT = re.compile(r'<script src="(/static/live/[0-9a-f]{64}\.js)" defer></script>')
NOSCRIPT_REFRESH = f'<noscript>{REFRESH} content="2"></noscript>'
UPDATES = "This page updates as the run goes."
UPDATES_NOSCRIPT = (
    "This page updates every 2 seconds while the run goes, and holds still while a call"
    " waits for you."
)
WAITING = "Waiting for you. Nothing else runs until you answer."
ALLOW_NOTE = "Allow lets a write run. A destructive call waits until you type yes."
DESTRUCTIVE = "This call is destructive. Type <b>yes</b> to allow it"


class Breaks:
    """Starts, then breaks: the run fails, and its receipt records the failure."""

    name = "fake"
    tool_prefix = "fake"
    skip_tools = ()

    async def run(self, job, *, board=None, approve):
        yield Started("s-1", job.model)
        raise RuntimeError("the engine broke")


def _word(text: str, cls: str, word: str) -> bool:
    return f'<p class="state"><span class="{cls}">{word}</span>' in text


def _receipt_line(run_id: str) -> str:
    return f"Receipt {run_id}. Check it any time: <code>metabot receipts verify {run_id}</code>."


def _events(text: str) -> list[tuple[str, str]]:
    """The stream's events, as (name, data); a comment line is kept as ("", comment)."""
    out: list[tuple[str, str]] = []
    for block in text.split("\n\n"):
        name, data, comment = "message", [], None
        for line in block.splitlines():
            if line.startswith(":"):
                comment = line
            elif line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].strip())
        if data:
            out.append((name, "\n".join(data)))
        elif comment is not None:
            out.append(("", comment))
    return out


class Stream(threading.Thread):
    """Reads a run's event stream. The TestClient hands a body over once it ends, so the
    stream is read in a thread while the test goes on answering in the page."""

    def __init__(self, client: TestClient, run_id: str) -> None:
        super().__init__(daemon=True)
        self.client, self.run_id = client, run_id
        self.response = None
        self.start()

    def run(self) -> None:
        self.response = self.client.get(f"/live/{self.run_id}/events")

    def finish(self, wait: float = 10.0):
        self.join(wait)
        assert not self.is_alive(), "the event stream did not end"
        assert self.response is not None
        return self.response


# ── the page ────────────────────────────────────────────────────────────────


def test_a_running_page_says_running_and_updates_as_the_run_goes(tmp_path):
    with ui(tmp_path, Sleeps()) as (client, guard):
        run_id = _start(client, guard, tmp_path, prompt="tidy the notes")
        text = _until(client, run_id, _started)
        assert "<h1>tidy the notes</h1>" in text and f'<p class="kicker">Run {run_id}</p>' in text
        assert _word(text, "none", "Running")
        assert UPDATES in text and NOSCRIPT_REFRESH in text
        assert f'<noscript><p class="muted">{UPDATES_NOSCRIPT}</p></noscript>' in text
        assert text.count(REFRESH) == 1  # the refresh is for a browser without scripts alone
        assert "<button>Stop the run</button>" in text
        assert "<h2>Steps</h2>" not in text  # the steps are the receipt's: none yet
        _post(client, guard, f"/live/{run_id}/stop")
        text = _until(client, run_id, _ended)
    assert _word(text, "none", "Stopped")
    assert "Stopped. The steps so far are in the receipt." in text
    assert _receipt_line(run_id) in text and ">valid<" in text
    assert REFRESH not in text and UPDATES not in text and "Stop the run" not in text


def test_a_page_that_waits_for_you_says_so_and_then_finished(tmp_path):
    with ui(tmp_path, Asks()) as (client, guard):
        run_id = _start(client, guard, tmp_path)
        text = _until(client, run_id, _asked)
        assert _word(text, "warn", "Waiting for you")
        assert WAITING in text and UPDATES in text  # the stream goes on while you answer
        assert REFRESH not in text and "<noscript>" not in text  # the noscript refresh does not
        asks = text[text.index('<section id="asks">') : text.index("</section>")]
        assert "<h2>Waiting for you</h2>" in asks
        assert '<button name="allow" value="1">Allow</button>' in asks
        assert '<button name="allow" value="0">Decline</button>' in asks
        assert asks.rstrip().endswith(f'<p class="muted">{ALLOW_NOTE}</p>')
        assert DESTRUCTIVE not in asks
        ask_id = FORM.search(text).group(1)
        _post(client, guard, f"/asks/{ask_id}", allow="1")
        text = _until(client, run_id, _ended)
    assert _word(text, "ok", "Finished")
    assert _receipt_line(run_id) in text and ">valid<" in text
    steps = text[text.index("<h2>Steps</h2>") : text.index("<h2>Trace</h2>")]
    assert "<td>fake/Edit</td>" in steps and '<span class="ok">ok</span>' in steps
    assert "approved by did:orcha:agent:" in steps
    assert '<section id="asks"></section>' in text  # nothing waits


def test_a_destructive_call_says_what_it_needs(tmp_path):
    engine = Asks(cls="destructive", tool="Bash", args={"command": "rm -rf build"})
    with ui(tmp_path, engine) as (client, guard):
        run_id = _start(client, guard, tmp_path)
        text = _until(client, run_id, _asked)
        assert DESTRUCTIVE in text and ALLOW_NOTE in text
        ask_id = FORM.search(text).group(1)
        _post(client, guard, f"/asks/{ask_id}", allow="0")
        _until(client, run_id, _ended)


def test_an_office_run_that_waits_says_waiting_alone(tmp_path):
    root = _repo(tmp_path, {"build": DESKS["build"]})
    with office_ui(root, {"codex": Asks()}) as (client, guard):
        office_id = _start_office(client, guard, root, "build")
        text = _until(client, office_id, _asked)
        assert _word(text, "warn", "Waiting for you")
        assert (
            '<p class="state"><span class="warn">Waiting for you</span> Its other desks go on.</p>'
            in text
        )
        assert "Nothing else runs" not in text
        assert "<button>Stop the office run</button>" in text
        assert f'<p class="kicker">Office run {office_id}</p>' in text
        ask_id = FORM.search(text).group(1)
        _post(client, guard, f"/asks/{ask_id}", allow="1")
        text = _until(client, office_id, _ended)
    assert _word(text, "ok", "Finished") and "metabot receipts verify" not in text


def test_a_run_that_breaks_says_failed_and_its_receipt_records_it(tmp_path):
    with ui(tmp_path, Breaks()) as (client, guard):
        run_id = _start(client, guard, tmp_path)
        text = _until(client, run_id, _ended)
    assert _word(text, "bad", "Failed")
    assert "Failed. The receipt records the failure." in text
    assert "RuntimeError: the engine broke" in text and _receipt_line(run_id) in text


def test_a_run_that_never_began_names_no_receipt():
    # stopped before its first event, or refused under a cap: nothing was sealed
    never = "stopped before the run began"
    run = LiveRun("mb-1", "openai", "small", "/tmp", "do it", state="stopped", error=never)
    page = live_page(run, [], "no receipt", "n0nce", "csrf")
    assert '<p class="state"><span class="none">Stopped</span> Stopped.</p>' in page
    run = LiveRun("mb-2", "openai", "small", "/tmp", "do it", state="failed", error="no price")
    page += live_page(run, [], "no receipt", "n0nce", "csrf")
    assert '<p class="state"><span class="bad">Failed</span> Failed.</p>' in page
    for said in ("metabot receipts verify", "in the receipt", "records the failure", "<th>receipt"):
        assert said not in page, said
    assert never in page and "no price" in page


def test_the_trace_sits_behind_a_closed_details_toggle(tmp_path):
    with ui(tmp_path, Answers()) as (client, guard):
        run_id = _start(client, guard, tmp_path)
        text = _until(client, run_id, _ended)
    trace = text[text.index("<h2>Trace</h2>") :]
    assert trace.startswith("<h2>Trace</h2><details><summary>Show details</summary><pre>")
    assert "<details open" not in text and "[metabot] started" in trace


def test_the_main_part_is_served_alone_for_the_script(tmp_path):
    with ui(tmp_path, Asks()) as (client, guard):
        run_id = _start(client, guard, tmp_path)
        _until(client, run_id, _asked)
        part = client.get(f"/live/{run_id}?part=main")
        assert part.status_code == 200 and part.headers["cache-control"] == "no-store"
        assert part.text.startswith('<div id="live">') and part.text.endswith("</div>")
        assert "<!doctype" not in part.text and "<h1>" not in part.text
        for box in ('<div id="head">', '<section id="asks">', '<div id="rest">'):
            assert box in part.text
        assert '<div id="live">' in client.get(f"/live/{run_id}").text
        ask_id = FORM.search(part.text).group(1)
        _post(client, guard, f"/asks/{ask_id}", allow="0")
        _until(client, run_id, _ended)


def test_the_live_page_alone_loads_the_script_from_this_server(tmp_path):
    with ui(tmp_path, Sleeps()) as (client, guard):
        run_id = _start(client, guard, tmp_path)
        text = _until(client, run_id, _started)
        path = SCRIPT.search(text).group(1)
        script = client.get(path)
        assert script.status_code == 200
        assert script.headers["content-type"] == "text/javascript; charset=utf-8"
        assert script.headers["cache-control"] == STATIC_CACHE
        assert "EventSource(" in script.text and "?part=main" in script.text
        assert "token" not in script.text
        assert client.get("/static/live/" + "0" * 64 + ".js").status_code == 404
        assert "<script" not in client.get("/chat").text
        _post(client, guard, f"/live/{run_id}/stop")
        text = _until(client, run_id, _ended)
        assert SCRIPT.search(text) is None  # an ended run's page changes no more


# ── the event stream ────────────────────────────────────────────────────────


def test_the_stream_says_when_the_run_asks_and_ends_when_it_ends(tmp_path):
    with ui(tmp_path, Asks()) as (client, guard):
        run_id = _start(client, guard, tmp_path)
        stream = Stream(client, run_id)
        text = _until(client, run_id, _asked)
        ask_id = FORM.search(text).group(1)
        _post(client, guard, f"/asks/{ask_id}", allow="1")
        _until(client, run_id, _ended)
        response = stream.finish()
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-accel-buffering"] == "no"
    events = _events(response.text)
    told = [json.loads(data) for name, data in events if name == "message"]
    assert told and events[-1][0] == "end"
    assert any(e["asks"] == 1 and e["state"] == "running" for e in told)
    assert told[-1]["state"] == "done" and told[-1]["asks"] == 0
    assert told[-1]["trace_len"] >= 3  # started, waits for you, allowed in the page


def test_a_stream_for_a_run_not_started_here_is_not_found(tmp_path):
    with ui(tmp_path) as (client, _guard):
        assert client.get("/live/mb-20990101T000000Z-aaaaaa/events").status_code == 404
        assert client.get("/live/bad id!/events").status_code == 400


def test_a_stream_without_the_cookie_is_refused(tmp_path):
    with ui(tmp_path, Sleeps()) as (client, guard):
        run_id = _start(client, guard, tmp_path)
        client.cookies.delete(COOKIE)
        try:
            refused = client.get(f"/live/{run_id}/events")
        finally:
            client.cookies.set(COOKIE, TOKEN)
        assert refused.status_code == 401 and "token" not in refused.headers.get("location", "")
        _post(client, guard, f"/live/{run_id}/stop")
        _until(client, run_id, _ended)


def test_a_stream_ends_when_the_broker_closes(tmp_path, monkeypatch):
    made: list[Broker] = []

    class Seen(Broker):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            made.append(self)

    monkeypatch.setattr(web_app, "Broker", Seen)
    run_id = new_run_id()
    with ui(tmp_path) as (client, _guard):
        [broker] = made
        # opened and never started: nothing but the close can end its stream
        client.portal.call(lambda: broker.open(run_id, cwd=str(tmp_path), prompt="wait"))
        stream = Stream(client, run_id)
        time.sleep(0.1)
        assert stream.is_alive()
        client.portal.call(broker.close)
        response = stream.finish()
    events = _events(response.text)
    assert events[-1][0] == "end" and json.loads(events[-2][1])["state"] == "running"


def test_the_stream_keeps_alive_while_nothing_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(web_app, "KEEPALIVE_S", 0.05)
    with ui(tmp_path, Sleeps()) as (client, guard):
        run_id = _start(client, guard, tmp_path)
        _until(client, run_id, _started)
        stream = Stream(client, run_id)
        time.sleep(0.3)
        _post(client, guard, f"/live/{run_id}/stop")
        response = stream.finish()
    events = _events(response.text)
    assert ("", ": keepalive") in events and events[-1][0] == "end"
    assert json.loads(events[-2][1])["state"] == "stopped"


def test_a_closing_ui_ends_an_open_stream(tmp_path):
    """The UI's own close ends every open stream with ``end`` before the server goes away.
    (Driven through the broker: a newer Starlette test client aborts its open requests
    before it sends the lifespan shutdown, so leaving the client block cannot show this.)"""
    from metabot.web_app import make_ui

    guard = Guard(TOKEN, PORT)
    app, broker = make_ui(guard, start_dir=tmp_path, make_engine=lambda name: Sleeps())
    with TestClient(app, base_url=f"http://127.0.0.1:{PORT}", follow_redirects=False) as client:
        client.cookies.set(COOKIE, TOKEN)
        run_id = _start(client, guard, tmp_path)
        _until(client, run_id, _started)
        stream = Stream(client, run_id)
        time.sleep(0.1)
        assert client.portal is not None
        client.portal.call(broker.close)  # the UI stopped: the run was sealed, the stream ended
        response = stream.finish()
    assert _events(response.text)[-1][0] == "end", f"the stream as received:\n{response.text}"
    assert json.loads(_events(response.text)[-2][1])["state"] == "stopped"


# ── closing waits for the streams ───────────────────────────────────────────


def test_close_waits_for_an_open_event_stream():
    """A UI that closes lets every open event stream write its ``end`` first, so a page never
    sees its stream cut off mid-message."""
    from metabot.web_runs import Broker

    async def scenario():
        broker = Broker()
        order = []

        async def a_stream():
            async with broker.stream():
                order.append("stream open")
                await asyncio.sleep(0.2)
                order.append("stream done")

        task = asyncio.create_task(a_stream())
        await asyncio.sleep(0.05)
        assert broker.streams == 1
        await broker.close()
        order.append("closed")
        await task
        return order

    assert asyncio.run(scenario()) == ["stream open", "stream done", "closed"]


def test_close_does_not_wait_forever_for_a_stuck_stream(monkeypatch):
    from metabot import web_runs

    monkeypatch.setattr(web_runs, "STREAM_DRAIN_S", 0.1)

    async def scenario():
        broker = web_runs.Broker()
        started = asyncio.Event()

        async def stuck():
            async with broker.stream():
                started.set()
                await asyncio.sleep(10)

        task = asyncio.create_task(stuck())
        await started.wait()
        t0 = time.monotonic()
        await broker.close()
        waited = time.monotonic() - t0
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return waited

    waited = asyncio.run(scenario())
    assert 0.1 <= waited < 2.0
