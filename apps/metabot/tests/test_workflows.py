"""The workflow file (``metabot.workflow/1``): what it may say, and the store that keeps it by
the sha256 of its bytes."""

from __future__ import annotations

import hashlib
import json
import os

import pytest

from metabot.config import Config, save_config
from metabot.workflows import (
    FORMAT,
    MAX_BYTES,
    MAX_STEPS,
    WorkflowError,
    here_problems,
    load_workflow,
    parse_workflow,
    resolve,
    store_workflow,
    stored_workflows,
    workflow_bytes,
)


def _doc(tmp_path, **top) -> dict:
    data = {
        "format": FORMAT,
        "name": "date-fix",
        "cwd": str(tmp_path),
        "steps": [{"name": "build", "kind": "ask", "prompt": "Fix the weekday bug."}],
    }
    data.update(top)
    return data


def _raw(data: dict) -> bytes:
    return json.dumps(data).encode("utf-8")


def _step(tmp_path, **step) -> dict:
    return _doc(tmp_path, steps=[{"name": "s", "kind": "ask", "prompt": "Go.", **step}])


# ── the format ──────────────────────────────────────────────────────────────


def test_a_workflow_is_addressed_by_the_sha256_of_its_exact_bytes(tmp_path):
    raw = _raw(_doc(tmp_path))
    wf = parse_workflow(raw)
    assert wf.sha256 == hashlib.sha256(raw).hexdigest()
    assert wf.raw == raw and wf.name == "date-fix" and wf.cwd == str(tmp_path)
    [step] = wf.steps
    assert (step.name, step.kind, step.prompt) == ("build", "ask", "Fix the weekday bug.")
    assert not step.allow_write and not step.read_only and step.connections and step.plugins
    spaced = json.dumps(_doc(tmp_path), indent=2).encode()  # the same JSON, other bytes
    assert parse_workflow(spaced).sha256 != wf.sha256


def test_the_bytes_new_writes_read_back_as_the_same_workflow(tmp_path):
    data = _doc(tmp_path, about="Fixes dates.")
    raw = workflow_bytes(data)
    assert raw.endswith(b"\n") and json.loads(raw) == data
    assert parse_workflow(raw).about == "Fixes dates."


def test_each_kind_of_step_reads_with_its_own_fields(tmp_path):
    office = tmp_path / "office"
    data = _doc(
        tmp_path,
        steps=[
            {"name": "a", "kind": "ask", "prompt": "Go.", "engine": "codex", "model": "m-1",
             "login": "work", "allow": ["Edit"], "max_cost": "1.50"},
            {"name": "b", "kind": "ask", "prompt": "Go.", "role": "review", "read_only": True},
            {"name": "c", "kind": "ladder", "prompt": "Go.", "checks": ["pytest -q"],
             "role": "private", "allow_write": True},
            {"name": "d", "kind": "office", "prompt": "Go.", "dir": str(office),
             "office_sha256": "a" * 64, "desks": ["build"], "checks": True,
             "until_checks": True, "rounds": 4, "advisors": False, "max_cost": 2},
        ],
    )  # fmt: skip
    a, b, c, d = parse_workflow(_raw(data)).steps
    assert (a.engine, a.model, a.login, a.allow, a.max_cost_musd) == (
        "codex", "m-1", "work", ("Edit",), 1_500_000,
    )  # fmt: skip
    assert (b.role, b.read_only) == ("review", True)
    assert (c.ladder_checks, c.role, c.allow_write) == (("pytest -q",), "private", True)
    assert (d.dir, d.office_sha256, d.desks, d.office_checks) == (
        str(office), "a" * 64, ("build",), True,
    )  # fmt: skip
    assert (d.until_checks, d.rounds, d.advisors, d.max_cost_musd) == (True, 4, False, 2_000_000)


def test_a_schedule_and_its_ceiling_are_read_and_kept_for_later(tmp_path):
    data = _doc(
        tmp_path,
        schedule={"cron": "0 9 * * 1-5", "tz": "Europe/London"},
        ceiling={"firings": 40, "expires": "2026-12-31", "month_cap": "20"},
    )
    wf = parse_workflow(_raw(data))
    assert (wf.schedule.cron, wf.schedule.tz) == ("0 9 * * 1-5", "Europe/London")
    assert (wf.ceiling.firings, wf.ceiling.expires, wf.ceiling.month_cap_musd) == (
        40, "2026-12-31", 20_000_000,
    )  # fmt: skip


