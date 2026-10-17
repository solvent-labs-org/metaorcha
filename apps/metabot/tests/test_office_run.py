"""An office relay: desks in order, each in its own worktree, one office run id."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

from conftest import not_on_path
from metabot import office_run
from metabot.approvals import DenyAll
from metabot.cli import main
from metabot.desks import DeskGitError
from metabot.events import ErrorEvent, Final, Notice, Started, ToolCall, ToolResult, Usage
from metabot.gitcmd import GitError
from metabot.office import OfficeError, load_office
from metabot.office_run import office_journal_path, run_fan, run_relay
from metabot.plugins import PluginError
from metabot.receipts import journal_matches, list_index, verify_file

TASK = "Ship the greeting feature"


def _git(repo: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, env=env, check=True
    )
    return proc.stdout


DESKS = {
    "plan": {"role": "roles/plan.md", "harness": "claude"},
    "build": {"role": "roles/build.md", "harness": "codex", "model": "m-build"},
    "review": {"role": "roles/review.md", "harness": "cursor"},
}


def _repo(tmp_path: Path, desks: dict | None = None, office_dir: str = ".") -> Path:
    repo = tmp_path / "repo"
    root = repo / office_dir
    root.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "MetaBot Test")
    _git(repo, "config", "user.email", "metabot-test@example.invalid")
    data = {
        "name": "shop",
        "gates": {"ready": {"owner": ["verdict"], "engineer": ["tests"]}},
        "desks": desks or DESKS,
    }
    (root / "office.yaml").write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    for name, spec in (desks or DESKS).items():
        role = spec.get("role")
        if role:
            (root / role).parent.mkdir(parents=True, exist_ok=True)
            (root / role).write_text(f"You are the {name} desk.\n", encoding="utf-8")
    (repo / "app.txt").write_text("v1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "office")
    return root


class DeskEngine:
    """An in-process engine: acts on its worktree, then answers."""

    tool_prefix = "fake"
    skip_tools = ()

    def __init__(self, name: str, act=None, fail: str | None = None, calls: int = 0) -> None:
        self.name = name
        self.act = act
        self.fail = fail
        self.calls = calls
        self.jobs: list = []

    async def run(self, job, *, board=None, approve):
        self.jobs.append(job)
        yield Started(f"s-{self.name}", job.model)
        for n in range(self.calls):
            yield ToolCall(f"c{n}", "Read", {"path": "."})
            yield ToolResult(f"c{n}", "ok", True)
        text = self.act(job) if self.act else f"{self.name} done"
        if self.fail:
            yield ErrorEvent(self.fail)
        yield Final(text, f"s-{self.name}")


def _build(job) -> str:
    (Path(job.cwd) / "feature.txt").write_text("hello\n", encoding="utf-8")
    return "BUILT feature.txt"


def _build_and_commit(job) -> str:
    """Builds, then commits its work itself, as a model may despite its prompt."""
    answer = _build(job)
    _git(Path(job.cwd), "add", "feature.txt")
    _git(Path(job.cwd), "commit", "-q", "-m", "my own commit")
    return answer


@pytest.fixture
def engines():
    return {
        "claude": DeskEngine("claude", act=lambda job: "PLAN: write feature.txt"),
        "codex": DeskEngine("codex", act=_build),
        "cursor": DeskEngine("cursor", act=lambda job: "REVIEW: fine"),
    }


async def _relay(root: Path, engines: dict, **kw):
    return await run_relay(
        load_office(root),
        kw.pop("task", TASK),
        approver=DenyAll(),
        make_engine=lambda name: engines[name],
        **kw,
    )


async def test_relay_runs_desks_in_order_each_with_the_earlier_answers(tmp_path, home, engines):
    root = _repo(tmp_path)
    result = await _relay(root, engines)
    assert result.ok, [(r.desk, r.error) for r in result.rows]
    office_id = result.office_run_id
    assert [r.desk for r in result.rows] == ["plan", "build", "review"]
    assert [r.run_id for r in result.rows] == [
        f"{office_id}.{d}" for d in ("plan", "build", "review")
    ]

    build_job = engines["codex"].jobs[0]
    build_wt = home / "desks" / office_id / "build"
    assert Path(build_job.cwd) == build_wt.resolve()
    assert build_job.model == "m-build"
    assert build_job.prompt.startswith("You are the build desk.")
    assert f"Task:\n{TASK}" in build_job.prompt
    plan_note = build_wt / ".metabot" / "inbox" / "plan.md"
    assert str(plan_note) in build_job.prompt
    assert plan_note.read_text(encoding="utf-8") == "PLAN: write feature.txt"

    review_job = engines["cursor"].jobs[0]
    review_wt = Path(review_job.cwd)
    assert (review_wt / "feature.txt").read_text(encoding="utf-8") == "hello\n"
    inbox = sorted(p.name for p in (review_wt / ".metabot" / "inbox").iterdir())
    assert inbox == ["build.md", "plan.md"]

    rows = {r["run_id"]: r for r in list_index() if r.get("office") == office_id}
    assert [rows[r.run_id]["parent"] for r in result.rows] == [
        None,
        f"{office_id}.plan",
        f"{office_id}.build",
    ]
    assert all(rows[r.run_id]["desk"] == r.desk for r in result.rows)
    assert result.rows[1].stat == (1, 1, 0)
    assert result.rows[0].commit is None and result.rows[1].commit
    assert (
        _git(root, "log", "-1", "--format=%B", result.rows[1].branch)
        .strip()
        .endswith(f"MetaBot-Run: {office_id}.build")
    )
    for row in result.rows:
        assert verify_file(row.receipt).valid and journal_matches(row.run_id)

    record = json.loads(office_journal_path(office_id).read_text(encoding="utf-8"))
    assert [d["run_id"] for d in record["desks"]] == [r.run_id for r in result.rows]
    assert record["mode"] == "relay" and record["finished_at"]
    assert _git(root, "status", "--porcelain") == ""  # the person's checkout is untouched


async def test_the_record_on_disk_holds_each_desk_as_it_goes(tmp_path, home, engines):
    seen: list[dict] = []

    def review(job):  # a relay killed here keeps what the desks before did
        [path] = (home / "offices").glob("*.json")
        seen.append(json.loads(path.read_text(encoding="utf-8")))
        return "REVIEW: fine"

    engines["cursor"] = DeskEngine("cursor", act=review)
    result = await _relay(_repo(tmp_path), engines)
    [record] = seen
    assert record["finished_at"] is None
    plan, build, now = record["desks"]
    assert [plan["label"], build["label"], now["label"]] == ["plan", "build", "review"]
    assert plan["ok"] and plan["receipt"] and build["ok"] and build["commit"]
    assert now["run_id"] == result.rows[2].run_id and now["receipt"] is None


async def test_a_failed_desk_stops_the_relay(tmp_path, engines):
    engines["codex"].fail = "codex exited 1"
    root = _repo(tmp_path)
    result = await _relay(root, engines)
    assert not result.ok
    assert [r.desk for r in result.rows] == ["plan", "build"]
    assert result.rows[1].error == "codex exited 1"
    assert result.rows[1].commit  # what it did is kept on its branch
    assert engines["cursor"].jobs == []
    record = json.loads(office_journal_path(result.office_run_id).read_text())
    assert [d["desk"] for d in record["desks"]] == ["plan", "build"]


async def test_an_office_run_takes_the_id_it_is_given_and_never_one_in_use(tmp_path, engines):
    root = _repo(tmp_path)
    result = await _relay(root, engines, desks=["plan"], office_run_id="mb-given-1")
    assert result.office_run_id == "mb-given-1"
    assert [r.run_id for r in result.rows] == ["mb-given-1.plan"]
    assert office_journal_path("mb-given-1").is_file()
    with pytest.raises(OfficeError, match="office run mb-given-1 exists already"):
        await _relay(root, engines, desks=["plan"], office_run_id="mb-given-1")
    with pytest.raises(ValueError, match="unsafe office run id"):
        await _relay(root, engines, desks=["plan"], office_run_id="../mb-x")
    assert len(engines["claude"].jobs) == 1  # neither refusal ran a desk


async def test_an_office_run_holds_its_id_from_the_start_so_no_other_run_takes_it(
    tmp_path, engines, monkeypatch
):
    """The id is claimed before the slow part of opening, not checked, then written after it."""
    root = _repo(tmp_path)
    real, nested = office_run._load_kits, []

    def meanwhile(*args, **kwargs):
        if not nested:  # another run asks for the same id while this one opens
            nested.append(None)
            with pytest.raises(OfficeError, match="office run mb-same-1 exists already"):
                office_run._open(
                    *(load_office(root), TASK, "relay"),
                    approver=DenyAll(),
                    desks=["plan"],
                    allow_write=False,
                    allows=(),
                    roster=None,
                    connections=False,
                    plugins=False,
                    on_event=None,
                    on_desk=None,
                    make_engine=None,
                    office_run_id="mb-same-1",
                )
        return real(*args, **kwargs)

    monkeypatch.setattr(office_run, "_load_kits", meanwhile)
    result = await _relay(root, engines, desks=["plan"], office_run_id="mb-same-1")
    assert nested and result.ok and len(engines["claude"].jobs) == 1


async def test_an_office_run_that_cannot_open_leaves_no_record(tmp_path, engines, monkeypatch):
    real, broke = office_run._load_kits, []

    def broken(*args, **kwargs):
        if not broke:
            broke.append(None)
            raise office_run.KitError("the kit is broken")
        return real(*args, **kwargs)

    monkeypatch.setattr(office_run, "_load_kits", broken)
    with pytest.raises(office_run.KitError):
        await _relay(_repo(tmp_path), engines, desks=["plan"], office_run_id="mb-broken-1")
    assert not office_journal_path("mb-broken-1").exists()
    result = await _relay(_repo(tmp_path / "again"), engines, office_run_id="mb-broken-1")
    assert result.office_run_id == "mb-broken-1"  # the id was never taken


TURNS_HINT = (
    "raise turns: for the build desk in office.yaml, or give it a model that needs fewer tool calls"
)


async def test_a_desk_past_its_turn_cap_stops_the_relay_and_says_what_to_change(tmp_path, engines):
    desks = {**DESKS, "build": {**DESKS["build"], "turns": 1}}
    engines["codex"].calls = 3
    root = _repo(tmp_path, desks)
    seen: list = []
    result = await _relay(root, engines, on_event=seen.append)
    assert result.rows[1].error == f"limit: 1 tool calls; {TURNS_HINT}"
    assert len(result.rows) == 2
    record = json.loads(office_journal_path(result.office_run_id).read_text())
    assert record["desks"][1]["error"] == f"limit: 1 tool calls; {TURNS_HINT}"
    assert [e.text for e in seen if isinstance(e, Notice)] == [TURNS_HINT]  # in the trace


class Spender(DeskEngine):
    """Reports more tokens than its desk's budget, then makes a call."""

    async def run(self, job, *, board=None, approve):
        self.jobs.append(job)
        yield Started(f"s-{self.name}", job.model)
        yield Usage({"prompt_tokens": 100, "completion_tokens": 0})
        yield ToolCall("c0", "Read", {"path": "."})
        yield Final("spent", f"s-{self.name}")


