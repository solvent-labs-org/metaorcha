from __future__ import annotations

import json

import pytest

from conftest import private_mode
from metabot import receipts
from metabot.paths import run_dir

STARTED = "2026-10-11T10:00:00Z"
FINISHED = "2026-10-11T10:00:05Z"


def _steps(output="ok"):
    return [
        {
            "call_id": "t1",
            "tool": "claude-code/Bash",
            "args": {"command": "ls"},
            "output": "a\nb",
            "success": True,
            "latency_ms": 12,
        },
        {
            "call_id": "sw-1",
            "tool": "did:orcha:agent:conn-0123456789ab#list_issues",
            "args": {"repo": "x"},
            "output": output,
            "success": True,
            "latency_ms": 30,
        },
    ]


def _seal(run_id="mb-test-000001", output="ok", secrets=None):
    key, did = receipts.ensure_key()
    path = receipts.seal_and_store(
        run_id=run_id,
        steps=_steps(output),
        verdicts=[receipts.model_verdict("claude", "claude-haiku-5-5")],
        started_at=STARTED,
        finished_at=FINISHED,
        key=key,
        secrets=secrets,
    )
    return path, did


def test_ensure_key_creates_once(tmp_path):
    key1, did1 = receipts.ensure_key()
    key2, did2 = receipts.ensure_key()
    assert did1 == did2
    assert did1.startswith("did:orcha:agent:key-")
    assert private_mode(tmp_path / "orcha-key") == 0o600


def test_seal_verifies():
    path, did = _seal()
    verdict = receipts.verify_file(path)
    assert verdict.valid is True
    assert verdict.step_count == 2
    env = json.loads(path.read_text())
    assert env["signer"]["did"] == did
    assert env["agent_dids"] == [did]
    assert env["policy_version"] == "metabot/1.0"
    assert {"check": "model", "result": "pass", "detail": "claude/claude-haiku-5-5"} in env[
        "verdicts"
    ]


def test_flip_breaks():
    path, _ = _seal()
    env = json.loads(path.read_text())
    h = env["steps"][0]["output_hash"]
    env["steps"][0]["output_hash"] = h[:-1] + ("0" if h[-1] != "0" else "1")
    path.write_text(json.dumps(env))
    assert receipts.verify_file(path).valid is False


def test_secret_never_written():
    path, _ = _seal(output="token is tok-123 here", secrets={"GH_TOKEN": "tok-123"})
    journal = run_dir("mb-test-000001") / "journal.json"
    assert b"tok-123" not in path.read_bytes()
    assert b"tok-123" not in journal.read_bytes()
    assert private_mode(journal) == 0o600
    assert "GH_TOKEN" in journal.read_text()


def test_index_appends():
    _seal("mb-test-000001")
    _seal("mb-test-000002")
    index = receipts.list_index()
    assert [row["run_id"] for row in index] == ["mb-test-000001", "mb-test-000002"]
    assert index[0]["steps"] == 2


def test_approval_verdicts_mirror_the_platform():
    assert receipts.approval_verdict("c1", approved=True, approver="did:orcha:agent:key-x") == {
        "check": "scope_approval:c1",
        "result": "pass",
        "detail": "did:orcha:agent:key-x",
    }
    assert receipts.approval_verdict("c2", approved=False) == {
        "check": "scope_approval:c2",
        "result": "warn",
        "detail": "declined",
    }
    assert receipts.approval_verdict("c3", approved=False, reason="no human present") == {
        "check": "scope_approval:c3",
        "result": "warn",
        "detail": "declined: no human present",
    }


def test_an_allow_verdict_names_the_allow_and_is_not_an_approval():
    # scope_approval is a person's decision; an allow was set before the run began
    assert receipts.allow_verdict("c4", "allow_write") == {
        "check": "scope_allow:c4",
        "result": "pass",
        "detail": "allow_write",
    }
    assert receipts.allow_verdict("c5", "allows did:orcha:agent:conn-1#create_issue") == {
        "check": "scope_allow:c5",
        "result": "pass",
        "detail": "allows did:orcha:agent:conn-1#create_issue",
    }


CAVEAT = "The answer may say otherwise; the receipt's steps are what happened."


def test_declines_and_other_failures_are_each_counted_once():
    steps = [
        {"call_id": "c1", "success": False},  # declined, so its step failed too
        {"call_id": "c2", "success": False},
        {"call_id": "c3", "success": True},
        {"call_id": "c4", "success": True},
    ]
    verdicts = [
        {"check": "model", "result": "pass", "detail": "fake/default"},
        {"check": "scope_approval:c1", "result": "warn", "detail": "declined"},
        {"check": "scope_approval:c3", "result": "pass", "detail": "did:key:x"},
        {"check": "scope_allow:c4", "result": "pass", "detail": "allow_write"},
        {"check": "scope_approval:c9", "result": "warn", "detail": "declined: limit"},  # no step
    ]
    assert receipts.declined_and_failed(steps, verdicts) == (2, 1)
    assert receipts.declined_and_failed([], []) == (0, 0)


