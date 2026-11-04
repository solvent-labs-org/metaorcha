"""Office runs started in the web UI: desks chosen in a form, asks answered in the page."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import html
import json
import os
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from conftest import not_on_path
from metabot.approvals import DenyAll
from metabot.office import load_office
from metabot.office_run import office_journal_path, run_relay
from metabot.paths import receipts_dir
from metabot.receipts import verify_file
from metabot.runner import RunCancelled
from metabot.web_app import make_app
from metabot.web_guard import COOKIE, Guard
from metabot.web_runs import Broker
from test_office import _linked_office
from test_office_run import DESKS, TASK, DeskEngine, _build, _repo
from test_web_runs import (
    FORM,
    PAYLOADS,
    PORT,
    TOKEN,
    Asks,
    Sleeps,
    _asked,
    _ended,
    _journal,
    _post,
    _until,
)

ADVISED = {**DESKS, "qa": {"role": "roles/qa.md", "harness": "cursor", "advises": ["build"]}}


@pytest.fixture(autouse=True)
def _no_pin(monkeypatch):
    """No Code Desks pin: the office is the one the folder names."""
    monkeypatch.delenv("DESK_OFFICE", raising=False)


def _engines() -> dict:
    return {
        "claude": DeskEngine("claude", act=lambda job: "PLAN: write feature.txt"),
        "codex": DeskEngine("codex", act=_build),
        "cursor": DeskEngine("cursor", act=lambda job: "REVIEW: fine"),
    }


@contextlib.contextmanager
def office_ui(start: Path, engines: dict) -> Iterator[tuple[TestClient, Guard]]:
    """The UI as ``metabot ui`` serves it from ``start``; each desk gets its engine by name."""
    guard = Guard(TOKEN, PORT)
    app = make_app(guard, start_dir=start, make_engine=lambda name: engines[name])
    with TestClient(app, base_url=f"http://127.0.0.1:{PORT}", follow_redirects=False) as client:
        client.cookies.set(COOKIE, TOKEN)
        yield client, guard


def _send(client: TestClient, guard: Guard, root: Path, desks, **fields):
    form = {"csrf": guard.csrf, "prompt": TASK, "folder": str(root), "mode": "relay", **fields}
    return client.post(
        "/office", data={**form, "desk": list(desks)}, headers={"origin": guard.origin}
    )


def _start(client: TestClient, guard: Guard, root: Path, *desks: str, **fields) -> str:
    page = _send(client, guard, root, desks, **fields)
    assert page.status_code == 303, page.text
    office_id = page.headers["location"].removeprefix("/live/")
    assert page.headers["location"] == f"/live/{office_id}"
    return office_id


def _record(office_id: str) -> dict:
    return json.loads(office_journal_path(office_id).read_text(encoding="utf-8"))


# ── choosing ────────────────────────────────────────────────────────────────


def test_the_office_page_offers_the_desks_of_the_office_the_ui_started_in(tmp_path):
    root = _repo(tmp_path, ADVISED)
    with office_ui(root, {}) as (client, _guard):
        text = client.get("/office").text
        chat = client.get("/chat").text
    assert '<a href="/office">Office</a>' in chat  # every page links it
    for name in ("plan", "build", "review"):
        assert f'name="desk" value="{name}" checked>' in text
    assert 'value="qa"' not in text  # an advisor does not run alone
    assert "codex/m-build" in text and "qa" in text
    assert '<option value="relay" selected>' in text and '<option value="fan">' in text
    assert 'name="advisors" value="1" checked>' in text
    assert 'name="checks"' not in text  # the office has no checks
    assert f'name="folder" value="{html.escape(str(root))}"' in text


@pytest.mark.parametrize("start", ["the main tree", "a worktree git cannot read"])
def test_an_office_whose_folder_name_is_not_utf_8_is_shown_and_run(tmp_path, start):
    team, here = _linked_office(tmp_path / os.fsdecode(b"caf\xe9"), tmp_path / "linked")
    record = team.parent / ".git" / "worktrees" / "linked"
    record.chmod(0o000)  # git cannot read the worktree by its record: the office is the main's
    try:
        if os.access(record, os.X_OK):
            pytest.skip("this user enters any folder")
        with office_ui(team if start == "the main tree" else here, _engines()) as (client, guard):
            page = client.get("/office")  # before: a server error
            assert page.status_code == 200 and "main-office" in page.text
            assert "caf\\udce9" in page.text  # the byte that is not UTF-8, shown as text
            if start != "the main tree":  # a folder the page can name back
                office_id = _start(client, guard, here, "dev")
                assert "main-office" in _until(client, office_id, _ended)
    finally:
        record.chmod(0o755)


def test_a_desk_whose_kit_cannot_load_is_offered_unticked_with_why(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "person"))  # where Code Desks looks last
    root = _repo(tmp_path, {**DESKS, "plan": {**DESKS["plan"], "kit": ["no-such-skill"]}})
    with office_ui(root, {}) as (client, _guard):
        text = client.get("/office").text
    assert 'name="desk" value="plan">' in text and 'name="desk" value="build" checked>' in text
    assert "kit: skill &#x27;no-such-skill&#x27; is not in" in text


def test_a_kit_place_it_cannot_enter_is_named_and_not_a_server_error(
    tmp_path, monkeypatch, path_checks
):
    import os

    monkeypatch.setenv("HOME", str(tmp_path / "person"))
    root = _repo(tmp_path, {**DESKS, "plan": {**DESKS["plan"], "kit": ["x"]}})
    shut = root / ".office"
    (shut / "skills" / "x").mkdir(parents=True)
    shut.chmod(0o600)
    try:
        if os.access(shut, os.X_OK):
            pytest.skip("this user enters any folder")
        with office_ui(root, {}) as (client, _guard):
            page = client.get("/office")
    finally:
        shut.chmod(0o755)
    assert page.status_code == 200 and 'name="desk" value="plan">' in page.text
    assert "cannot be read: Permission denied" in page.text


def test_a_folder_is_looked_up_and_one_with_no_office_says_so(tmp_path):
    root = _repo(tmp_path)
    empty = tmp_path / "empty"
    empty.mkdir()
    with office_ui(empty, {}) as (client, _guard):
        none = client.get("/office").text
        found = client.get("/office", params={"folder": str(root)}).text
        missing = client.get("/office", params={"folder": str(tmp_path / "nope")}).text
    assert "no office.yaml in" in none and 'name="desk"' not in none
    assert 'name="desk" value="plan" checked>' in found
    assert "is not a folder" in missing and 'name="desk"' not in missing


def test_a_folder_that_cannot_be_entered_is_refused_and_not_a_server_error(tmp_path, home):
    import os

    root = _repo(tmp_path)
    shut = tmp_path / "shut"
    shut.mkdir()
    shut.chmod(0o600)
    try:
        if os.access(shut, os.X_OK):
            pytest.skip("this user enters any folder")
        engines = _engines()
        with office_ui(root, engines) as (client, guard):
            page = client.get("/office", params={"folder": str(shut)})
            sent = _send(client, guard, root, ("plan",), folder=str(shut))
    finally:
        shut.chmod(0o755)
    assert page.status_code == 200 and "cannot be entered" in page.text
    assert sent.status_code == 400 and "cannot be entered" in sent.text
    assert not any(engine.jobs for engine in engines.values())


def test_an_office_it_cannot_look_at_says_why_and_is_not_a_server_error(
    tmp_path, home, monkeypatch, path_checks
):
    import os

    shut = tmp_path / "shut"
    under = shut / "here"
    under.mkdir(parents=True)
    (shut / "office.yaml").write_text("name: t\n", encoding="utf-8")
    linked = tmp_path / "linked"
    linked.mkdir()
    (linked / "office.yaml").symlink_to(shut / "office.yaml")
    loop = tmp_path / "loop"
    loop.symlink_to(loop)
    engines = _engines()
    shut.chmod(0o600)
    try:
        if os.access(shut, os.X_OK):
            pytest.skip("this user enters any folder")
        with office_ui(under, engines) as (client, guard):
            started_under = client.get("/office")
            through = client.get("/office", params={"folder": str(linked)})
            sent = _send(client, guard, linked, ("plan",))
            monkeypatch.setenv("DESK_OFFICE", str(loop))
            pinned = client.get("/office")
    finally:
        shut.chmod(0o755)
    assert started_under.status_code == 200 and "is not a folder" in started_under.text
    for page in (through, sent):
        assert f"cannot read {linked / 'office.yaml'}: Permission denied" in page.text
    assert through.status_code == 200 and sent.status_code == 400
    assert pinned.status_code == 200 and "is not a folder" in pinned.text
    assert not any(engine.jobs for engine in engines.values())


def test_a_worktree_whose_git_file_names_no_record_reads_its_own_office(tmp_path, home):
    from test_office import _linked_office

    _team, here = _linked_office(tmp_path)
    (here.parent / ".git").write_bytes(b"gitdir: /nowhere\x00x/.git/worktrees/linked\n")
    with office_ui(here, {}) as (client, _guard):
        page = client.get("/office")
    assert page.status_code == 200, page.text  # before: a server error
    assert f'name="folder" value="{html.escape(str(here))}"' in page.text


@pytest.mark.parametrize(
    ("fields", "desks", "said"),
    [
        ({"prompt": "  "}, ("plan",), "the task is empty"),
        ({}, ("nope",), "no desk named"),
        ({}, ("x" * 300,), "no desk named"),
        ({}, (), "choose at least one desk"),
        ({}, ("plan", "plan"), "a desk is named twice"),
        ({"mode": "swarm"}, ("plan",), "unknown mode"),
        ({"checks": "1"}, ("plan",), "the office has no checks to run"),
        ({"folder": "EMPTY"}, ("plan",), "no office.yaml in"),
    ],
)
def test_a_form_that_cannot_start_says_why_and_starts_nothing(tmp_path, home, fields, desks, said):
    root = _repo(tmp_path)
    (tmp_path / "empty").mkdir()
    if fields.get("folder") == "EMPTY":
        fields = {**fields, "folder": str(tmp_path / "empty")}
    engines = _engines()
    with office_ui(root, engines) as (client, guard):
        page = _send(client, guard, root, desks, **{"prompt": PAYLOADS[0], **fields})
        listed = client.get("/office").text
    assert page.status_code == 400 and said in page.text
    assert PAYLOADS[0] not in page.text  # what was typed comes back as text
    assert "x" * 41 not in page.text  # a name that is no desk's is cut short
    if not {"prompt", "folder"} & fields.keys():  # with no office, there is no form to fill
        assert html.escape(PAYLOADS[0]) in page.text
    assert "None yet." in listed and not (home / "offices").exists()
    assert not any(engine.jobs for engine in engines.values())


# ── running ─────────────────────────────────────────────────────────────────


def test_a_relay_started_in_the_page_runs_its_desks_and_shows_them(tmp_path):
    root = _repo(tmp_path)
    with office_ui(root, _engines()) as (client, guard):
        office_id = _start(client, guard, root, "plan", "build", "review")
        text = _until(client, office_id, _ended)
        listed = client.get("/office").text
    assert "<th>state</th><td>done</td>" in text and "<th>mode</th><td>relay</td>" in text
    assert f'href="/offices/{office_id}"' in text
    record = _record(office_id)
    assert (record["office_run_id"], record["mode"], record["task"]) == (office_id, "relay", TASK)
    runs = [f"{office_id}.{name}" for name in ("plan", "build", "review")]
    assert [desk["run_id"] for desk in record["desks"]] == runs
    assert all(verify_file(receipts_dir() / f"{run}.json").valid for run in runs)
    for name in ("plan", "build", "review"):
        assert f"[metabot] desk {name}:" in text  # each desk's start, in the trace
    assert all(f'href="/runs/{run}"' in text for run in runs)  # the desks, on the page
    assert f"metabot desks pick {office_id}/review" in text
    assert f'href="/live/{office_id}"' in listed and "office shop: relay" in listed


def test_a_desk_asks_on_the_office_run_page_and_the_ask_names_it(tmp_path):
    root = _repo(tmp_path, {"build": DESKS["build"]})
    with office_ui(root, {"codex": Asks()}) as (client, guard):
        office_id = _start(client, guard, root, "build")
        text = _until(client, office_id, _asked)
        assert "<td>build</td><td>running</td>" in text  # its row, while it waits
        assert "<b>build</b>: <b>write</b> call <code>fake/Edit</code>" in text
        assert "[metabot] build: write call fake/Edit waits for you" in text
        ask_id = FORM.search(text).group(1)
        answered = _post(client, guard, f"/asks/{ask_id}", allow="1")
        assert answered.headers["location"] == f"/live/{office_id}"
        text = _until(client, office_id, _ended)
    assert "<th>state</th><td>done</td>" in text
    assert "[metabot] build: fake/Edit allowed in the page" in text
    [verdict] = [
        v
        for v in _journal(f"{office_id}.build")["verdicts"]
        if v["check"] == "scope_approval:call-1"
    ]
    assert verdict["result"] == "pass" and verdict["detail"].startswith("did:orcha:agent:")


def test_a_fan_started_in_the_page_runs_the_desks_at_once(tmp_path):
    root = _repo(tmp_path)
    with office_ui(root, _engines()) as (client, guard):
        office_id = _start(client, guard, root, "plan", "build", mode="fan")
        text = _until(client, office_id, _ended)
    assert "<th>state</th><td>done</td>" in text and "<th>mode</th><td>fan</td>" in text
    record = _record(office_id)
    assert record["mode"] == "fan" and [d["desk"] for d in record["desks"]] == ["plan", "build"]
    assert "plan| " in text and "build| " in text  # each desk's lines name it


@pytest.mark.parametrize(("advisors", "labels"), [("1", ["build", "qa.on-build"]), ("", ["build"])])
def test_advisors_review_a_relay_when_the_form_asks(tmp_path, advisors, labels):
    root = _repo(tmp_path, ADVISED)
    with office_ui(root, _engines()) as (client, guard):
        office_id = _start(client, guard, root, "build", advisors=advisors)
        _until(client, office_id, _ended)
    assert [desk["label"] for desk in _record(office_id)["desks"]] == labels


@pytest.mark.parametrize("mode", ["relay", "fan"])
def test_stopping_an_office_run_stops_its_desks_and_keeps_its_record(tmp_path, mode):
    desks = ("plan",) if mode == "relay" else ("plan", "build")
    root = _repo(tmp_path, {name: DESKS[name] for name in desks})
    with office_ui(root, {"claude": Sleeps(), "codex": Sleeps()}) as (client, guard):
        office_id = _start(client, guard, root, *desks, mode=mode)
        _until(client, office_id, lambda text: text.count("[metabot] started") == len(desks))
        stopped = _post(client, guard, f"/live/{office_id}/stop")
        assert stopped.status_code == 303
        text = _until(client, office_id, _ended)
    assert "<th>state</th><td>stopped</td>" in text and "before the run began" not in text
    assert "<h2>Pick</h2>" not in text  # a stopped run offers nothing to pick
    record = _record(office_id)
    assert [desk["error"] for desk in record["desks"]] == ["cancelled"] * len(desks)
    assert record["finished_at"]
    assert all(verify_file(receipts_dir() / f"{office_id}.{n}.json").valid for n in desks)


async def test_a_stopped_office_run_leaves_its_record_to_say_how_far_it_got():
    broker = Broker()
    sealed = object()

    async def seals() -> None:
        raise RunCancelled(sealed)

    for kind, office in (("mb-run", None), ("mb-office", "shop")):
        broker.open(f"{kind}-1", cwd="/", prompt="p", office=office)
        broker.start(f"{kind}-1", asyncio.sleep(60))
        broker.open(f"{kind}-2", cwd="/", prompt="p", office=office)
        broker.start(f"{kind}-2", seals())
    await asyncio.sleep(0)  # each begins
    for run_id in list(broker.runs):
        broker.stop(run_id)
    await broker.close()
    said = {run_id: (live.state, live.error, live.result) for run_id, live in broker.runs.items()}
    assert said == {
        "mb-run-1": ("stopped", "stopped before the run began", None),
        "mb-run-2": ("stopped", None, sealed),
        "mb-office-1": ("stopped", None, None),  # its record says how far it got
        "mb-office-2": ("stopped", None, None),  # one desk's run is not the office's result
    }


def test_an_office_run_that_cannot_open_says_why_on_its_page(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "person"))  # where Code Desks looks last
    root = _repo(tmp_path, {"plan": {**DESKS["plan"], "kit": ["no-such-skill"]}})
    engines = _engines()
    with office_ui(root, engines) as (client, guard):
        office_id = _start(client, guard, root, "plan")
        text = _until(client, office_id, _ended)
    assert "<th>state</th><td>failed</td>" in text
    assert "desks.plan.kit: skill" in text and "OfficeError" not in text
    assert not office_journal_path(office_id).exists() and not engines["claude"].jobs
    assert f'href="/offices/{office_id}"' not in text  # there is no record to show


def test_an_office_run_whose_record_cannot_be_written_did_not_end_well(tmp_path, monkeypatch):
    from metabot import office_run

    saved = office_run._save

    def save(record, rows):  # the disk fills as the office run closes
        if record.get("finished_at"):
            raise OSError(errno.ENOSPC, "No space left on device")
        return saved(record, rows)

    monkeypatch.setattr(office_run, "_save", save)
    root = _repo(tmp_path)
    with office_ui(root, _engines()) as (client, guard):
        office_id = _start(client, guard, root, "plan", "build", "review")
        text = _until(client, office_id, _ended)
    assert "<th>state</th><td>failed</td>" in text
    said = "the office's record could not be written: [Errno 28] No space left on device"
    assert f'<p class="bad">{html.escape(said)}</p>' in text
    assert html.escape(f"[metabot] {said}") in text  # and in its trace, as it closed


def test_the_live_address_of_an_office_run_from_elsewhere_goes_to_its_record(tmp_path):
    root = _repo(tmp_path)
    engines = _engines()
    done = asyncio.run(
        run_relay(
            load_office(root),
            TASK,
            approver=DenyAll(),
            desks=["plan"],
            make_engine=lambda name: engines[name],
        )
    )
    with office_ui(root, engines) as (client, _guard):
        page = client.get(f"/live/{done.office_run_id}")
    assert page.status_code == 303
    assert page.headers["location"] == f"/offices/{done.office_run_id}"


# ── the page stays free while an office run works ─────────────────────────────


@pytest.mark.parametrize("step", ["toplevel", "commit_desk"])
def test_an_office_run_s_slow_git_step_leaves_the_other_pages_free(tmp_path, monkeypatch, step):
    """Opening the office run, and each desk's git work, run off the page's event loop."""
    import metabot.office_run as office_run

    real = getattr(office_run, step)
    entered, slow = threading.Event(), {"on": False}

    def slowed(*args, **kwargs):
        if slow["on"]:
            entered.set()
            time.sleep(1.5)  # a slow disk, a big repo, or git's own timeout
        return real(*args, **kwargs)

    monkeypatch.setattr(office_run, step, slowed)
    root = _repo(tmp_path, {"plan": DESKS["plan"]})
    engine = DeskEngine("claude", act=_build)
    with office_ui(root, {"claude": engine}) as (client, guard):
        slow["on"] = True
        office_id = _start(client, guard, root, "plan")
        assert entered.wait(10)
        began = time.monotonic()
        assert client.get("/").status_code == 200  # a page that has nothing to do with the run
        took = time.monotonic() - began
        _until(client, office_id, _ended)
    assert took < 0.75, f"a page waited {took:.1f}s on the office run's {step}"


def test_a_stop_during_a_desk_s_git_step_waits_for_it_so_the_record_holds_its_commit(
    tmp_path, monkeypatch
):
    import metabot.office_run as office_run

    real, entered = office_run.commit_desk, threading.Event()

    def slowed(*args, **kwargs):
        entered.set()
        time.sleep(1.0)
        return real(*args, **kwargs)

    monkeypatch.setattr(office_run, "commit_desk", slowed)
    root = _repo(tmp_path, {"plan": DESKS["plan"]})
    with office_ui(root, {"claude": DeskEngine("claude", act=_build)}) as (client, guard):
        office_id = _start(client, guard, root, "plan")
        assert entered.wait(10)
        assert _post(client, guard, f"/live/{office_id}/stop").status_code == 303
        text = _until(client, office_id, _ended)
    assert "<th>state</th><td>stopped</td>" in text
    (desk,) = _record(office_id)["desks"]
    assert desk["commit"] and desk["stat"]  # git finished, and the record says so


def test_the_broker_never_opens_a_second_run_under_one_id():
    broker = Broker()
    broker.open("mb-one", cwd="/", prompt="p")
    with pytest.raises(ValueError, match="run mb-one is open already"):
        broker.open("mb-one", cwd="/", prompt="q")
    assert broker.runs["mb-one"].prompt == "p"


def test_the_page_never_says_done_beside_a_record_read_before_the_end(tmp_path, monkeypatch):
    """The state is read before the record, so 'done' always comes with the record's pick."""
    from metabot import web_data

    real = web_data.office_view
    held: list = []

    def read_then_wait(run_id):
        view = real(run_id)
        if not held and view is not None and view.get("offer") is None:
            held.append(run_id)  # this page holds a record read while the run went on
            deadline = time.monotonic() + 10
            while "offer" not in _record(run_id) and time.monotonic() < deadline:
                time.sleep(0.02)
            time.sleep(0.2)  # the run ends while this page is being made
        return view

    monkeypatch.setattr(web_data, "office_view", read_then_wait)
    root = _repo(tmp_path)
    with office_ui(root, _engines()) as (client, guard):
        office_id = _start(client, guard, root, "plan", "build", "review")
        text = _until(client, office_id, _ended)
    assert held and "<th>state</th><td>done</td>" in text
    assert f"metabot desks pick {office_id}/" in text


