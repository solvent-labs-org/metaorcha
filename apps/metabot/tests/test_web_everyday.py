"""The home page: one box for a task and a folder. A task runs as a ladder, on the endpoints
the settings name, and a write waits for the person's answer on the page."""

from __future__ import annotations

import json
import re
import shlex
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from fake_chat import FakeChat, answer, call
from test_web_runs import _ended, _post, ui  # the UI harness the run tests use

FORM = re.compile(r'action="/asks/(ask-[0-9a-f]{16})"')


def configure(home: Path, monkeypatch, **settings) -> None:
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("METABOT_HOME", str(home))
    (home / "config.json").write_text(json.dumps(settings), encoding="utf-8")


def _until(client, run_id: str, test: Callable[[str], bool], wait: float = 10.0) -> str:
    deadline = time.monotonic() + wait
    while True:
        text = client.get(f"/live/{run_id}").text
        if test(text):
            return text
        assert time.monotonic() < deadline, text[-3000:]
        time.sleep(0.02)


def test_the_home_page_is_one_box_and_a_folder_and_nothing_else(tmp_path, monkeypatch):
    configure(tmp_path / "home", monkeypatch)
    with ui(tmp_path) as (client, _guard):
        body = client.get("/").text
    assert "What should MetaBot do?" in body
    assert 'name="prompt"' in body and 'name="folder"' in body
    assert 'name="check"' in body  # optional: a command that judges each try's work
    for field in ('name="engine"', 'name="model"', 'name="role"'):
        assert field not in body
    assert 'href="/chat"' in body  # the developer view is one link away


def test_developer_mode_puts_the_runs_list_at_home(tmp_path, monkeypatch):
    configure(tmp_path / "home", monkeypatch, ui_mode="developer")
    with ui(tmp_path) as (client, _guard):
        body = client.get("/").text
    assert "What should MetaBot do?" not in body
    assert "<h1>Runs</h1>" in body


def test_a_task_from_home_runs_as_a_ladder_on_its_endpoint_with_no_key(tmp_path, monkeypatch):
    server = FakeChat([answer("all done")])
    try:
        configure(
            tmp_path / "home",
            monkeypatch,
            endpoints={"home": {"base_url": server.url, "model": "small"}},
            ladder=[{"engine": "openai", "endpoint": "home"}],
        )
        with ui(tmp_path) as (client, guard):
            page = _post(client, guard, "/", prompt="tidy the notes", folder=str(tmp_path))
            assert page.status_code == 303, page.text
            run_id = page.headers["location"].removeprefix("/live/")
            text = _until(client, run_id, lambda t: "all done" in t)
        assert "openai@home/small" in text  # the rung it ran on, named in the trace
        assert server.requests[0]["model"] == "small"
        assert "Authorization" not in server.headers[0]  # no key was configured, none was sent
    finally:
        server.close()


def test_a_write_from_home_waits_for_an_answer_on_the_page(tmp_path, monkeypatch):
    server = FakeChat(
        [call("write_file", {"path": "note.txt", "content": "hi"}), answer("wrote it")]
    )
    try:
        configure(
            tmp_path / "home",
            monkeypatch,
            endpoints={"home": {"base_url": server.url, "model": "small"}},
            ladder=[{"engine": "openai", "endpoint": "home"}],
        )
        with ui(tmp_path) as (client, guard):
            run_id = (
                _post(client, guard, "/", prompt="write a note", folder=str(tmp_path))
                .headers["location"]
                .removeprefix("/live/")
            )
            asked = _until(client, run_id, lambda t: FORM.search(t) is not None)
            assert not (tmp_path / "note.txt").exists()  # nothing is written before the answer
            answered = _post(client, guard, f"/asks/{FORM.search(asked).group(1)}", allow="1")
            assert answered.status_code == 303
            _until(client, run_id, lambda t: "wrote it" in t)
        assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "hi"
    finally:
        server.close()


def test_an_empty_task_is_refused_on_the_page(tmp_path, monkeypatch):
    configure(tmp_path / "home", monkeypatch)
    with ui(tmp_path) as (client, guard):
        response = _post(client, guard, "/", prompt="   ", folder=str(tmp_path))
    assert response.status_code == 400
    assert "the task is empty" in response.text


def test_settings_that_do_not_read_are_shown_on_the_page_not_run(tmp_path, monkeypatch):
    configure(
        tmp_path / "home",
        monkeypatch,
        endpoints={"Bad Name": {"base_url": "http://x/v1", "model": "m"}},
    )
    with ui(tmp_path) as (client, guard):
        response = _post(client, guard, "/", prompt="do it", folder=str(tmp_path))
    assert response.status_code == 400
    assert "endpoints" in response.text


def test_a_ladder_naming_a_missing_endpoint_shows_the_run_as_failed(tmp_path, monkeypatch):
    configure(tmp_path / "home", monkeypatch, ladder=[{"engine": "openai", "endpoint": "nowhere"}])
    with ui(tmp_path) as (client, guard):
        page = _post(client, guard, "/", prompt="do it", folder=str(tmp_path))
        assert page.status_code == 303
        run_id = page.headers["location"].removeprefix("/live/")
        text = _until(client, run_id, lambda t: "failed" in t)
    assert "nowhere" in text