REFUSED = [
    (lambda t: b"not json", "file", "not JSON"),
    (lambda t: b"\xff\xfe{}", "file", "not UTF-8"),
    (lambda t: b"[1, 2]", "file", "a JSON object"),
    (lambda t: _raw({**_doc(t), "format": "metabot.workflow/2"}), "format", FORMAT),
    (lambda t: _raw({k: v for k, v in _doc(t).items() if k != "format"}), "format", FORMAT),
    (lambda t: _raw({**_doc(t), "owner": "me"}), "owner", "not a field"),
    (lambda t: b'{"format": "metabot.workflow/1", "name": "a", "name": "b"}', "name", "twice"),
    (lambda t: _raw(_doc(t)).replace(b'"steps"', b'"x": NaN, "steps"'), "file", "NaN"),
    (lambda t: _raw(_doc(t, name="-bad")), "name", "1-64 of"),
    (lambda t: _raw(_doc(t, name="deadbeefcafe")), "name", "sha256"),
    (lambda t: _raw(_doc(t, cwd="rel/path")), "cwd", "absolute"),
    (lambda t: _raw(_doc(t, about=7)), "about", "text"),
    (lambda t: _raw(_doc(t, steps=[])), "steps", "1 to"),
    (lambda t: _raw(_doc(t, steps=[_doc(t)["steps"][0]] * (MAX_STEPS + 1))), "steps", "1 to"),
    (lambda t: _raw(_step(t, kind="a2a")), "steps[0].kind", "ask, ladder or office"),
    (lambda t: _raw(_step(t, kind="ladder", login="work")), "steps[0].login", "ladder step"),
    (lambda t: _raw(_step(t, publisher="x")), "steps[0].publisher", "ask step"),
    (lambda t: _raw(_step(t, prompt="  ")), "steps[0].prompt", "empty"),
    (lambda t: _raw(_step(t, name="a b")), "steps[0].name", "1-64 of"),
    (lambda t: _raw(_step(t, allow="Edit")), "steps[0].allow", "list"),
    (lambda t: _raw(_step(t, allow_write=1)), "steps[0].allow_write", "true or false"),
    (lambda t: _raw(_step(t, read_only=True, allow_write=True)), "steps[0]", "read_only"),
    (lambda t: _raw(_step(t, engine="no such!")), "steps[0].engine", "engine name"),
    (lambda t: _raw(_step(t, model="-x")), "steps[0].model", "model name"),
    (lambda t: _raw(_step(t, login="Work Two")), "steps[0].login", "login name"),
    (lambda t: _raw(_step(t, max_cost="-1")), "steps[0].max_cost", "dollars"),
    (lambda t: _raw(_step(t, max_cost=True)), "steps[0].max_cost", "dollars"),
    (lambda t: _raw(_step(t, kind="ladder", role="build")), "steps[0].role", "private"),
    (lambda t: _raw(_step(t, kind="ladder", checks="pytest")), "steps[0].checks", "list"),
    (lambda t: _raw(_step(t, kind="office", office_sha256="a" * 64)), "steps[0].dir", "absolute"),
    (lambda t: _raw(_step(t, kind="office", dir=str(t))), "steps[0].office_sha256", "64 hex"),
    (
        lambda t: _raw(_step(t, kind="office", dir=str(t), office_sha256="a" * 64, rounds=3)),
        "steps[0].rounds",
        "until_checks",
    ),
    (
        lambda t: _raw(
            _step(
                t, kind="office", dir=str(t), office_sha256="a" * 64, until_checks=True, rounds=11
            )
        ),
        "steps[0].rounds",
        "1 to 10",
    ),  # fmt: skip
    (lambda t: _raw(_doc(t, schedule={"cron": "0 9 * * *", "tz": "UTC"})), "ceiling", "needs"),
    (lambda t: _raw(_doc(t, ceiling={})), "ceiling", "at least one"),
    (lambda t: _raw(_doc(t, ceiling={"firings": 0})), "ceiling.firings", "whole number"),
    (lambda t: _raw(_doc(t, ceiling={"expires": "2026-13-01"})), "ceiling.expires", "date"),
    (lambda t: _raw(_doc(t, ceiling={"month_cap": "0"})), "ceiling.month_cap", "dollars"),
    (lambda t: _raw(_doc(t, ceiling={"dollars": 5})), "ceiling.dollars", "not a field"),
    (
        lambda t: _raw(
            _doc(t, schedule={"cron": "61 * * * *", "tz": "UTC"}, ceiling={"firings": 1})
        ),
        "schedule.cron",
        "minute",
    ),  # fmt: skip
    (
        lambda t: _raw(
            _doc(t, schedule={"cron": "0 9 * * *", "tz": "Mars/Base"}, ceiling={"firings": 1})
        ),
        "schedule.tz",
        "Mars/Base",
    ),  # fmt: skip
]


