"""The web UI's pages: runs, a run, an office run tree, connections and routines, shown as text."""

from __future__ import annotations

import asyncio
import html
import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from metabot import web_data
from metabot.approvals import DenyAll, Scripted
from metabot.connections import ConnectionStore
from metabot.events import Final, Started, ToolCall, ToolResult
from metabot.gate import Policy
from metabot.office import load_office
from metabot.office_run import office_journal_path, run_relay
from metabot.origins import origins_for
from metabot.paths import run_path
from metabot.routines import RoutineSpec, fire, save_routine
from metabot.runner import run_job
from metabot.web_app import make_app
from metabot.web_guard import COOKIE, Guard
from metabot.web_pages import COMMITTED, UNSIGNED
from test_checks import _trusted
from test_office_run import DESKS, TASK, DeskEngine, _build, _repo
from test_serve import Quick

PORT = 47002
TOKEN = "k" * 43
PAYLOADS = ["<script>alert(1)</script>", '"><img src=x onerror=alert(1)>']


def _client(tmp_path: Path) -> TestClient:
    app = make_app(Guard(TOKEN, PORT), start_dir=tmp_path)
    client = TestClient(app, base_url=f"http://127.0.0.1:{PORT}", follow_redirects=False)
    client.cookies.set(COOKIE, TOKEN)
    return client


class Asker:
    """Asks the gate about each call before it runs it, then answers."""

    name = "fake"
    tool_prefix = "fake"
    skip_tools = ()

    def __init__(self, calls=(), answer: str = "done") -> None:
        self.calls = list(calls)  # (call_id, tool, class, args)
        self.answer = answer

    async def run(self, job, *, board=None, approve):
        yield Started("s-ask", job.model)
        for call_id, tool, cls, args in self.calls:
            yield ToolCall(call_id, tool, args)
            msg = {"op": "approve", "call_id": call_id, "tool": f"fake/{tool}", "cls": cls}
            reply = await approve({**msg, "preview": json.dumps(args)})
            yield ToolResult(call_id, "ran" if reply["allow"] else "declined", reply["allow"])
        yield Final(self.answer, "s-ask")


def _run(tmp_path: Path, engine=None, approver=None, prompt: str = "a task"):
    return asyncio.run(
        run_job(
            engine=engine or Asker(),
            prompt=prompt,
            cwd=tmp_path,
            policy=Policy(),
            approver=approver or DenyAll(),
        )
    )


def _row_of(text: str, marker: str) -> str:
    """The table row that holds ``marker``."""
    at = text.index(marker)
    return text[text.rindex("<tr", 0, at) : text.index("</tr>", at)]


def _as_text(response, payload: str) -> None:
    assert response.status_code == 200
    assert payload not in response.text
    assert html.escape(payload, quote=True) in response.text


# ── runs ────────────────────────────────────────────────────────────────────


def test_the_runs_page_lists_each_run_with_its_receipt_state(tmp_path):
    ids = [_run(tmp_path, prompt=f"task {n}").run_id for n in range(3)]
    receipt = Path(json.loads((run_path(ids[1]) / "journal.json").read_text())["receipt"])
    data = json.loads(receipt.read_text(encoding="utf-8"))
    data["signature"] = ("A" if data["signature"][0] != "A" else "B") + data["signature"][1:]
    receipt.write_text(json.dumps(data), encoding="utf-8")
    page = _client(tmp_path).get("/runs")
    assert page.status_code == 200
    where = [page.text.index(run_id) for run_id in ids]
    assert where[2] < where[1] < where[0]  # newest first
    rows = {run_id: _row_of(page.text, run_id) for run_id in ids}
    assert "INVALID" in rows[ids[1]]
    assert "valid" in rows[ids[0]] and "INVALID" not in rows[ids[0]]
    assert f'href="/runs/{ids[0]}"' in page.text