def test_the_page_never_shows_an_ask_beside_a_record_read_before_its_desk_began(
    tmp_path, monkeypatch
):
    """The asks are read before the record, so a desk that asks always has its row."""
    import metabot.office_run as office_run
    from metabot import web_data

    real_view, real_worktree, real_say = (
        web_data.office_view,
        office_run.create_worktree,
        Broker._say,
    )
    held, asked = threading.Event(), threading.Event()

    def read_then_wait(run_id):
        view = real_view(run_id)
        if not held.is_set() and view is not None and not view["desks"]:
            held.set()  # this page holds a record read before the desk began
            asked.wait(10)  # the desk asks while this page is being made
        return view

    def worktree(*args, **kwargs):
        held.wait(10)  # the desk begins only once a page holds the older record
        return real_worktree(*args, **kwargs)

    def say(broker, run_id, text):
        real_say(broker, run_id, text)
        if "waits for you" in text:  # said once the ask is filed
            asked.set()

    monkeypatch.setattr(web_data, "office_view", read_then_wait)
    monkeypatch.setattr(office_run, "create_worktree", worktree)
    monkeypatch.setattr(Broker, "_say", say)
    root = _repo(tmp_path, {"build": DESKS["build"]})
    with office_ui(root, {"codex": Asks()}) as (client, guard):
        office_id = _start(client, guard, root, "build")
        text = _until(client, office_id, _asked)
    assert held.is_set() and asked.is_set()
    assert "<td>build</td><td>running</td>" in text  # the desk that asks is in the table