async def test_a_desk_past_its_budget_says_what_to_change(tmp_path, engines):
    desks = {**DESKS, "build": {**DESKS["build"], "budget": 10}}
    engines["codex"] = Spender("codex")
    result = await _relay(_repo(tmp_path, desks), engines)
    hint = "raise budget: for the build desk in office.yaml"
    assert result.rows[1].error == f"limit: 10 tokens; {hint}"


async def test_an_advisor_past_its_turn_cap_says_what_to_change(tmp_path, engines):
    qa = {"role": "roles/qa.md", "harness": "claude", "advises": ["build"], "turns": 1}
    engines["claude"].calls = 3  # the plan desk has turns to spare; the qa advisor does not
    result = await _relay(_repo(tmp_path, {**DESKS, "qa": qa}), engines)
    [row] = [r for r in result.rows if r.desk == "qa"]
    hint = TURNS_HINT.replace("the build desk", "the qa desk")
    assert row.error == f"limit: 1 tool calls; {hint}"


async def test_chosen_desks_run_in_the_order_given(tmp_path, engines):
    root = _repo(tmp_path)
    result = await _relay(root, engines, desks=["build", "plan"])
    assert [r.desk for r in result.rows] == ["build", "plan"]
    assert engines["cursor"].jobs == []


@pytest.mark.parametrize("desks", [["ghost"], ["plan", "plan"], []])
async def test_unknown_or_repeated_desks_are_refused(tmp_path, engines, desks):
    root = _repo(tmp_path)
    with pytest.raises(OfficeError):
        await _relay(root, engines, desks=desks)