def test_a_run_page_shows_steps_and_the_approvals(tmp_path):
    calls = [
        ("call-ask-1", "Edit", "write", {"path": "a.txt"}),
        ("call-ask-2", "Edit", "write", {"path": "b.txt"}),
    ]
    result = _run(tmp_path, Asker(calls, answer="all done"), Scripted([True, False]))
    page = _client(tmp_path).get(f"/runs/{result.run_id}")
    assert page.status_code == 200
    text = page.text
    assert "fake/Edit" in text and "a.txt" in text and "b.txt" in text
    first, second = _row_of(text, ">call-ask-1<"), _row_of(text, ">call-ask-2<")
    assert "approved" in first and "declined" not in first and ">ok<" in first
    assert "declined" in second and "approved" not in second and ">FAIL<" in second
    assert "all done" in text and "valid" in text


@pytest.mark.parametrize(
    ("policy", "shown"),
    [
        (Policy(allow_write=True), "allowed in advance: every write"),
        (Policy(allows=frozenset({"fake/Edit"})), "allowed in advance: fake/Edit"),
    ],
)
def test_the_run_page_says_which_allow_let_a_write_run(tmp_path, policy, shown):
    calls = [("call-w", "Edit", "write", {"path": "a.txt"}), ("call-r", "Read", "read", {})]
    result = asyncio.run(
        run_job(engine=Asker(calls), prompt="p", cwd=tmp_path, policy=policy, approver=DenyAll())
    )
    text = _client(tmp_path).get(f"/runs/{result.run_id}").text
    assert shown in _row_of(text, ">call-w<")
    assert "allowed" not in _row_of(text, ">call-r<")  # a read needs no allow
    assert '<span class="ok">valid</span>' in text


def test_the_run_page_says_the_answer_may_disagree_with_the_steps(tmp_path):
    calls = [("call-1", "Edit", "write", {"path": "a.txt"})]
    declined = _run(tmp_path, Asker(calls, answer="I wrote a.txt"))  # nobody there to allow it
    client = _client(tmp_path)
    caveat = (
        "1 call was declined. The answer may say otherwise; the receipt's steps are what happened."
    )
    assert html.escape(caveat, quote=True) in client.get(f"/runs/{declined.run_id}").text
    clean = _run(tmp_path, Asker([("call-2", "Read", "read", {})]))
    assert "may say otherwise" not in client.get(f"/runs/{clean.run_id}").text


def test_the_run_page_marks_what_the_receipt_does_not_sign(tmp_path):
    calls = [("call-1", "Edit", "write", {"path": "a.txt"})]
    result = _run(tmp_path, Asker(calls, answer="the answer"), Scripted([False]))
    client = _client(tmp_path)
    text = client.get(f"/runs/{result.run_id}").text
    assert '<span class="ok">valid</span>' in text
    assert html.escape(COMMITTED, quote=True) in text  # it commits to the prompt and answer
    path = run_path(result.run_id) / "journal.json"
    journal = json.loads(path.read_text(encoding="utf-8"))
    [verdict] = [v for v in journal["verdicts"] if v["check"] == "scope_approval:call-1"]
    verdict.update(result="pass", detail="did:orcha:agent:key-0000000000000000")
    path.write_text(json.dumps(journal), encoding="utf-8")
    text = client.get(f"/runs/{result.run_id}").text
    # an approval edited into the journal is never shown beside "valid"
    assert "JOURNAL MISMATCH" in text and '<span class="ok">valid</span>' not in text
    assert html.escape(UNSIGNED, quote=True) in text  # nor is what the receipt commits to


def test_the_runs_page_shows_the_newest_runs_up_to_its_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(web_data, "ROW_LIMIT", 2)
    ids = [_run(tmp_path, prompt=f"task {n}").run_id for n in range(3)]
    page = _client(tmp_path).get("/runs")
    assert ids[0] not in page.text and ids[1] in page.text and ids[2] in page.text
    assert "The newest 2 of 3 runs." in page.text


# ── an office run ───────────────────────────────────────────────────────────

ADVISED = {
    **DESKS,
    "build": {**DESKS["build"], "fan": 2},
    "qa": {"role": "roles/qa.md", "harness": "claude", "advises": "build"},
}


def _office(tmp_path: Path, desks: dict = ADVISED):
    engines = {
        "claude": DeskEngine("claude", act=lambda job: "PLAN or ADVICE"),
        "codex": DeskEngine("codex", act=_build),
        "cursor": DeskEngine("cursor", act=lambda job: "REVIEW: fine"),
    }
    return asyncio.run(
        run_relay(
            load_office(_repo(tmp_path, desks)),
            TASK,
            approver=DenyAll(),
            make_engine=lambda name: engines[name],
        )
    )