def test_an_advisor_that_cannot_run_here_is_named_and_its_box_left_unticked(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "person"))  # where Code Desks looks last
    root = _repo(tmp_path, {**ADVISED, "qa": {**ADVISED["qa"], "kit": ["no-such-skill"]}})
    with office_ui(root, {}) as (client, _guard):
        text = client.get("/office").text
    assert 'name="advisors" value="1">' in text  # a relay with it would refuse to open
    assert "Cannot run here: qa: kit: skill &#x27;no-such-skill&#x27; is not in" in text


def test_a_desk_whose_cwd_leads_out_through_a_link_is_offered_unticked_with_why(tmp_path):
    root = _repo(tmp_path, {**DESKS, "build": {**DESKS["build"], "cwd": "src"}})
    (root / "src").symlink_to(tmp_path)
    with office_ui(root, {}) as (client, _guard):
        text = client.get("/office").text
    assert 'name="desk" value="build">' in text and 'name="desk" value="plan" checked>' in text
    assert "cwd must stay inside the desk&#x27;s worktree" in text


def test_a_desk_whose_command_is_not_on_path_is_offered_unticked_with_why(tmp_path, monkeypatch):
    not_on_path(monkeypatch, "codex", "cursor-agent")
    with office_ui(_repo(tmp_path, ADVISED), {}) as (client, _guard):
        text = client.get("/office").text
    assert 'name="desk" value="plan" checked>' in text
    assert 'name="desk" value="build">' in text and 'name="desk" value="review">' in text
    assert "<td>codex/m-build</td>" in text and "<td>cursor</td>" in text  # as office check says
    assert "codex is not on PATH" in text
    assert 'name="advisors" value="1">' in text  # a relay with it would refuse to open
    assert "Cannot run here: qa: cursor-agent is not on PATH" in text
