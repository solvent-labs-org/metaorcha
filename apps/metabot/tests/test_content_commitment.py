"""A receipt commits to its run's prompt and answer, and a seal to its task, each by a
salted hash in one signed ``content`` verdict. The salts stay in the journal, which
re-derives every commitment; given a text and its salt, anyone holding a copy of the
receipt checks it offline with the SDK alone."""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import re
import shutil
import subprocess
import sys

import pytest
from emerge.record import hash_value

from metabot.approvals import DenyAll
from metabot.cli import main
from metabot.gate import Policy
from metabot.paths import metabot_home, receipt_path, run_dir
from metabot.receipts import ensure_key, journal_matches, seal_and_store
from metabot.runner import run_job
from test_web_pages import Asker, _client

ZEROS = "0" * 64
DETAIL = re.compile(r"prompt=[0-9a-f]{64};final=[0-9a-f]{64}")


def _run(tmp_path, prompt="a task", answer="done", **kw):
    return asyncio.run(
        run_job(
            engine=Asker(answer=answer), prompt=prompt, cwd=tmp_path, policy=Policy(),
            approver=DenyAll(), **kw,
        )
    )  # fmt: skip


def _envelope(run_id) -> dict:
    return json.loads(receipt_path(run_id).read_text(encoding="utf-8"))


def _journal(run_id) -> dict:
    return json.loads((run_dir(run_id) / "journal.json").read_text(encoding="utf-8"))


def _rewrite(run_id, journal) -> None:
    (run_dir(run_id) / "journal.json").write_text(json.dumps(journal), encoding="utf-8")


def _pins(run_id) -> dict[str, str]:
    [verdict] = [v for v in _envelope(run_id)["verdicts"] if v["check"] == "content"]
    return dict(part.split("=", 1) for part in verdict["detail"].split(";"))


def _opens(run_id, field) -> bool:
    j = _journal(run_id)
    return _pins(run_id)[field] == hash_value(
        {"field": field, "salt": j["salts"][field], "text": j["meta"][field]}
    )


# 1
def test_the_content_digest_matches_the_golden_vectors():
    from metabot.receipts import content_digest

    canonical = '{"field":"final","salt":"' + ZEROS + '","text":"caf\\u00e9;answer=x"}'
    assert json.dumps(
        {"field": "final", "salt": ZEROS, "text": "café;answer=x"},
        sort_keys=True, separators=(",", ":"),
    ) == canonical  # fmt: skip
    assert content_digest("prompt", "hello", ZEROS) == (
        "bb88224ea9d648d84c14cf39260832207580c31145f1fdae72322e472daced72"
    )
    assert content_digest("final", "café;answer=x", ZEROS) == (
        "54978e5cbcc20e57896c490221ca6e8b45991c3248a73e291e85c8a13f4816d9"
    )
    assert content_digest("task", "", ZEROS) == (
        "36c8390bf2bcaa362a5566c16095bc8b4844000e91b95f3bdaa58eeebb212665"
    )


# 2
def test_a_run_commits_to_its_prompt_and_answer(tmp_path):
    result = _run(tmp_path, prompt="2+2", answer="4")
    envelope, journal = _envelope(result.run_id), _journal(result.run_id)
    found = [v for v in envelope["verdicts"] if v["check"] == "content"]
    assert len(found) == 1, "no content verdict"
    assert found[0]["result"] == "pass" and DETAIL.fullmatch(found[0]["detail"])
    assert envelope["steps"] == [] and result.steps == 0  # a commitment is no step
    assert set(journal["salts"]) == {"prompt", "final"}
    for field, text in (("prompt", "2+2"), ("final", "4")):
        salt = journal["salts"][field]
        assert re.fullmatch(r"[0-9a-f]{64}", salt)
        opening = {"field": field, "salt": salt, "text": text}
        canonical = json.dumps(opening, sort_keys=True, separators=(",", ":")).encode()
        assert _pins(result.run_id)[field] == hash_value(opening)
        assert _pins(result.run_id)[field] == hashlib.sha256(canonical).hexdigest()
    assert journal_matches(result.run_id)