def test_an_office_page_shows_the_tree(tmp_path):
    result = _office(tmp_path)
    office_id = result.office_run_id
    page = _client(tmp_path).get(f"/offices/{office_id}")
    assert page.status_code == 200
    text = page.text
    order = [text.index(f">{label}<") for label in ("plan", "build.1", "build.2", "qa.on-build")]
    assert order == sorted(order) and text.index(">review<") > order[-1]
    for label in ("build.1", "build.2", "qa.on-build"):
        assert 'class="indent"' in _row_of(text, f">{label}<"), label
    for label in ("plan", "review"):
        assert 'class="indent"' not in _row_of(text, f">{label}<"), label
    assert text.count("valid") >= 5 and "1 file +1 -0" in text
    for label in result.offer:
        assert f"metabot desks pick {office_id}/{label}" in text
    record = json.loads(office_journal_path(office_id).read_text(encoding="utf-8"))
    assert record["offer"] == result.offer and result.offer


def test_an_office_page_shows_each_desks_checks(tmp_path):
    root = _trusted(tmp_path, {"tests": "test -f feature.txt"})
    engines = {"codex": DeskEngine("codex", act=_build)}
    result = asyncio.run(
        run_relay(
            load_office(root),
            TASK,
            approver=DenyAll(),
            make_engine=lambda name: engines[name],
            checks=True,
        )
    )
    page = _client(tmp_path).get(f"/offices/{result.office_run_id}")
    row = _row_of(page.text, ">build<")
    assert "pass" in row and row.count(">valid<") == 2  # the desk's receipt and its checks'
    assert "ready:tests" in page.text


def test_the_runs_page_links_an_office_run(tmp_path):
    result = _office(tmp_path)
    page = _client(tmp_path).get("/runs")
    assert f'href="/offices/{result.office_run_id}"' in page.text


# ── connections and routines ────────────────────────────────────────────────


def test_the_connections_page_never_shows_a_secret(tmp_path):
    secret = "s3cr3t-token-value-0123456789"
    conn = ConnectionStore().add(
        name="gh", transport="stdio", command=["fake-server", "--x"], token=secret,
        token_env="GH_TOKEN",
    )  # fmt: skip
    page = _client(tmp_path).get("/connections")
    assert page.status_code == 200
    for shown in ("gh", conn.id, "stdio", "active", "fake-server --x", "GH_TOKEN"):
        assert shown in page.text, shown
    assert secret not in page.text


def test_the_connections_page_shows_a_browsers_origins(tmp_path):
    origins = origins_for(["http://x.test"])  # as connections add --browser stores them
    ConnectionStore().add(name="web", transport="stdio", command=["b"], origins=origins)
    ConnectionStore().add(name="plain", transport="stdio", command=["p"])
    page = _client(tmp_path).get("/connections")
    cell = _row_of(page.text, ">web<").rsplit("<td>", 1)[1].removesuffix("</td>")
    assert cell.split(", ") == origins  # the whole list in its order, not a match anywhere
    assert origins[0] == "http://localhost:*" and origins[-1] == "http://x.test:80"
    assert "not a browser" in _row_of(page.text, ">plain<")


def _routine(tmp_path: Path, name: str = "tick") -> str:
    spec = RoutineSpec(
        name=name, goal="Say done.", engine="openai", model=None, cwd=str(tmp_path),
        connections=[], scope_allow=[], criteria={}, cron="0 9 * * *", tz="UTC",
    )  # fmt: skip
    return save_routine(spec, list_tools=lambda conn: [])


def test_the_routines_page_shows_next_due_and_firings(tmp_path):
    rid = _routine(tmp_path)
    firing = asyncio.run(fire(rid, make_engine=lambda name: Quick()))
    page = _client(tmp_path).get("/routines")
    assert page.status_code == 200
    for shown in ("tick", "0 9 * * *", "UTC", "on", "09:00", firing.state):
        assert shown in page.text, shown
    assert f'href="/runs/{firing.run_id}"' in page.text