@pytest.mark.parametrize(
    ("declined", "failed", "said"),
    [
        (0, 0, None),
        (1, 0, f"1 call was declined. {CAVEAT}"),
        (0, 3, f"3 calls did not succeed. {CAVEAT}"),
        (2, 1, f"2 calls were declined and 1 call did not succeed. {CAVEAT}"),
    ],
)
def test_the_caveat_says_how_many_calls_did_not_go_as_asked(declined, failed, said):
    assert receipts.answer_caveat(declined, failed) == said


def test_named_here_finds_the_files_and_folders_a_prompt_names(tmp_path):
    (tmp_path / "calc.py").write_text("")
    (tmp_path / ".gitignore").write_text("")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "x.txt").write_text("")
    (tmp_path / "out").symlink_to(tmp_path.parent)
    prompt = (
        "what does calc.py do? and sub/x.txt, `.gitignore`, sub/, ../, out/, missing.py, "
        f"{tmp_path / 'calc.py'}, /etc/hostname, e.g. v1.2 and calc.py."
    )
    assert receipts.named_here(prompt, tmp_path) == ["calc.py", "sub/x.txt", ".gitignore", "sub/"]
    assert receipts.named_here("what is two plus two?", tmp_path) == []
    assert receipts.named_here("look at sub. then stop.", tmp_path) == []  # full stops only


@pytest.mark.parametrize(
    ("named", "said"),
    [
        ([], None),
        (["calc.py"], "calc.py is here, but this run made no tool calls, so {} of it."),
        (
            ["a.py", "b.py"],
            "a.py and b.py are here, but this run made no tool calls, so {} of them.",
        ),
        (["a", "b", "c"], "a, b and c are here, but this run made no tool calls, so {} of them."),
        (
            ["a", "b", "c", "d", "e"],
            "a, b, c and 2 more are here, but this run made no tool calls, so {} of them.",
        ),
    ],
)
def test_the_unread_caveat_names_what_the_run_never_looked_at(named, said):
    want = said.format("the receipt shows no read") if said else None
    assert receipts.unread_caveat(named) == want


def test_journal_rederives_every_hash():
    path, _ = _seal(output="token is tok-123 here", secrets={"GH_TOKEN": "tok-123"})
    assert receipts.journal_matches("mb-test-000001") is True
    journal_path = run_dir("mb-test-000001") / "journal.json"
    journal = json.loads(journal_path.read_text())
    journal["steps"][1]["output"] = "something else"
    journal_path.write_text(json.dumps(journal))
    assert receipts.journal_matches("mb-test-000001") is False


SIGNED_COPIES = [
    ("verdicts", [{"check": "model", "result": "pass", "detail": "claude/another-model"}]),
    ("verdicts", []),
    ("signer", "did:orcha:agent:key-0000000000000000"),
    ("started_at", "2026-10-11T09:00:00Z"),
    ("finished_at", "2026-10-11T11:00:00Z"),
    ("run_id", "mb-test-000002"),
]


@pytest.mark.parametrize(("key", "value"), SIGNED_COPIES)
def test_a_journal_whose_copy_of_a_signed_fact_was_edited_does_not_match(key, value):
    # the run page shows these from the journal, beside the receipt's state
    _seal()
    path = run_dir("mb-test-000001") / "journal.json"
    journal = json.loads(path.read_text())
    assert journal[key] != value
    journal[key] = value
    path.write_text(json.dumps(journal))
    assert receipts.journal_matches("mb-test-000001") is False


@pytest.mark.parametrize("side", ["journal", "receipt"])
@pytest.mark.parametrize("steps", [{"a": 1, "b": 2}, ["one", "two"], "ab"])
def test_steps_edited_into_another_shape_do_not_match(side, steps):
    _seal()
    if side == "journal":
        path = run_dir("mb-test-000001") / "journal.json"
    else:
        path = receipts.receipts_dir() / "mb-test-000001.json"
    data = json.loads(path.read_text())
    data["steps"] = steps
    path.write_text(json.dumps(data))
    assert receipts.journal_matches("mb-test-000001") is False


def test_labels_are_made_printable_before_sealing():
    key, _ = receipts.ensure_key()
    steps = _steps()
    steps[0]["tool"] = "claude-code/Bäsh\n"
    steps[0]["call_id"] = "id\nwith newline"
    path = receipts.seal_and_store(
        run_id="mb-test-000003",
        steps=steps,
        verdicts=[receipts.model_verdict("cursor", "Grök 4.7")],
        started_at=STARTED,
        finished_at=FINISHED,
        key=key,
    )
    assert receipts.verify_file(path).valid is True


def test_unreadable_receipt_is_invalid(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert receipts.verify_file(bad).valid is False
    assert receipts.verify_file(tmp_path / "missing.json").valid is False


def test_index_skips_a_torn_line():
    _seal("mb-test-000001")
    from metabot.paths import metabot_home

    with open(metabot_home() / "runs.jsonl", "a") as fh:
        fh.write('{"run_id": "torn')
    assert [row["run_id"] for row in receipts.list_index()] == ["mb-test-000001"]