async def test_a_desk_that_cannot_run_is_refused_before_anything_starts(tmp_path, home, engines):
    desks = {**DESKS, "aider": {"role": "roles/aider.md", "harness": "custom", "command": "aider"}}
    root = _repo(tmp_path, desks)
    with pytest.raises(OfficeError, match="aider"):
        await _relay(root, engines)
    assert not (home / "desks").exists()
    assert list_index() == []
    assert all(engine.jobs == [] for engine in engines.values())


async def test_an_empty_task_is_refused(tmp_path, engines):
    with pytest.raises(OfficeError, match="task"):
        await _relay(_repo(tmp_path), engines, task="  \n")


async def test_an_office_outside_git_is_refused(tmp_path, engines):
    root = tmp_path / "plain"
    root.mkdir()
    (root / "office.yaml").write_text(
        yaml.safe_dump({"name": "x", "gates": {"g": {"owner": ["a"]}}, "desks": DESKS}),
        encoding="utf-8",
    )
    for spec in DESKS.values():
        (root / spec["role"]).parent.mkdir(parents=True, exist_ok=True)
        (root / spec["role"]).write_text("role\n", encoding="utf-8")
    with pytest.raises(OfficeError, match="git"):
        await _relay(root, engines)


async def test_a_desk_works_in_its_cwd_inside_the_worktree(tmp_path, home, engines):
    desks = {"build": {"role": "roles/build.md", "harness": "codex", "cwd": "src"}}
    root = _repo(tmp_path, desks, office_dir="ops")
    (root / "src").mkdir()
    (root / "src" / "keep.txt").write_text("x\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "src")
    result = await _relay(root, engines)
    assert result.ok, result.rows[0].error
    wt = home / "desks" / result.office_run_id / "build"
    assert Path(engines["codex"].jobs[0].cwd) == (wt / "ops" / "src").resolve()


async def test_a_cwd_that_leaves_the_worktree_through_a_symlink_is_refused(tmp_path, engines):
    desks = {"build": {"role": "roles/build.md", "harness": "codex", "cwd": "src"}}
    root = _repo(tmp_path, desks)
    (root / "src").symlink_to(tmp_path)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "a link out")
    result = await _relay(root, engines)
    assert not result.ok
    assert "outside" in result.rows[0].error
    assert engines["codex"].jobs == []