# ── attacker text ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("payload", PAYLOADS)
def test_a_prompt_and_an_answer_render_as_text(tmp_path, payload):
    result = _run(tmp_path, Asker(answer=payload), prompt=payload)
    client = _client(tmp_path)
    _as_text(client.get("/runs"), payload)
    _as_text(client.get(f"/runs/{result.run_id}"), payload)


@pytest.mark.parametrize("payload", PAYLOADS)
def test_a_tool_name_and_its_arguments_render_as_text(tmp_path, payload):
    asks = Asker([("call-ask-1", payload, "write", {"path": payload})])
    result = _run(tmp_path, asks, Scripted([True]))
    _as_text(_client(tmp_path).get(f"/runs/{result.run_id}"), payload)


@pytest.mark.parametrize("payload", PAYLOADS)
def test_an_office_record_renders_as_text(tmp_path, payload):
    result = _office(tmp_path)
    path = office_journal_path(result.office_run_id)
    record = json.loads(path.read_text(encoding="utf-8"))
    record["office"], record["task"] = payload, payload
    record["desks"][0]["label"] = payload
    record["desks"][0]["error"] = payload
    path.write_text(json.dumps(record), encoding="utf-8")
    _as_text(_client(tmp_path).get(f"/offices/{result.office_run_id}"), payload)


@pytest.mark.parametrize("payload", PAYLOADS)
def test_a_hand_edited_connection_renders_as_text(tmp_path, payload):
    store = ConnectionStore()
    store.add(name="gh", transport="stdio", command=["s"])
    data = json.loads(store.path.read_text(encoding="utf-8"))
    data["connections"][0].update(name=payload, command=[payload], origins=[payload])
    store.path.write_text(json.dumps(data), encoding="utf-8")
    _as_text(_client(tmp_path).get("/connections"), payload)


@pytest.mark.parametrize("payload", PAYLOADS)
def test_a_hand_edited_routine_renders_as_text(tmp_path, home, payload):
    rid = _routine(tmp_path)
    path = home / "routines.json"
    table = json.loads(path.read_text(encoding="utf-8"))
    table[rid].update(name=payload, cron=payload, tz=payload)
    path.write_text(json.dumps(table), encoding="utf-8")
    _as_text(_client(tmp_path).get("/routines"), payload)


# ── what is broken or not there ─────────────────────────────────────────────


def test_a_hand_edited_record_does_not_break_a_page(tmp_path, home):
    result = _run(tmp_path, Asker([("call-ask-1", "Read", "read", {})]))
    journal = run_path(result.run_id) / "journal.json"
    journal.write_text(json.dumps({"run_id": result.run_id, "meta": "x", "steps": {"a": 1}}))
    office = _office(tmp_path / "o")
    path = office_journal_path(office.office_run_id)
    record = json.loads(path.read_text(encoding="utf-8"))
    record["desks"] = {"not": "a list"}
    path.write_text(json.dumps(record), encoding="utf-8")
    (home / "routines.json").write_text("[1, 2]", encoding="utf-8")
    ConnectionStore().path.write_text("{not json", encoding="utf-8")
    client = _client(tmp_path)
    assert client.get(f"/runs/{result.run_id}").status_code == 200
    assert client.get(f"/offices/{office.office_run_id}").status_code == 200
    for route in ("/routines", "/connections"):
        page = client.get(route)
        assert page.status_code == 200 and page.text.count("cannot read") == 1, route


def test_a_receipt_path_a_record_names_is_never_opened(tmp_path, home):
    result = _run(tmp_path)
    decoy = tmp_path / "decoy.json"
    decoy.write_text("not a receipt", encoding="utf-8")
    index = home / "runs.jsonl"
    rows = [json.loads(line) for line in index.read_text(encoding="utf-8").splitlines()]
    rows[-1]["receipt"] = str(decoy)
    index.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    page = _client(tmp_path).get("/runs")
    assert ">valid<" in _row_of(page.text, result.run_id)  # read from where MetaBot keeps it


def test_an_unsafe_or_unknown_id_is_refused(tmp_path):
    client = _client(tmp_path)
    assert client.get("/runs/mb-20990101T000000Z-aaaaaa").status_code == 404
    assert client.get("/offices/mb-20990101T000000Z-aaaaaa").status_code == 404
    assert client.get("/runs/bad id!").status_code == 400
    assert client.get("/runs/..%2F..%2Fetc").status_code in (400, 404)