@pytest.mark.parametrize(("make", "where", "said"), REFUSED)
def test_a_file_that_is_not_a_workflow_is_refused_and_says_where(tmp_path, make, where, said):
    with pytest.raises(WorkflowError) as caught:
        parse_workflow(make(tmp_path))
    assert caught.value.field == where
    assert said in caught.value.reason


def test_repeated_step_names_are_refused(tmp_path):
    step = {"name": "same", "kind": "ask", "prompt": "Go."}
    with pytest.raises(WorkflowError, match=r"steps\[1\]\.name: .*same.*already"):
        parse_workflow(_raw(_doc(tmp_path, steps=[step, step])))


def test_a_file_past_the_size_limit_is_refused_unread(tmp_path):
    raw = _raw(_doc(tmp_path, about="x" * MAX_BYTES))
    with pytest.raises(WorkflowError, match="larger than"):
        parse_workflow(raw)


# ── the store ───────────────────────────────────────────────────────────────


def test_the_store_keeps_the_exact_bytes_under_their_sha256(tmp_path, home):
    raw = json.dumps(_doc(tmp_path), indent=3).encode() + b"\n"
    wf = store_workflow(raw)
    kept = home / "workflows" / f"{wf.sha256}.json"
    assert kept.read_bytes() == raw
    assert os.stat(kept).st_mode & 0o777 == 0o600
    assert os.stat(home / "workflows").st_mode & 0o777 == 0o700
    assert resolve("date-fix") == wf.sha256
    assert resolve(wf.sha256) == wf.sha256
    assert resolve(wf.sha256[:12]) == wf.sha256
    assert load_workflow("date-fix").raw == raw


def test_a_name_finds_its_newest_version_and_each_version_stays(tmp_path):
    old = store_workflow(_raw(_doc(tmp_path)))
    new = store_workflow(_raw(_doc(tmp_path, about="second")))
    assert resolve("date-fix") == new.sha256
    assert load_workflow(old.sha256).about is None  # the older one is still there by its sha
    rows = stored_workflows()
    assert [(r.sha256, r.newest) for r in rows] == [(old.sha256, False), (new.sha256, True)]
    again = store_workflow(_raw(_doc(tmp_path)))  # the same bytes again: one copy, now newest
    assert again.sha256 == old.sha256 and resolve("date-fix") == old.sha256
    assert len(stored_workflows()) == 2


@pytest.mark.parametrize("ref", ["nothing", "abc", "0" * 12, "a" * 65])
def test_a_reference_to_no_stored_workflow_is_refused(tmp_path, ref):
    store_workflow(_raw(_doc(tmp_path)))
    with pytest.raises(KeyError):
        resolve(ref)


def test_a_short_sha_two_versions_share_is_refused(tmp_path, monkeypatch):
    a = store_workflow(_raw(_doc(tmp_path)))
    b = store_workflow(_raw(_doc(tmp_path, about="2")))
    common = os.path.commonprefix([a.sha256, b.sha256])
    if len(common) < 12:  # two real hashes seldom share 12 digits: say they share all of them
        index = json.loads((a.path.parent / "index.json").read_text())
        index[a.sha256[:12] + b.sha256[12:]] = index.pop(b.sha256)
        (a.path.parent / "index.json").write_text(json.dumps(index))
    with pytest.raises(KeyError, match="more than one"):
        resolve(a.sha256[:12])


