"""Routines: a save check, a firing row before its run, and nobody to ask."""

from __future__ import annotations

import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from emerge.criteria import compose_policy_version, criteria_digest
from mcp_types import Tool

from conftest import FAKE_MCP
from fake_chat import answer, call
from metabot.config import Config
from metabot.connections import ConnectionStore
from metabot.engines.openai_compat import OpenAIEngine
from metabot.events import Final, Started, TextEvent
from metabot.paths import run_dir
from metabot.receipts import journal_matches, verify_file
from metabot.routines import (
    RoutineError,
    RoutineSpec,
    check_routine,
    fire,
    firings,
    get_routine,
    next_due,
    remove_routine,
    routines,
    save_routine,
    set_enabled,
)

LONDON = ZoneInfo("Europe/London")
TOOLS = ["list_issues", "create_issue", "delete_repo", "frobnicate"]


def _tools(_conn) -> list[Tool]:
    return [Tool(name=n, input_schema={"type": "object", "properties": {}}) for n in TOOLS]


@pytest.fixture
def count_file(tmp_path) -> Path:
    return tmp_path / "count"


@pytest.fixture
def gh(count_file):
    return ConnectionStore().add(
        name="gh",
        transport="stdio",
        command=[sys.executable, str(FAKE_MCP), "--count-file", str(count_file)],
    )


def _spec(tmp_path: Path, **changes) -> RoutineSpec:
    fields = {
        "name": "triage",
        "goal": "Triage the new issues.",
        "engine": "openai",
        "model": "tiny",
        "cwd": str(tmp_path),
        "connections": ["gh"],
        "scope_allow": [],
        "criteria": {},
        "cron": "0 9 * * 1-5",
        "tz": "Europe/London",
    }
    return RoutineSpec(**{**fields, **changes})


def _check(spec: RoutineSpec) -> dict[str, str]:
    return check_routine(spec, ConnectionStore(), _tools)


def test_allows_resolve_to_the_connections_did_with_their_class(tmp_path, gh):
    allows = _check(_spec(tmp_path, scope_allow=["gh__create_issue", f"{gh.id}#list_issues"]))
    assert allows == {f"{gh.id}#create_issue": "write", f"{gh.id}#list_issues": "read"}


def test_destructive_allow_is_refused(tmp_path, gh):
    with pytest.raises(RoutineError) as caught:
        _check(_spec(tmp_path, scope_allow=["gh__delete_repo"]))
    assert caught.value.field == "scope_allow"
    assert (
        "can never be allowed on a routine (a destructive call always waits for a person)"
        in str(caught.value)
    )


def test_an_unrecognised_capability_resolves_to_destructive_and_is_refused(tmp_path, gh):
    with pytest.raises(RoutineError, match="resolves to destructive"):
        _check(_spec(tmp_path, scope_allow=["gh__frobnicate"]))


def test_unknown_capability_is_refused(tmp_path, gh):
    with pytest.raises(RoutineError, match="is not a capability of gh"):
        _check(_spec(tmp_path, scope_allow=["gh__nope"]))


def test_an_allow_must_name_one_of_the_routines_connections(tmp_path, gh):
    ConnectionStore().add(name="other", transport="stdio", command=["x"])
    with pytest.raises(RoutineError, match="one of this routine's connections"):
        _check(_spec(tmp_path, scope_allow=["other__create_issue"]))


def test_a_revoked_connection_is_refused(tmp_path, gh):
    ConnectionStore().revoke("gh")
    with pytest.raises(RoutineError, match="revoked"):
        _check(_spec(tmp_path))


def test_a_connection_naming_a_plugins_copy_outside_this_home_is_refused(tmp_path):
    word = f"rh/plugins/tools@{'c' * 64}/server.py"  # as 0.2.0 saved it under METABOT_HOME=rh
    ConnectionStore().add(name="tools-db", transport="stdio", command=["python3", word])
    with pytest.raises(RoutineError) as caught:
        _check(_spec(tmp_path, connections=["tools-db"]))
    # before: saved, and each firing skipped it without a word; with an allow on it, "did not
    # answer: PermissionError", with no way out named
    assert caught.value.field == "connections"
    assert f"metabot plugin enable {'c' * 12} --add-servers" in str(caught.value)


def test_a_missing_connection_is_refused(tmp_path):
    with pytest.raises(RoutineError, match="no connection named"):
        _check(_spec(tmp_path, connections=["ghost"]))


