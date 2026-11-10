"""Home (U2): Today's four groups, Plan it and its one approved run, the spend cap, and a kept
workflow's page."""

from __future__ import annotations

import html
import json
import re
from pathlib import Path

import pytest
from emerge.run_attestation import verify_run_attestation

from metabot.live import begin_live, end_live
from metabot.paths import receipts_dir
from metabot.receipts import _append_index, verdicts_of
from metabot.web_app import CAP_MOST_USD, spend_cap
from metabot.web_home import (
    APPROVE_LINE,
    BLOCKED,
    DECLINED,
    NO_PLAN,
    ONE_LINE,
    PLAN_CHANGED,
    RUNNING_UNSIGNED,
    WORKFLOW_LINE,
)
from metabot.web_pages import CAP_HELP, CAP_LABEL
from metabot.web_runs import NOT_PRICED
from test_web_everyday import _until, configure
from test_web_runs import PAYLOADS, Answers, Asks, Sleeps, _asked, _ended, _post, _start, ui

SHA = re.compile(r'name="sha256" value="([0-9a-f]{64})"')
RUNS_ON = re.compile(r'name="runs_on" value="([0-9a-f]{64})"')
NEAR = {"home": {"base_url": "http://127.0.0.1:9/v1", "model": "small"}}
FAR = {"far": {"base_url": "https://api.example.invalid/v1", "model": "big"}}
REVIEW = {"review": {"engine": "openai", "model": "small"}}
BANNED = re.compile(r"\b(first|only|nobody|trustless|web3|auto|autonomous|orcha)\b", re.I)


def _near(home: Path, monkeypatch, **more) -> None:
    """A ladder on one endpoint on this machine, and a review role on openai."""
    settings = {"endpoints": NEAR, "ladder": [{"engine": "openai", "endpoint": "home"}],
                "roster": REVIEW, **more}  # fmt: skip
    configure(home, monkeypatch, **settings)


def _far(home: Path, monkeypatch) -> None:
    """A ladder on one endpoint off this machine, billed by use."""
    configure(home, monkeypatch, endpoints=FAR, ladder=[{"engine": "openai", "endpoint": "far"}])


def _plan(client, guard, task: str, max_cost: str = ""):
    return _post(client, guard, "/plan", task=task, max_cost=max_cost)


def _read(body: str) -> tuple[str, str]:
    """The two hashes a plan's approval form carries: the draft's, and what it runs on."""
    return SHA.search(body).group(1), RUNS_ON.search(body).group(1)


def _approve(client, guard, task: str, read: tuple[str, str] | str, max_cost: str = ""):
    sha, runs_on = (read, "0" * 64) if isinstance(read, str) else read
    fields = {"task": task, "max_cost": max_cost, "sha256": sha, "runs_on": runs_on}
    return _post(client, guard, "/plan/run", **fields)


def _signed(read: tuple[str, str]) -> str:
    return f"sha256={read[0]};runs_on={read[1]};approved_in=web"


def _envelope(run_id: str) -> dict:
    return json.loads((receipts_dir() / f"{run_id}.json").read_text(encoding="utf-8"))


def _plan_verdicts(envelope: dict) -> list[dict]:
    return [v for v in envelope["verdicts"] if v.get("check") == "plan"]


def _failed_group(body: str) -> str:
    return body.split("<h2>Checks that failed</h2>", 1)[1].split("<h2>", 1)[0]


def _nothing_kept(home: Path, config_before: bytes) -> None:
    """A plan run keeps no workflow, writes no workflow approval, and changes no setting."""
    assert not (home / "workflows").exists() or not any((home / "workflows").iterdir())
    names = [p.name for p in receipts_dir().glob("*.json")] if receipts_dir().is_dir() else []
    assert not [n for n in names if n.startswith(("wfa-", "wfr-"))]
    assert (home / "config.json").read_bytes() == config_before


# ── Today ───────────────────────────────────────────────────────────────────