# 3
EDITS = {
    "prompt edited": lambda j: j["meta"].update(prompt="another task"),
    "answer edited": lambda j: j["meta"].update(final="another answer"),
    "prompt removed": lambda j: j["meta"].pop("prompt"),
    "salt edited": lambda j: j["salts"].update(prompt=ZEROS),
    "salts removed": lambda j: j.pop("salts"),
    "a salt added": lambda j: j["salts"].update(status=ZEROS),
}


@pytest.mark.parametrize("edit", EDITS.values(), ids=EDITS.keys())
def test_editing_the_kept_prompt_answer_or_salt_breaks_the_journal(tmp_path, capsys, edit):
    result = _run(tmp_path)
    assert journal_matches(result.run_id)
    journal = _journal(result.run_id)
    edit(journal)
    _rewrite(result.run_id, journal)
    assert journal_matches(result.run_id) is False
    assert main(["receipts", "verify", result.run_id]) == 1
    assert "journal DOES NOT MATCH the receipt" in capsys.readouterr().out


# 4
def test_a_credential_is_redacted_before_the_prompt_and_answer_are_committed(tmp_path):
    result = _run(
        tmp_path, prompt="use tok-123 here", answer="tok-123 is set",
        secrets={"GH_TOKEN": "tok-123"},
    )  # fmt: skip
    meta = _journal(result.run_id)["meta"]
    assert meta["prompt"] == "use [REDACTED:GH_TOKEN] here"
    assert meta["final"] == "[REDACTED:GH_TOKEN] is set"
    assert _opens(result.run_id, "prompt") and _opens(result.run_id, "final")
    for path in (receipt_path(result.run_id), run_dir(result.run_id) / "journal.json"):
        assert "tok-123" not in path.read_text(encoding="utf-8")


# 5
def test_a_short_prompt_cannot_be_guessed_from_a_receipt_that_travels(tmp_path):
    first = _run(tmp_path, prompt="2+2", project=tmp_path)
    second = _run(tmp_path, prompt="2+2", project=tmp_path)
    assert _pins(first.run_id)["prompt"] != _pins(second.run_id)["prompt"]
    index = (metabot_home() / "runs.jsonl").read_text(encoding="utf-8")
    for run in (first, second):
        assert run.project_receipt is not None
        travelling = [receipt_path(run.run_id).read_text(), run.project_receipt.read_text()]
        for text in travelling:
            assert hashlib.sha256(b"2+2").hexdigest() not in text
            assert hash_value("2+2") not in text
        for salt in _journal(run.run_id)["salts"].values():
            assert all(salt not in text for text in [*travelling, index])


# 6
SDK_ONLY = """
import json, sys
from emerge.run_attestation import canonical_json_bytes, sha256_hex, verify_run_attestation
receipt, field, salt, text = sys.argv[1:]
envelope = json.load(open(receipt, encoding="utf-8"))
pins = [v for v in envelope["verdicts"] if v["check"] == "content"]
assert verify_run_attestation(envelope).valid and len(pins) == 1
parts = dict(p.split("=", 1) for p in pins[0]["detail"].split(";"))
opened = sha256_hex(canonical_json_bytes({"field": field, "salt": salt, "text": text}))
sys.exit(0 if parts.get(field) == opened and "metabot" not in sys.modules else 1)
"""


def test_a_disclosed_prompt_checks_with_the_sdk_alone(tmp_path):
    result = _run(tmp_path, prompt="use tok-123 here", secrets={"GH_TOKEN": "tok-123"})
    journal = _journal(result.run_id)
    copy = tmp_path / "shared" / "receipt.json"
    copy.parent.mkdir()
    shutil.copy(receipt_path(result.run_id), copy)
    shutil.rmtree(run_dir(result.run_id))  # the journal stays behind; the copy travels

    def check(field, salt, text):
        argv = [sys.executable, "-I", "-B", "-c", SDK_ONLY, str(copy), field, salt, text]
        return subprocess.run(argv, cwd=tmp_path, check=False).returncode

    prompt, salts = journal["meta"]["prompt"], journal["salts"]
    assert check("prompt", salts["prompt"], prompt) == 0
    assert check("final", salts["final"], journal["meta"]["final"]) == 0
    assert check("prompt", salts["prompt"], prompt + "x") == 1
    assert check("prompt", salts["prompt"], "use tok-123 here") == 1  # only the kept text
    assert check("prompt", salts["final"], journal["meta"]["final"]) == 1  # relabelled