def test_bytes_changed_since_they_were_stored_are_refused(tmp_path, home):
    wf = store_workflow(_raw(_doc(tmp_path)))
    wf.path.write_bytes(_raw(_doc(tmp_path, about="changed")))
    with pytest.raises(WorkflowError) as caught:
        load_workflow("date-fix")
    assert caught.value.field == "bytes"
    assert "changed since it was stored" in caught.value.reason


def test_a_kept_file_that_is_gone_is_refused(tmp_path):
    wf = store_workflow(_raw(_doc(tmp_path)))
    wf.path.unlink()
    with pytest.raises(WorkflowError, match="cannot be read"):
        load_workflow("date-fix")


def test_a_file_that_is_not_a_workflow_is_never_stored(tmp_path, home):
    with pytest.raises(WorkflowError):
        store_workflow(b"{}")
    assert not (home / "workflows").exists() or not any((home / "workflows").glob("*.json"))


# ── what stops it running here ──────────────────────────────────────────────


def test_a_workflow_that_fits_this_machine_has_no_problems(tmp_path):
    wf = parse_workflow(_raw(_step(tmp_path, engine="openai")))
    assert here_problems(wf) == []


def test_what_stops_a_workflow_here_is_named_step_by_step(tmp_path):
    save_config(Config())
    gone = tmp_path / "gone"
    data = _doc(
        tmp_path,
        cwd=str(gone),
        steps=[
            {"name": "a", "kind": "ask", "prompt": "Go.", "engine": "nosuch"},
            {"name": "b", "kind": "ask", "prompt": "Go.", "engine": "claude", "login": "work"},
            {"name": "c", "kind": "ask", "prompt": "Go.", "role": "nosuch"},
            {"name": "d", "kind": "office", "prompt": "Go.", "dir": str(tmp_path),
             "office_sha256": "a" * 64},
            {"name": "e", "kind": "ladder", "prompt": "Go.", "checks": ["rm -rf /"]},
        ],
    )  # fmt: skip
    said = "\n".join(here_problems(parse_workflow(_raw(data))))
    assert f"{gone} is not a folder" in said
    assert "step a: unknown engine 'nosuch'" in said
    assert "step b: " in said and "'work'" in said
    assert "step c: " in said and "nosuch" in said
    assert "step d: " in said and "office.yaml" in said
    assert "step e: " in said and "never run" in said


def test_an_office_whose_file_changed_since_the_workflow_named_it_cannot_run(tmp_path):
    office = tmp_path / "office"
    office.mkdir()
    (office / "office.yaml").write_text(
        "name: shop\ngates: {ready: {owner: [verdict]}}\n"
        "desks: {build: {harness: claude, role: build.md}}\n",
        encoding="utf-8",
    )
    (office / "build.md").write_text("You build.\n", encoding="utf-8")
    data = _step(tmp_path, kind="office", dir=str(office), office_sha256="0" * 64)
    [said] = here_problems(parse_workflow(_raw(data)))
    assert said.startswith("step s: ") and "office.yaml has changed" in said


def test_the_readme_example_reads_as_a_workflow():
    from pathlib import Path

    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
    section = readme.split("\n## Workflows\n", 1)[1].split("\n## ", 1)[0]
    example = section.split("```json\n", 1)[1].split("```", 1)[0]
    wf = parse_workflow(example.encode("utf-8"))
    assert [s.kind for s in wf.steps] == ["ladder", "ask"]
    assert wf.ceiling is not None and wf.ceiling.firings == 40


def test_an_index_changed_by_hand_never_finds_other_bytes_by_a_name(tmp_path):
    wf = store_workflow(_raw(_doc(tmp_path)))
    index_path = wf.path.parent / "index.json"
    index = json.loads(index_path.read_text())
    index[wf.sha256]["name"] = "other"
    index["../../escape"] = {"name": "escape", "seq": 99}  # never a sha256: never read
    index_path.write_text(json.dumps(index))
    assert [r.name for r in stored_workflows()] == ["other"]
    with pytest.raises(WorkflowError, match="the index names"):
        load_workflow("other")
    with pytest.raises(KeyError):
        resolve("escape")