def test_today_opens_home_with_four_groups_their_empty_states_and_the_run_form(
    tmp_path, monkeypatch
):
    configure(tmp_path / "home", monkeypatch)
    with ui(tmp_path) as (client, _guard):
        body = client.get("/").text
    assert "<h1>Today</h1>" in body
    for group in ("Needs you", "Checks that failed", "Running now", "Latest receipt"):
        assert f"<h2>{group}</h2>" in body
    assert "Nothing needs you right now." in body
    assert "No check has failed lately." in body and "Nothing is running." in body
    assert "No receipt yet." in body
    assert "What should MetaBot do?" in body and body.count('class="primary"') == 1
    assert f'{html.escape(CAP_LABEL)} <input name="max_cost"' in body
    assert html.escape(CAP_HELP, quote=True) in body
    assert 'href="/plan"' in body


def test_needs_you_lists_each_waiting_approval_and_opens_its_run(tmp_path, monkeypatch):
    configure(tmp_path / "home", monkeypatch)
    with ui(tmp_path, Asks()) as (client, guard):
        run_id = _start(client, guard, tmp_path)
        _until(client, run_id, _asked)
        body = client.get("/").text
        assert "1 approval is waiting." in body
        assert f'<a href="/live/{run_id}">Run {run_id}</a>' in body
        assert "<b>write</b> call <code>fake/Edit</code>" in body
        _post(client, guard, f"/live/{run_id}/stop")
        _until(client, run_id, _ended)
        assert "Nothing needs you right now." in client.get("/").text


def test_running_now_links_each_run_and_stops_one_started_here(tmp_path, monkeypatch):
    configure(tmp_path / "home", monkeypatch)
    begin_live("run", "elsewhere-1", engine="codex")  # a run another process says it runs
    try:
        with ui(tmp_path, Sleeps()) as (client, guard):
            run_id = _start(client, guard, tmp_path)
            _until(client, run_id, lambda t: "[metabot] started" in t)
            body = client.get("/").text
            assert "2 runs in progress." in body
            assert f'<a href="/live/{run_id}">{run_id}</a>' in body
            stop = f'<form method="post" action="/live/{run_id}/stop">'
            assert stop in body
            assert f'<button aria-label="Stop run {run_id}">Stop</button>' in body
            assert "<tr><th>state</th><th>run</th><th>what it runs</th><th>stop</th></tr>" in body
            form = body[body.index(stop) :].split("</form>", 1)[0]
            assert f'name="csrf" value="{guard.csrf}"' in form
            assert "<code>elsewhere-1</code>" in body and "started outside this page" in body
            assert 'action="/live/elsewhere-1/stop"' not in body  # no button that cannot work
            assert html.escape(RUNNING_UNSIGNED, quote=True) in body
            stopped = _post(client, guard, f"/live/{run_id}/stop")
            assert stopped.status_code == 303
            assert "<td>stopped</td>" in _until(client, run_id, _ended)
    finally:
        end_live("elsewhere-1")


def test_checks_that_failed_names_the_run_whose_check_failed(tmp_path, monkeypatch):
    from fake_chat import FakeChat, answer

    server = FakeChat([answer("all done")])
    try:
        endpoints = {"home": {"base_url": server.url, "model": "small"}}
        configure(tmp_path / "home", monkeypatch, endpoints=endpoints,
                  ladder=[{"engine": "openai", "endpoint": "home"}])  # fmt: skip
        with ui(tmp_path) as (client, guard):
            page = _post(
                client, guard, "/", prompt="tidy", folder=str(tmp_path), check="test -f no.txt"
            )
            run_id = page.headers["location"].removeprefix("/live/")
            _until(client, run_id, lambda t: "Spent $" in t)
            body = client.get("/").text
            assert f'A check failed on <a href="/runs/{run_id}.1">{run_id}.1</a>.' in body
            assert '<span class="bad">Failed</span>' in body  # the colour comes with its word
            # a genuine failure, copied under another run's name, names no run
            real = receipts_dir() / f"{run_id}.1.checks.json"
            (receipts_dir() / "mb-copied.checks.json").write_bytes(real.read_bytes())
            _append_index({"run_id": "mb-copied.checks"})
            group = _failed_group(client.get("/").text)
            assert f"{run_id}.1</a>." in group and "mb-copied" not in group
            # a failure changed after it was signed names no run either
            envelope = json.loads(real.read_text(encoding="utf-8"))
            envelope["finished_at"] = "2001-01-01T00:00:00Z"
            real.write_text(json.dumps(envelope), encoding="utf-8")
            group = _failed_group(client.get("/").text)
        assert "A check failed on" not in group and "No check has failed lately." in group
    finally:
        server.close()