async def test_a_committed_handoff_link_is_never_followed(tmp_path, engines):
    root = _repo(tmp_path)
    (root / ".metabot").symlink_to(tmp_path)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "a link out")
    result = await _relay(root, engines)
    assert [r.ok for r in result.rows] == [True, False]
    assert "handoff" in result.rows[1].error or ".metabot" in result.rows[1].error
    assert not (tmp_path / "inbox").exists()
    assert engines["codex"].jobs == []


def _cli(capsys, *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def test_office_run_and_the_tree_through_the_cli(tmp_path, capsys, monkeypatch, engines):
    root = _repo(tmp_path)
    monkeypatch.setattr(office_run, "get_engine", lambda name: engines[name])
    code, out, err = _cli(capsys, "office", "run", "--dir", str(root), "-q", TASK)
    assert code == 0, err
    office_id = next(w for w in out.split() if w.startswith("mb-"))
    for desk in ("plan", "build", "review"):
        assert any(line.strip().startswith(desk) and "ok" in line for line in out.splitlines())
    assert "REVIEW: fine" in out
    assert f"metabot desks pick {office_id}/review" in out

    code, out, err = _cli(capsys, "receipts", "show", office_id)
    assert code == 0, err
    assert "relay" in out and TASK in out
    lines = [
        line for line in out.splitlines() if line.strip().startswith(("plan", "build", "review"))
    ]
    assert len(lines) == 3 and all("valid" in line and "ok" in line for line in lines)
    for desk in ("plan", "build", "review"):
        assert _cli(capsys, "receipts", "verify", f"{office_id}.{desk}")[0] == 0


def test_an_any_desk_runs_on_the_roster_through_the_cli(tmp_path, capsys, monkeypatch, engines):
    root = _repo(tmp_path, {"pool": {"role": "roles/pool.md", "harness": "any"}})
    monkeypatch.setattr(office_run, "get_engine", lambda name: engines[name])
    here = ({"claude": None, "codex": "/bin/codex", "cursor": None}, False)
    monkeypatch.setattr("metabot.roster.here", lambda cfg: here)
    code, out, err = _cli(capsys, "office", "run", "--dir", str(root), "-q", TASK)
    assert code == 0, err
    assert len(engines["codex"].jobs) == 1  # the default build role: the first engine here
    assert _cli(capsys, "roster", "set", "build", "--engine", "cursor", "--model", "m-mine")[0] == 0
    code, out, err = _cli(capsys, "office", "run", "--dir", str(root), "-q", TASK)
    assert code == 0, err
    assert [job.model for job in engines["cursor"].jobs] == ["m-mine"]  # yours wins
    assert len(engines["codex"].jobs) == 1


def test_office_run_reports_a_refused_desk(tmp_path, capsys, monkeypatch, engines):
    desks = {**DESKS, "aider": {"role": "roles/aider.md", "harness": "custom", "command": "aider"}}
    root = _repo(tmp_path, desks)
    monkeypatch.setattr(office_run, "get_engine", lambda name: engines[name])
    code, out, err = _cli(capsys, "office", "run", "--dir", str(root), TASK)
    assert code == 2 and "aider" in err
    code, out, err = _cli(
        capsys, "office", "run", "--dir", str(root), "--desks", "plan,build", "-q", TASK
    )
    assert code == 0, err


async def test_a_desk_whose_command_is_not_on_path_refuses_the_run_before_any_desk_starts(
    tmp_path, home, monkeypatch, engines
):
    not_on_path(monkeypatch, "codex")
    root = _repo(tmp_path)
    runs = (
        lambda: _relay(root, engines),
        lambda: run_fan(load_office(root), TASK, approver=DenyAll(), make_engine=engines.get),
    )
    for run in runs:
        with pytest.raises(OfficeError) as caught:
            await run()
        assert caught.value.where == "desks"
        assert "build: codex is not on PATH" in str(caught.value)
    assert all(engine.jobs == [] for engine in engines.values())
    assert list_index() == [] and not (home / "desks").exists()
    result = await _relay(root, engines, desks=["plan", "review"])
    assert result.ok and engines["codex"].jobs == []


def test_office_run_names_the_command_that_is_not_on_path(tmp_path, capsys, monkeypatch, engines):
    not_on_path(monkeypatch, "codex")
    root = _repo(tmp_path)
    monkeypatch.setattr(office_run, "get_engine", lambda name: engines[name])
    code, out, err = _cli(capsys, "office", "run", "--dir", str(root), TASK)
    assert code == 2 and "build: codex is not on PATH" in err
    assert all(engine.jobs == [] for engine in engines.values())


@pytest.mark.parametrize(
    "error",
    [
        DeskGitError("not a commit here: refs/heads/x"),
        GitError(("rev-parse",), 128, "fatal: bad object\n"),
        PluginError("cannot read plugins.json"),
        ValueError("run id in use: mb-x"),
    ],
)
def test_office_run_says_what_stopped_it(tmp_path, capsys, monkeypatch, error):
    root = _repo(tmp_path)

    async def stops(*args, **kw):
        raise error

    monkeypatch.setattr(office_run, "run_relay", stops)
    code, out, err = _cli(capsys, "office", "run", "--dir", str(root), TASK)
    assert code == 1 and out == ""
    assert err == f"metabot: the office run stopped: {error}\n"


async def test_every_desk_gets_the_enabled_plugins_and_records_their_pin(tmp_path, engines):
    from metabot.paths import run_dir
    from metabot.plugins import add_marketplace, enable, install
    from plugin_kit import make_market, make_plugin, yes

    market = make_market(tmp_path, "tools")
    make_plugin(market / "plugins" / "tools")
    add_marketplace("m", market)
    plugin = install("tools@m", yes)
    view = enable("tools").view
    root = _repo(tmp_path)
    result = await _relay(root, engines)
    assert result.ok, [(r.desk, r.error) for r in result.rows]
    assert all(e.jobs[0].plugin_dirs == [view] for e in engines.values())
    for row in result.rows:
        meta = json.loads((run_dir(row.run_id) / "journal.json").read_text())["meta"]
        assert meta["plugins"] == [plugin.pin]
    fresh = {name: DeskEngine(name) for name in engines}
    result = await _relay(root, fresh, plugins=False)
    assert all(e.jobs[0].plugin_dirs == [] for e in fresh.values())