# 7
def test_an_empty_answer_is_committed_too(tmp_path):
    result = _run(tmp_path, answer="")
    assert _journal(result.run_id)["meta"]["final"] == ""
    assert _opens(result.run_id, "final") and journal_matches(result.run_id)


# 8
def _sealed(run_id, verdicts=(), **kw):
    key, _ = ensure_key()
    seal_and_store(
        run_id=run_id, steps=[], verdicts=list(verdicts), started_at="2026-10-11T10:00:00Z",
        finished_at="2026-10-11T10:00:05Z", key=key, meta={"prompt": "kept"}, **kw,
    )  # fmt: skip


def test_a_receipt_that_commits_to_nothing_matches_as_before():
    _sealed("mb-test-000008", commit=())
    assert "salts" not in _journal("mb-test-000008")
    assert all(v["check"] != "content" for v in _envelope("mb-test-000008")["verdicts"])
    assert journal_matches("mb-test-000008") is True


# 9
BAD = {
    "not hex": ([{"check": "content", "result": "pass", "detail": "prompt=zz"}], ()),
    "a name twice": (
        [{"check": "content", "result": "pass", "detail": f"prompt={ZEROS};prompt={ZEROS}"}],
        (),
    ),
    "empty": ([{"check": "content", "result": "pass", "detail": ""}], ()),
    "a pin with no salt": (
        [{"check": "content", "result": "pass", "detail": f"prompt={ZEROS}"}],
        (),
    ),
    "two verdicts": ([{"check": "content", "result": "pass", "detail": f"x={ZEROS}"}], ("prompt",)),
}


@pytest.mark.parametrize("verdicts, commit", BAD.values(), ids=BAD.keys())
def test_a_malformed_or_doubled_content_verdict_does_not_match(verdicts, commit):
    _sealed("mb-test-000009", verdicts, **({"commit": commit} if commit else {}))
    assert journal_matches("mb-test-000009") is False


# 10
async def test_a_desk_rounds_prompt_is_committed_with_its_handoff(tmp_path):
    from test_seals import _looped

    _, office = await _looped(tmp_path)
    second = f"{office}.build.r2"
    kept = _journal(second)["meta"]["prompt"]
    assert "This is round 2 of 3" in kept and ".metabot/inbox/" in kept
    assert _opens(second, "prompt") and _opens(second, "final")
    assert journal_matches(second) and journal_matches(office)
    checks = _envelope(f"{office}.build.checks")["verdicts"]
    assert all(v["check"] != "content" for v in checks)  # a checks run's prompt is a label


# 11
def test_the_run_page_says_what_the_receipt_commits_to(tmp_path):
    from metabot.web_pages import COMMITTED, UNSIGNED

    result = _run(tmp_path, answer="the answer")
    client = _client(tmp_path)
    text = client.get(f"/runs/{result.run_id}").text
    assert '<span class="ok">valid</span>' in text
    assert html.escape(COMMITTED, quote=True) in text
    assert html.escape(UNSIGNED, quote=True) not in text
    journal = _journal(result.run_id)
    journal["meta"]["prompt"] = "a prompt nobody gave"
    _rewrite(result.run_id, journal)
    text = client.get(f"/runs/{result.run_id}").text
    assert "JOURNAL MISMATCH" in text and '<span class="ok">valid</span>' not in text
    assert html.escape(COMMITTED, quote=True) not in text
    _sealed("mb-test-000011")  # commits to nothing: the page says the prompt is not signed
    text = client.get("/runs/mb-test-000011").text
    assert html.escape(UNSIGNED, quote=True) in text