@pytest.mark.parametrize(
    ("criteria", "says"),
    [
        ({"counts_match": True}, "unsupported criterion: counts_match"),
        ({"exit_zero": "yes"}, "true or false"),
        ({f"k{n}": True for n in range(9)}, "at most 8"),
    ],
)
def test_unsupported_criterion_is_refused(tmp_path, gh, criteria, says):
    with pytest.raises(RoutineError, match=says):
        _check(_spec(tmp_path, criteria=criteria))


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"cron": "0 0 31 2 *"}, "cron"),
        ({"cron": "61 * * * *"}, "cron"),
        ({"tz": "Mars/Olympus"}, "tz"),
        ({"engine": "nope"}, "engine"),
        ({"goal": "  "}, "goal"),
        ({"name": ""}, "name"),
        ({"turns": 0}, "turns"),
    ],
)
def test_the_rest_of_a_routine_is_checked_too(tmp_path, gh, changes, field):
    with pytest.raises(RoutineError) as caught:
        _check(_spec(tmp_path, **changes))
    assert caught.value.field == field


def test_a_cwd_that_is_not_a_folder_is_refused(tmp_path, gh):
    with pytest.raises(RoutineError) as caught:
        _check(_spec(tmp_path, cwd=str(tmp_path / "gone")))
    assert caught.value.field == "cwd"


def test_save_gives_an_id_and_keeps_the_resolved_allows(tmp_path, home, gh):
    rid = save_routine(_spec(tmp_path, scope_allow=["gh__create_issue"]), list_tools=_tools)
    assert re.fullmatch(r"rt-[0-9a-f]{12}", rid)
    record = get_routine(rid)
    assert record["allows"] == {f"{gh.id}#create_issue": "write"}
    assert record["connection_ids"] == [gh.id]
    assert json.loads((home / "routines.json").read_text())[rid]["name"] == "triage"
    set_enabled(rid, False)
    assert get_routine(rid)["enabled"] is False
    again = save_routine(_spec(tmp_path, goal="Again."), list_tools=_tools, routine_id=rid)
    assert again == rid and get_routine(rid)["goal"] == "Again."


def test_names_are_unique_and_a_routine_can_be_removed(tmp_path, gh):
    rid = save_routine(_spec(tmp_path), list_tools=_tools)
    with pytest.raises(RoutineError) as caught:
        save_routine(_spec(tmp_path), list_tools=_tools)
    assert caught.value.field == "name"
    assert get_routine("triage")["id"] == rid
    assert remove_routine("triage")["id"] == rid
    assert routines() == []
    with pytest.raises(KeyError):
        get_routine(rid)


def test_next_due_is_in_the_routines_zone(tmp_path, gh):
    record = get_routine(save_routine(_spec(tmp_path), list_tools=_tools))
    due = next_due(record, datetime(2026, 10, 16, 8, 30, tzinfo=UTC))  # a Friday
    assert due == datetime(2026, 10, 19, 9, 0, tzinfo=LONDON)  # 09:00 on Monday, London time


def _local(fake):
    return lambda name: OpenAIEngine(base_url=fake.url, model="tiny", config=Config())


def _steps(firing) -> list[dict]:
    return json.loads((run_dir(firing.run_id) / "journal.json").read_text())["steps"]


def _verdicts(firing) -> list[dict]:
    receipt = json.loads(Path(firing.receipt).read_text())
    return receipt["verdicts"]


class Watcher:
    """An in-process engine that looks at the firings file when it starts."""

    name = "watch"
    tool_prefix = "watch"
    skip_tools = ()

    def __init__(self, home: Path) -> None:
        self.home = home
        self.seen: list[dict] = []

    async def run(self, job, *, board=None, approve):
        lines = (self.home / "firings.jsonl").read_text().splitlines()
        self.seen = [json.loads(line) for line in lines]
        yield Started("s-1", job.model)
        yield Final("watched", "s-1")


async def test_a_firing_is_a_row_before_it_is_a_run(tmp_path, home, gh):
    rid = save_routine(_spec(tmp_path, connections=[]), list_tools=_tools)
    watcher = Watcher(home)
    firing = await fire(rid, make_engine=lambda name: watcher)
    assert [(r["firing_id"], r["state"], r["run_id"]) for r in watcher.seen] == [
        (firing.firing_id, "running", firing.run_id)
    ]
    assert firing.state == "sealed" and firing.reason is None
    assert [(f.firing_id, f.state) for f in firings(rid)] == [(firing.firing_id, "sealed")]
    assert verify_file(firing.receipt).valid and journal_matches(firing.run_id)