def test_running_now_lists_runs_a_person_may_stop_and_never_their_rungs_or_desks(
    tmp_path, monkeypatch
):
    configure(tmp_path / "home", monkeypatch)
    begin_live("ladder", "lad-1")
    begin_live("run", "rung-1", parent="lad-1", name="rung 1")  # one of the ladder's rungs
    begin_live("run", "desk-1", parent="off-1", name="build")  # one of an office's desks
    begin_live("serve", "odd-1")  # metabot serve: not a run a person stops here
    try:
        with ui(tmp_path) as (client, _guard):
            body = client.get("/").text
    finally:
        for live_id in ("lad-1", "rung-1", "desk-1", "odd-1"):
            end_live(live_id)
    assert "1 run in progress." in body and "<code>lad-1</code>" in body
    for hidden in ("rung-1", "desk-1", "odd-1"):
        assert hidden not in body


def test_latest_receipt_is_checked_as_the_page_is_drawn(tmp_path, monkeypatch):
    configure(tmp_path / "home", monkeypatch)
    with ui(tmp_path, Answers()) as (client, guard):
        run_id = _start(client, guard, tmp_path)
        _until(client, run_id, _ended)
        body = client.get("/").text
        assert f'Receipt <a href="/runs/{run_id}">{run_id}</a>: <span class="ok">valid</span>.' in (
            body
        )
        path = receipts_dir() / f"{run_id}.json"
        envelope = json.loads(path.read_text(encoding="utf-8"))
        envelope["finished_at"] = "2001-01-01T00:00:00Z"
        path.write_text(json.dumps(envelope), encoding="utf-8")
        body = client.get("/").text
    assert '<span class="bad">INVALID</span>. It was changed after the run.' in body


def test_today_pages_are_never_kept_by_the_browser(tmp_path, monkeypatch):
    configure(tmp_path / "home", monkeypatch)
    with ui(tmp_path) as (client, guard):
        for response in (
            client.get("/"),
            client.get("/plan"),
            client.get("/workflows/" + "0" * 64),
            _plan(client, guard, "Review my patch"),
            _approve(client, guard, "Review my patch", "0" * 64),
        ):
            assert response.headers["cache-control"] == "no-store", response.request.url


# ── Plan it ─────────────────────────────────────────────────────────────────


def test_plan_it_asks_for_one_sentence_and_a_cap(tmp_path, monkeypatch):
    configure(tmp_path / "home", monkeypatch)
    with ui(tmp_path) as (client, guard):
        body = client.get("/plan").text
    assert "<h1>What should get done?</h1>" in body  # the copy's headline
    assert '<p class="kicker">Plan it</p>' in body
    assert '<label for="plan-task">The work</label>' in body
    assert 'placeholder="Describe the work in one sentence."' in body
    assert '<button class="primary">Plan it</button>' in body
    assert f'name="csrf" value="{guard.csrf}"' in body and 'name="max_cost"' in body
    assert "<script" not in body