def test_a_task_from_home_keeps_one_id_for_its_page_its_ladder_and_its_seal(tmp_path, monkeypatch):
    server = FakeChat([answer("all done")])
    try:
        home = tmp_path / "home"
        configure(
            home,
            monkeypatch,
            endpoints={"home": {"base_url": server.url, "model": "small"}},
            ladder=[{"engine": "openai", "endpoint": "home"}],
        )
        with ui(tmp_path) as (client, guard):
            page = _post(client, guard, "/", prompt="tidy the notes", folder=str(tmp_path))
            run_id = page.headers["location"].removeprefix("/live/")
            done = _until(client, run_id, lambda t: "all done" in t and "the run" in t)
        assert "valid, 1 members" in done  # judged as the run page judges a seal
        # the page's id is the ladder's: its record, its first rung and its seal
        assert (home / "ladders" / f"{run_id}.json").is_file()
        assert (home / "receipts" / f"{run_id}.1.json").is_file()
        assert (home / "receipts" / f"{run_id}.json").is_file()
        # once this UI has stopped, the run's address still leads to it
        with ui(tmp_path) as (client, _guard):
            later = client.get(f"/live/{run_id}", follow_redirects=False)
        assert later.status_code == 303 and later.headers["location"] == f"/runs/{run_id}"
    finally:
        server.close()


def _checked(tmp_path, monkeypatch, check: str) -> str:
    """A home task on one rung that answers, judged by ``check``; the live page once it ends."""
    server = FakeChat([answer("all done")])
    try:
        configure(
            tmp_path / "home",
            monkeypatch,
            endpoints={"home": {"base_url": server.url, "model": "small"}},
            ladder=[{"engine": "openai", "endpoint": "home"}],
        )
        with ui(tmp_path) as (client, guard):
            page = _post(
                client, guard, "/", prompt="tidy the notes", folder=str(tmp_path), check=check
            )
            assert page.status_code == 303, page.text
            run_id = page.headers["location"].removeprefix("/live/")
            return _until(client, run_id, lambda t: "Spent $" in t)
    finally:
        server.close()


def test_a_check_from_home_judges_each_try(tmp_path, monkeypatch):
    (tmp_path / "notes.txt").write_text("x\n")
    failed = _checked(tmp_path, monkeypatch, "test -f tidy.txt")
    assert "its check failed" in failed and "<h2>Answer</h2>" not in failed
    assert "The last try's answer, which the ladder did not take" in failed
    (tmp_path / "again").mkdir()
    assert "Done on openai@home/small" in _checked(tmp_path / "again", monkeypatch, "true")


@pytest.mark.parametrize(
    ("check", "said"),
    [
        ("true\nrm -rf x", "one line"),
        ("true\tx", "one line"),  # a pasted tab: the run it opened failed
        ("rm -rf build", "never runs"),  # so did a check the gate would never run
    ],
)
def test_a_check_that_would_never_run_is_refused_on_the_page(tmp_path, monkeypatch, check, said):
    configure(tmp_path / "home", monkeypatch)
    with ui(tmp_path) as (client, guard):
        response = _post(client, guard, "/", prompt="do it", folder=str(tmp_path), check=check)
    assert response.status_code == 400 and said in response.text


def test_a_stop_during_the_check_shows_the_try_the_ladder_did_not_take(tmp_path, monkeypatch):
    # Review 10-28: the page said "stopped before the run began", and a stopped
    # ladder's last try was headed as its answer.
    started = tmp_path / "started"
    server = FakeChat([answer("all done")])
    try:
        configure(
            tmp_path / "home",
            monkeypatch,
            endpoints={"home": {"base_url": server.url, "model": "small"}},
            ladder=[{"engine": "openai", "endpoint": "home"}],
        )
        with ui(tmp_path) as (client, guard):
            check = f"touch {shlex.quote(str(started))} && sleep 30"
            page = _post(client, guard, "/", prompt="tidy", folder=str(tmp_path), check=check)
            run_id = page.headers["location"].removeprefix("/live/")
            deadline = time.monotonic() + 10
            while not started.exists():
                assert time.monotonic() < deadline, client.get(f"/live/{run_id}").text[-3000:]
                time.sleep(0.02)
            assert _post(client, guard, f"/live/{run_id}/stop").status_code == 303
            text = _until(client, run_id, _ended)
    finally:
        server.close()
    assert "before the run began" not in text and "<h2>Answer</h2>" not in text
    assert "The last try's answer, which the ladder did not take" in text
    assert "all done" in text


def test_a_folder_past_one_that_cannot_be_entered_is_not_a_folder(tmp_path):
    import os

    from metabot.web_app import plan_everyday

    shut = tmp_path / "shut"
    (shut / "inside").mkdir(parents=True)
    shut.chmod(0)
    try:
        if os.access(shut, os.X_OK):
            pytest.skip("this user enters any folder")
        with pytest.raises(ValueError, match="is not a folder"):
            plan_everyday({"prompt": "hi", "folder": str(shut / "inside")}, tmp_path)
    finally:
        shut.chmod(0o755)