async def test_a_scheduled_firing_records_its_due_time(tmp_path, home, gh):
    rid = save_routine(_spec(tmp_path, connections=[]), list_tools=_tools)
    due = datetime(2026, 10, 12, 9, 0, tzinfo=LONDON)
    firing = await fire(rid, due=due, make_engine=lambda name: Watcher(home))
    assert firing.due == "2026-10-12T08:00:00Z"  # due times are kept in UTC
    meta = json.loads((run_dir(firing.run_id) / "journal.json").read_text())["meta"]
    assert (meta["routine"], meta["routine_name"], meta["firing"], meta["due"]) == (
        rid,
        "triage",
        firing.firing_id,
        "2026-10-12T08:00:00Z",
    )
    manual = await fire(rid, make_engine=lambda name: Watcher(home))
    assert manual.due is None


async def test_a_firing_never_asks_a_human(chat, tmp_path, gh, count_file):
    rid = save_routine(_spec(tmp_path), list_tools=_tools)
    fake = chat(
        script=[
            call("write_file", {"path": "note.txt", "content": "x"}, "c1"),
            call("gh__create_issue", {"title": "t"}, "c2"),
            answer("done"),
        ]
    )
    firing = await fire(rid, make_engine=_local(fake))
    assert firing.state == "sealed"
    assert not (tmp_path / "note.txt").exists() and not count_file.exists()
    declined = [v for v in _verdicts(firing) if v["check"].startswith("scope_approval:")]
    assert [v["detail"] for v in declined] == ["declined: no human present"] * 2
    assert verify_file(firing.receipt).valid


async def test_allowed_write_runs_unattended(chat, tmp_path, gh, count_file):
    rid = save_routine(_spec(tmp_path, scope_allow=["gh__create_issue"]), list_tools=_tools)
    fake = chat(script=[call("gh__create_issue", {"title": "t"}, "c1"), answer("filed")])
    firing = await fire(rid, make_engine=_local(fake))
    assert firing.state == "sealed" and firing.reason is None
    assert count_file.read_text().split() == ["create_issue"]
    [step] = _steps(firing)
    assert step["tool"] == f"{gh.id}#create_issue" and step["success"] is True
    allowed = [v for v in _verdicts(firing) if v["check"] == f"scope_allow:{step['call_id']}"]
    assert allowed == [
        {
            "check": f"scope_allow:{step['call_id']}",
            "result": "pass",
            "detail": f"allows {gh.id}#create_issue",
        }
    ]
    assert verify_file(firing.receipt).valid and journal_matches(firing.run_id)


async def test_a_firing_reaches_only_its_own_connections(chat, tmp_path, gh, count_file):
    other_count = tmp_path / "other-count"
    ConnectionStore().add(
        name="other",
        transport="stdio",
        command=[sys.executable, str(FAKE_MCP), "--count-file", str(other_count)],
    )
    rid = save_routine(_spec(tmp_path), list_tools=_tools)
    fake = chat(script=[call("other__list_issues", {}, "c1"), answer("done")])
    firing = await fire(rid, make_engine=_local(fake))
    offered = {t["function"]["name"] for t in fake.requests[0]["tools"]}
    assert "gh__list_issues" in offered and not any(n.startswith("other__") for n in offered)
    assert not other_count.exists() and firing.state == "sealed"


async def test_criteria_give_declared_acceptance(chat, tmp_path, gh):
    criteria = {"exit_zero": True}
    rid = save_routine(_spec(tmp_path, connections=[], criteria=criteria), list_tools=_tools)
    fake = chat(script=[call("shell", {"command": "echo hi"}, "c1"), answer("ok")])
    firing = await fire(rid, make_engine=_local(fake))
    receipt = json.loads(Path(firing.receipt).read_text())
    assert receipt["policy_version"] == compose_policy_version(
        "metabot/1.0", criteria_digest(criteria)
    )
    assert {"check": "declared_acceptance", "result": "pass", "detail": "ok"} in [
        {k: v.get(k) for k in ("check", "result", "detail")} for v in receipt["verdicts"]
    ]
    assert json.loads(_steps(firing)[0]["output"])["exit_code"] == 0
    assert firing.acceptance == "pass" and verify_file(firing.receipt).valid