def test_a_plan_shows_each_step_its_rules_and_an_approval_and_keeps_nothing(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _near(home, monkeypatch)
    before = (home / "config.json").read_bytes()
    with ui(tmp_path) as (client, guard):
        page = _plan(client, guard, "Review my patch")
    assert page.status_code == 200, page.text
    body = page.text
    assert "Here is the plan." in body
    assert (
        "Each step shows its engine, what it may change, and whether it asks before it writes."
        in body
    )
    assert "ask: openai/small, role review, no login" in body
    assert "<td>Read only</td>" in body
    assert "review: the task says &quot;Review&quot;" in body  # the rule that fired
    assert '<button class="primary">Approve and run</button>' in body
    assert body.count('class="primary"') == 1
    assert '<a class="button" href="/plan?cancelled=1">Cancel</a>' in body
    assert html.escape(APPROVE_LINE) in body
    assert SHA.search(body) is not None
    _nothing_kept(home, before)


def test_cancel_says_nothing_ran(tmp_path, monkeypatch):
    configure(tmp_path / "home", monkeypatch)
    with ui(tmp_path) as (client, _guard):
        assert DECLINED in client.get("/plan?cancelled=1").text


@pytest.mark.parametrize(
    ("task", "said"),
    [("   ", NO_PLAN), ("fix it\nand rm -rf it", ONE_LINE), ("fix‮it", ONE_LINE)],
)
def test_a_sentence_that_makes_no_plan_is_refused(tmp_path, monkeypatch, task, said):
    configure(tmp_path / "home", monkeypatch)
    with ui(tmp_path) as (client, guard):
        page = _plan(client, guard, task)
    assert page.status_code == 400 and html.escape(said) in page.text


def test_approve_and_run_signs_the_plan_into_the_ask_s_receipt(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _near(home, monkeypatch)
    before = (home / "config.json").read_bytes()
    with ui(tmp_path, Answers()) as (client, guard):
        read = _read(_plan(client, guard, "Review my patch").text)
        started = _approve(client, guard, "Review my patch", read)
        assert started.status_code == 303, started.text
        run_id = started.headers["location"].removeprefix("/live/")
        _until(client, run_id, _ended)
    envelope = _envelope(run_id)
    assert verify_run_attestation(envelope).valid  # the verdicts are inside the signed part
    assert _plan_verdicts(envelope) == [
        {"check": "plan", "result": "pass", "detail": _signed(read)}
    ]
    envelope["verdicts"] = [
        {**v, "detail": v["detail"].replace("web", "cli")} if v.get("check") == "plan" else v
        for v in envelope["verdicts"]
    ]
    assert not verify_run_attestation(envelope).valid  # a changed plan verdict reads INVALID
    _nothing_kept(home, before)


def test_approve_and_run_signs_the_plan_into_a_ladder_s_seal(tmp_path, monkeypatch):
    from fake_chat import FakeChat, answer

    server = FakeChat([answer("tidied")])
    try:
        home = tmp_path / "home"
        endpoints = {"home": {"base_url": server.url, "model": "small"}}
        configure(home, monkeypatch, endpoints=endpoints, ladder=[{"engine": "openai",
                  "endpoint": "home"}])  # fmt: skip
        with ui(tmp_path) as (client, guard):
            body = _plan(client, guard, "tidy the notes", "1.50").text
            assert "ladder: rung 1" in body and "<td>Asks before it writes</td>" in body
            assert "<td>$1.50</td>" in body
            read = _read(body)
            started = _approve(client, guard, "tidy the notes", read, "1.50")
            run_id = started.headers["location"].removeprefix("/live/")
            _until(client, run_id, lambda t: "tidied" in t and "the run" in t)
        seal = _envelope(run_id)
        assert verify_run_attestation(seal).valid
        assert [v["detail"] for v in _plan_verdicts(seal)] == [_signed(read)]
        assert any(v["check"] == "budget" for v in seal["verdicts"])  # the cap, signed too
    finally:
        server.close()


def test_a_plan_that_changed_since_it_was_read_never_runs(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _near(home, monkeypatch)
    with ui(tmp_path, Answers()) as (client, guard):
        read = _read(_plan(client, guard, "Review my patch").text)
        _near(home, monkeypatch, roster={"review": {"engine": "openai", "model": "other"}})
        page = _approve(client, guard, "Review my patch", read)
        assert page.status_code == 400 and html.escape(PLAN_CHANGED) in page.text
        assert SHA.search(page.text).group(1) != read[0]  # the plan as it is now, to read again
        now = _read(page.text)
        assert _approve(client, guard, "Review my patch", (now[0], "f" * 64)).status_code == 400
        assert _approve(client, guard, "Review my patch", ("f" * 64, now[1])).status_code == 400
        assert _approve(client, guard, "Review my patch", ("F" * 64, now[1])).status_code == 400
        assert _approve(client, guard, "Review my patch", (now[0], "F" * 64)).status_code == 400
        assert _approve(client, guard, "Review my", now).status_code == 400
    assert not receipts_dir().is_dir() or not list(receipts_dir().glob("*.json"))  # none ran


def test_a_plan_whose_ladder_changed_since_it_was_read_never_runs(tmp_path, monkeypatch):
    """The draft names no rung: what the page showed it runs on is part of what is approved."""
    home = tmp_path / "home"
    other = {"other": {"base_url": "http://127.0.0.1:8/v1", "model": "big"}}
    configure(home, monkeypatch, endpoints={**NEAR, **other},
              ladder=[{"engine": "openai", "endpoint": "home"}])  # fmt: skip
    with ui(tmp_path, Answers()) as (client, guard):
        shown = _plan(client, guard, "tidy the notes").text
        assert "ladder: rung 1 local openai@home/small" in shown
        read = _read(shown)
        configure(home, monkeypatch, endpoints={**NEAR, **other},
                  ladder=[{"engine": "openai", "endpoint": "other"}])  # fmt: skip
        page = _approve(client, guard, "tidy the notes", read)
        assert page.status_code == 400 and html.escape(PLAN_CHANGED) in page.text
        assert "ladder: rung 1 local openai@other/big" in page.text  # as it stands now
        assert _read(page.text)[0] == read[0]  # the draft alone is the same
        assert _read(page.text)[1] != read[1]
    assert not receipts_dir().is_dir() or not list(receipts_dir().glob("*.json"))  # none ran


def test_a_plan_that_cannot_run_shows_why_and_offers_no_approval(tmp_path, monkeypatch):
    _far(tmp_path / "home", monkeypatch)
    with ui(tmp_path) as (client, guard):
        page = _plan(client, guard, "keep this private: my diary")
        assert page.status_code == 200
        assert "no rung here keeps the work on this machine" in page.text
        assert 'action="/plan/run"' not in page.text and "Approve and run" not in page.text
        assert html.escape(BLOCKED) in page.text
        sha = re.search(r"Plan sha256 <code>([0-9a-f]{64})</code>", page.text).group(1)
        runs_on = re.search(r"what it runs on, sha256 <code>([0-9a-f]{64})</code>", page.text)
        read = (sha, runs_on.group(1))
        refused = _approve(client, guard, "keep this private: my diary", read)
    assert refused.status_code == 400 and html.escape(BLOCKED) in refused.text


def test_a_plan_that_needs_a_cap_runs_only_with_one(tmp_path, monkeypatch):
    _far(tmp_path / "home", monkeypatch)
    with ui(tmp_path) as (client, guard):
        bare = _plan(client, guard, "tidy the notes").text
        assert "max_cost in step ladder" in bare and "Approve and run" not in bare
        # the page says where on it the cap goes, never a field of a file it cannot edit
        assert f"give a spend cap in &quot;{html.escape(CAP_LABEL)}&quot; below" in bare
        assert "in the step to the most" not in bare
        capped = _plan(client, guard, "tidy the notes", "2").text
    assert "max_cost in step ladder" not in capped and "Approve and run" in capped
    assert "<td>$2.00</td>" in capped and "max_cost is the spend cap you gave: $2.00" in capped
    assert "max_cost is left empty" not in capped
    assert 'name="max_cost" value="2"' in capped  # the approval sends the cap it showed


def test_an_ask_that_cannot_be_priced_under_a_cap_never_starts_and_says_so(tmp_path, monkeypatch):
    configure(
        tmp_path / "home", monkeypatch, roster=REVIEW,
        openai_base_url="https://api.example.invalid/v1", openai_model="small",
    )  # fmt: skip
    with ui(tmp_path, Answers()) as (client, guard):
        read = _read(_plan(client, guard, "Review my patch", "1").text)
        run_id = (
            _approve(client, guard, "Review my patch", read, "1")
            .headers["location"]
            .removeprefix("/live/")
        )
        text = _until(client, run_id, _ended)
    assert f"{html.escape(NOT_PRICED, quote=True)} (no price for fake/small" in text
    assert not (receipts_dir() / f"{run_id}.json").exists()


@pytest.mark.parametrize("path", ["/plan", "/plan/run"])
def test_plan_posts_need_the_form_token_and_this_origin(tmp_path, monkeypatch, path):
    _near(tmp_path / "home", monkeypatch)
    with ui(tmp_path, Answers()) as (client, guard):
        fields = {"task": "Review my patch", "max_cost": "", "sha256": "0" * 64}
        assert _post(client, guard, path, csrf="x", **fields).status_code == 403
        bare = client.post(path, data={"csrf": guard.csrf, **fields})  # no Origin
        assert bare.status_code == 403


# ── the cap ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "said",
    ["abc", "-1", "0", "0.0000001", "1e3", "Infinity", "NaN", "1,5", "١", "+2",
     str(CAP_MOST_USD + 1), "9" * 30, "2.1234567"],
)  # fmt: skip
def test_a_cap_that_is_not_dollars_above_zero_is_refused(said):
    with pytest.raises(ValueError, match="spend cap"):
        spend_cap(said)


def test_a_cap_is_read_as_max_cost_reads_it():
    assert spend_cap("") is None and spend_cap("  ") is None
    assert spend_cap("2") == 2_000_000 and spend_cap("0.50") == 500_000
    assert spend_cap(".5") == 500_000 and spend_cap(str(CAP_MOST_USD)) == CAP_MOST_USD * 10**6


@pytest.mark.parametrize("path", ["/", "/plan"])
def test_a_bad_cap_is_refused_on_the_page_before_anything_runs(tmp_path, monkeypatch, path):
    _near(tmp_path / "home", monkeypatch)
    fields = {"prompt": "tidy", "folder": str(tmp_path)} if path == "/" else {"task": "tidy"}
    with ui(tmp_path) as (client, guard):
        page = _post(client, guard, path, max_cost="-3", **fields)
    assert page.status_code == 400 and "the spend cap is dollars above 0" in page.text
    assert 'value="-3"' in page.text  # shown back, so it can be put right


def test_a_cap_on_the_home_form_caps_the_ladder(tmp_path, monkeypatch):
    from fake_chat import FakeChat, answer

    server = FakeChat([answer("all done")])
    try:
        endpoints = {"home": {"base_url": server.url, "model": "small"}}
        configure(tmp_path / "home", monkeypatch, endpoints=endpoints,
                  ladder=[{"engine": "openai", "endpoint": "home"}])  # fmt: skip
        with ui(tmp_path) as (client, guard):
            page = _post(client, guard, "/", prompt="tidy", folder=str(tmp_path), max_cost="3")
            run_id = page.headers["location"].removeprefix("/live/")
            _until(client, run_id, lambda t: "all done" in t and "the run" in t)
        [budget] = [v for v in verdicts_of(receipts_dir() / f"{run_id}.json")
                    if v["check"] == "budget"]  # fmt: skip
        assert "3000000" in budget["detail"] or "$3.00" in budget["detail"]
    finally:
        server.close()


# ── a kept workflow ─────────────────────────────────────────────────────────


def _kept(tmp_path: Path, prompt: str = "Say hi.") -> object:
    from metabot.workflows import FORMAT, store_workflow

    data = {
        "format": FORMAT,
        "name": "nightly",
        "cwd": str(tmp_path),
        "steps": [{"name": "say", "kind": "ask", "prompt": prompt, "engine": "openai"}],
    }
    return store_workflow(json.dumps(data).encode())


def test_a_kept_workflow_has_a_read_only_page_by_its_hash(tmp_path, monkeypatch):
    from metabot.workflow_approval import approval_state, approve, revoke

    configure(tmp_path / "home", monkeypatch)
    wf = _kept(tmp_path, prompt=PAYLOADS[0])
    with ui(tmp_path) as (client, _guard):
        body = client.get(f"/workflows/{wf.sha256}").text
        assert "<h1>Workflow</h1>" in body and html.escape(WORKFLOW_LINE) in body
        assert f'<input id="wf-hash" readonly size="66" value="{wf.sha256}">' in body
        assert '<label for="wf-hash">Copy hash</label>' in body
        assert "<code>nightly</code>" in body
        assert "step 1    say (ask): openai" in body  # as workflow show says it
        assert "<script>" not in body and html.escape(PAYLOADS[0]) in body  # the file, as text
        assert '<span class="none">None</span>' in body
        approve(wf)
        assert '<span class="ok">In force</span>' in client.get(f"/workflows/{wf.sha256}").text
        revoke(wf.sha256, approval_state(wf.sha256).in_force)
        assert '<span class="bad">Revoked</span>' in client.get(f"/workflows/{wf.sha256}").text
        assert 'method="post"' not in body.split("</header>", 1)[1]  # read only


def test_an_approval_another_key_signed_is_not_in_force_on_the_page(tmp_path, monkeypatch):
    from metabot.workflow_approval import approve

    configure(tmp_path / "home", monkeypatch)
    wf = _kept(tmp_path)
    monkeypatch.setenv("ORCHA_KEY_PATH", str(tmp_path / "another-key"))
    approve(wf)  # signed by another key, kept in this home
    monkeypatch.setenv("ORCHA_KEY_PATH", str(tmp_path / "orcha-key"))
    with ui(tmp_path) as (client, _guard):
        body = client.get(f"/workflows/{wf.sha256}").text
    assert '<span class="none">None</span>' in body and "In force" not in body


@pytest.mark.parametrize(
    ("ref", "status"),
    [("A" * 64, 400), ("0" * 63, 400), ("0" * 65, 400), ("nightly", 400), ("0" * 64, 404)],
)
def test_a_workflow_address_is_its_whole_hash(tmp_path, monkeypatch, ref, status):
    configure(tmp_path / "home", monkeypatch)
    _kept(tmp_path)
    with ui(tmp_path) as (client, _guard):
        assert client.get(f"/workflows/{ref}").status_code == status


def test_a_kept_workflow_s_page_shows_its_whole_file(tmp_path, monkeypatch):
    configure(tmp_path / "home", monkeypatch)
    wf = _kept(tmp_path, prompt="x" * 21_000 + " TAIL-INSTRUCTION")
    with ui(tmp_path) as (client, _guard):
        body = client.get(f"/workflows/{wf.sha256}").text
    assert "x" * 21_000 + " TAIL-INSTRUCTION" in body


def test_a_kept_workflow_changed_on_disk_says_so(tmp_path, monkeypatch):
    configure(tmp_path / "home", monkeypatch)
    wf = _kept(tmp_path)
    wf.path.write_bytes(wf.raw.replace(b"Say hi.", b"Say no."))
    with ui(tmp_path) as (client, _guard):
        page = client.get(f"/workflows/{wf.sha256}")
    assert page.status_code == 200 and "has changed since it was stored" in page.text


# ── the words ───────────────────────────────────────────────────────────────


def test_home_words_keep_the_copy_rules(tmp_path, monkeypatch):
    import metabot.web_home as words
    from metabot import web_pages

    said = [v for k, v in vars(words).items() if k.isupper() and isinstance(v, str)]
    said += [CAP_LABEL, CAP_HELP, NOT_PRICED, web_pages.CAP_REACHED, web_pages.PLAN_FIRST]
    for text in said:
        assert not BANNED.search(text), text


def test_approve_and_run_signs_the_plan_into_an_office_run_s_seal(tmp_path, monkeypatch):
    from test_office_run import TASK, _repo
    from test_web_office import _engines, office_ui

    monkeypatch.delenv("DESK_OFFICE", raising=False)
    root = _repo(tmp_path)
    with office_ui(root, _engines()) as (client, guard):
        body = _plan(client, guard, TASK).text
        assert "as an office" in body and "office: desk build on codex/m-build" in body
        read = _read(body)
        started = _approve(client, guard, TASK, read)
        assert started.status_code == 303, started.text
        office_id = started.headers["location"].removeprefix("/live/")
        _until(client, office_id, _ended)
    seal = _envelope(office_id)
    assert verify_run_attestation(seal).valid
    assert [v["detail"] for v in _plan_verdicts(seal)] == [_signed(read)]


def test_a_run_stopped_at_its_spend_cap_says_so_on_its_page():
    from types import SimpleNamespace

    from metabot.web_pages import CAP_REACHED, live_page
    from metabot.web_runs import LiveRun

    run = LiveRun("mb-1", "openai", "small", "/tmp", "do it", state="done")
    run.result = SimpleNamespace(
        limit="limit: $1.00", final_text="", steps=[], declined=0, failed=0
    )
    assert CAP_REACHED in live_page(run, [], None, "n", "c")
    run.result.limit = "limit: 40 tool calls"
    assert CAP_REACHED not in live_page(run, [], None, "n", "c")


def test_an_office_run_stopped_at_its_spend_cap_says_so_on_its_page():
    from metabot.office_run import OfficeResult
    from metabot.web_pages import CAP_REACHED, live_page
    from metabot.web_runs import LiveRun

    run = LiveRun("o-1", "", None, "/tmp", "do it", office="shop", mode="relay", state="done")
    run.result = OfficeResult("o-1", "relay", limit="limit: $1.00")
    assert CAP_REACHED in live_page(run, [], None, "n", "c")
    run.result = OfficeResult("o-1", "relay")
    assert CAP_REACHED not in live_page(run, [], None, "n", "c")