async def test_a_nonzero_exit_fails_declared_acceptance(chat, tmp_path, gh):
    criteria = {"exit_zero": True}
    rid = save_routine(_spec(tmp_path, connections=[], criteria=criteria), list_tools=_tools)
    fake = chat(script=[call("shell", {"command": "ls no-such-file"}, "c1"), answer("ok")])
    firing = await fire(rid, make_engine=_local(fake))
    verdicts = {v["check"]: v for v in _verdicts(firing)}
    assert verdicts["declared_acceptance"]["result"] == "fail"
    assert verdicts["declared_acceptance"]["detail"] == "exit_zero: nonzero exit: 2"
    assert firing.state == "sealed" and firing.acceptance == "fail"


async def test_declared_criteria_with_nothing_to_judge_fail_the_run(chat, tmp_path, gh):
    rid = save_routine(
        _spec(tmp_path, connections=[], criteria={"exit_zero": True}), list_tools=_tools
    )
    fake = chat(script=[answer("nothing run")])
    firing = await fire(rid, make_engine=_local(fake))
    verdicts = {v["check"]: v for v in _verdicts(firing)}
    assert verdicts["declared_acceptance"]["result"] == "fail"


async def test_a_connection_naming_a_plugins_copy_outside_this_home_refuses_the_firing(
    tmp_path, home, gh
):
    rid = save_routine(_spec(tmp_path), list_tools=_tools)
    doc = json.loads((home / "connections.json").read_text(encoding="utf-8"))
    doc["connections"][0]["command"] = ["python3", f"rh/plugins/tools@{'c' * 64}/server.py"]
    (home / "connections.json").write_text(json.dumps(doc), encoding="utf-8")
    firing = await fire(rid, make_engine=lambda name: pytest.fail("ran"))
    # before: it fired without the connection, saying nothing of it
    assert firing.state == "refused"
    assert f"metabot plugin enable {'c' * 12} --add-servers" in firing.reason


async def test_a_connection_revoked_after_save_refuses_the_firing(tmp_path, gh):
    rid = save_routine(_spec(tmp_path), list_tools=_tools)
    ConnectionStore().revoke("gh")
    firing = await fire(rid, make_engine=lambda name: pytest.fail("ran"))
    assert firing.state == "refused" and "revoked" in firing.reason
    assert firing.receipt is None
    assert [f.state for f in firings(rid)] == ["refused"]


async def test_a_connections_file_that_does_not_read_refuses_the_firing(tmp_path, gh):
    rid = save_routine(_spec(tmp_path), list_tools=_tools)
    path = ConnectionStore().path
    path.write_text("{not json", encoding="utf-8")
    firing = await fire(rid, make_engine=lambda name: pytest.fail("ran"))
    assert firing.state == "refused" and firing.reason.startswith(f"cannot read {path}")
    assert [f.state for f in firings(rid)] == ["refused"]


async def test_a_config_file_that_does_not_read_refuses_the_firing(tmp_path, gh, home):
    rid = save_routine(_spec(tmp_path), list_tools=_tools)
    path = home / "config.json"
    path.write_text('{"models": "haiku"}', encoding="utf-8")
    firing = await fire(rid, make_engine=lambda name: pytest.fail("ran"))
    assert firing.state == "refused" and firing.reason.startswith(f"cannot read {path}")
    assert firing.run_id is None and firing.receipt is None
    assert [f.state for f in firings(rid)] == ["refused"]


async def test_an_engine_that_breaks_leaves_an_error_row(tmp_path, gh):
    rid = save_routine(_spec(tmp_path, connections=[]), list_tools=_tools)

    def broken(name):
        raise RuntimeError("no such engine here")

    firing = await fire(rid, make_engine=broken)
    assert firing.state == "error" and "no such engine here" in firing.reason
    assert [f.state for f in firings(rid)] == ["error"]


class BreaksPartWay:
    """An engine adapter that breaks once its run has begun: the run still seals."""

    name = "fake"
    tool_prefix = "fake"
    skip_tools = ()

    async def run(self, job, *, board=None, approve):
        yield Started("s-1", job.model)
        yield TextEvent("partly")
        raise RuntimeError("adapter bug")


async def test_an_engine_that_breaks_part_way_leaves_its_receipt_on_the_row(tmp_path, gh):
    rid = save_routine(_spec(tmp_path, connections=[]), list_tools=_tools)
    firing = await fire(rid, make_engine=lambda name: BreaksPartWay())
    assert firing.state == "error" and "adapter bug" in firing.reason
    assert firing.receipt and verify_file(firing.receipt).valid
    assert firing.receipt.endswith(f"{firing.run_id}.json")
