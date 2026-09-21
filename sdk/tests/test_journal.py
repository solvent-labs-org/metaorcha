"""The local run journal and its declared-acceptance rules.

The rules here mirror the platform's (``middleware/criteria.py`` and
``validator.run_observer._declared_acceptance``); the digest case is pinned to
the value the platform put in a real SM-2 envelope on 2026-09-21.
"""

from __future__ import annotations

import json
import stat

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from emerge.journal import (
    JOURNAL_FORMAT,
    append_step,
    compose_policy_version,
    criteria_digest,
    declared_acceptance_verdict,
    evaluate_step,
    journal_path,
    load_journal,
    new_journal,
    parse_criteria,
    parse_exit_code,
    receipt_path,
    save_journal,
    seal_journal,
)
from emerge.record import key_did
from emerge.run_attestation import verify_run_attestation

# sha256 of the canonical bytes of {"exit_zero": true}; the platform composed
# exactly this into policy_version on the 2026-09-21 SM-2 walk.
EXIT_ZERO_DIGEST = "7ebe884e1812714ae129de1868289cea8317d555b25186ed62e53e20a8cb45c7"


def _step(call_id: str, output, *, success: bool = True) -> dict:
    return {
        "call_id": call_id,
        "tool": "claude-code/Bash",
        "args": {"command": "true"},
        "output": output,
        "success": success,
        "latency_ms": 5,
    }


# ── Criteria: digest, composition, parsing ───────────────────────────────────


def test_criteria_digest_matches_the_platform_for_exit_zero():
    assert criteria_digest({"exit_zero": True}) == EXIT_ZERO_DIGEST


def test_policy_version_composes_exactly_like_the_platform():
    assert (
        compose_policy_version("local-session/1.0", EXIT_ZERO_DIGEST)
        == f"local-session/1.0+criteria:{EXIT_ZERO_DIGEST}"
    )
    assert compose_policy_version("local-session/1.0", None) == "local-session/1.0"


def test_parse_criteria_accepts_supported_keys_and_rejects_others():
    assert parse_criteria("exit_zero, citations_required") == {
        "exit_zero": True,
        "citations_required": True,
    }
    assert parse_criteria(None) == {}
    assert parse_criteria("") == {}
    with pytest.raises(ValueError, match="unsupported criterion 'no_such'"):
        parse_criteria("exit_zero,no_such")


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ({"exit_code": 0}, 0),
        ({"returncode": 3}, 3),
        ({"exit": "2"}, 2),
        ({"exit_code": True}, None),  # bool is not an exit code
        ({"stdout": "hi"}, None),
        ('{"exit_code": 1}', 1),  # a JSON string is decoded like raw_output
        ("not json", None),
        (None, None),
        ([1], None),
    ],
)
def test_parse_exit_code_reads_the_platform_keys_only(output, expected):
    assert parse_exit_code(output) == expected


# ── Per-step and run-level rules ─────────────────────────────────────────────


def test_exit_zero_is_not_applicable_on_a_step_without_an_exit_code():
    per = evaluate_step({"exit_zero": True}, {"stdout": "read a file"})
    assert per == {
        "exit_zero": {"result": "n/a", "detail": "no exit code in step output"}
    }


def test_exit_zero_fails_on_nonzero_and_passes_on_zero():
    assert evaluate_step({"exit_zero": True}, {"exit_code": 3})["exit_zero"] == {
        "result": "fail",
        "detail": "nonzero exit: 3",
    }
    assert evaluate_step({"exit_zero": True}, {"exit_code": 0})["exit_zero"] == {
        "result": "pass",
        "detail": "ok",
    }


def test_unsupported_criterion_fails_closed_and_false_is_not_a_declaration():
    per = evaluate_step({"exit_zero": False, "bogus": True}, {"exit_code": 0})
    assert "exit_zero" not in per
    assert per["bogus"]["result"] == "fail"


def test_run_verdict_fails_when_any_applicable_step_failed():
    steps = [
        _step("a", {"exit_code": 3}, success=False),
        _step("b", {"exit_code": 0}),
    ]
    assert declared_acceptance_verdict({"exit_zero": True}, steps) == {
        "check": "declared_acceptance",
        "result": "fail",
        "detail": "exit_zero: nonzero exit: 3",
    }


def test_run_verdict_passes_when_one_applied_and_none_failed():
    steps = [_step("a", {"stdout": "x"}), _step("b", {"exit_code": 0})]
    assert declared_acceptance_verdict({"exit_zero": True}, steps) == {
        "check": "declared_acceptance",
        "result": "pass",
        "detail": "ok",
    }


def test_run_verdict_fails_when_no_step_reported_an_exit_code():
    steps = [_step("a", {"stdout": "only reads"})]
    assert declared_acceptance_verdict({"exit_zero": True}, steps) == {
        "check": "declared_acceptance",
        "result": "fail",
        "detail": "no step reported an exit code",
    }
    assert declared_acceptance_verdict({"exit_zero": True}, []) == {
        "check": "declared_acceptance",
        "result": "fail",
        "detail": "no step reported an exit code",
    }


def test_no_criteria_means_no_verdict():
    assert declared_acceptance_verdict({}, [_step("a", {"exit_code": 0})]) is None


# ── Journal file ─────────────────────────────────────────────────────────────


def test_journal_and_receipt_paths_are_confined_to_dot_orcha(tmp_path):
    assert (
        journal_path(tmp_path, "sess-1") == tmp_path / ".orcha" / "runs" / "sess-1.json"
    )
    assert (
        receipt_path(tmp_path, "sess-1")
        == tmp_path / ".orcha" / "receipts" / "sess-1.json"
    )
    for bad in ("../x", "a/b", "", "x" * 129, "sp ace"):
        with pytest.raises(ValueError, match="run_id"):
            journal_path(tmp_path, bad)


def test_save_journal_is_owner_only_and_round_trips(tmp_path):
    journal = new_journal(run_id="s1", agent_dids=[], criteria={"exit_zero": True})
    append_step(journal, _step("a", {"exit_code": 0}))
    path = journal_path(tmp_path, "s1")
    save_journal(path, journal)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not path.with_name(path.name + ".tmp").exists()
    loaded = load_journal(path)
    assert loaded["format"] == JOURNAL_FORMAT
    assert loaded["steps"] == journal["steps"]


def test_append_step_drops_a_repeated_call_id():
    journal = new_journal(run_id="s1", agent_dids=[])
    append_step(journal, _step("a", {"exit_code": 0}))
    append_step(journal, _step("a", {"exit_code": 3}, success=False))
    assert len(journal["steps"]) == 1
    assert journal["steps"][0]["output"] == {"exit_code": 0}


def test_load_journal_rejects_a_foreign_file(tmp_path):
    path = tmp_path / "j.json"
    path.write_text(json.dumps({"format": "other", "steps": []}))
    with pytest.raises(ValueError, match="not a orcha.run-journal/v1 journal"):
        load_journal(path)


# ── Sealing ──────────────────────────────────────────────────────────────────


def test_seal_journal_yields_a_verifiable_receipt_with_platform_shaped_fields():
    key = Ed25519PrivateKey.generate()
    journal = new_journal(
        run_id="sess-9",
        agent_dids=[],
        policy="local-session/1.0",
        criteria={"exit_zero": True},
        started_at="2026-09-21T10:00:00Z",
        harness="claude-code",
    )
    append_step(
        journal, _step("a", {"exit_code": 3, "error": "Exit code 3"}, success=False)
    )
    append_step(journal, _step("b", {"exit_code": 0, "stdout": "ok"}))
    envelope = seal_journal(journal, key, finished_at="2026-09-21T10:00:05Z")

    assert envelope["run_id"] == "sess-9"
    assert envelope["agent_dids"] == [key_did(key)]
    assert (
        envelope["policy_version"] == f"local-session/1.0+criteria:{EXIT_ZERO_DIGEST}"
    )
    assert envelope["verdicts"] == [
        {
            "check": "declared_acceptance",
            "result": "fail",
            "detail": "exit_zero: nonzero exit: 3",
        }
    ]
    assert [s["success"] for s in envelope["steps"]] == [False, True]
    rendered = json.dumps(envelope)
    assert "Exit code 3" not in rendered  # raw output never enters the receipt
    assert verify_run_attestation(envelope).valid


def test_seal_journal_without_criteria_has_no_verdict_and_a_plain_policy():
    key = Ed25519PrivateKey.generate()
    journal = new_journal(run_id="s", agent_dids=["did:orcha:agent:me"])
    append_step(journal, _step("a", {"exit_code": 0}))
    envelope = seal_journal(journal, key)
    assert envelope["policy_version"] == "local-session/1.0"
    assert envelope["verdicts"] == []
    assert envelope["agent_dids"] == ["did:orcha:agent:me"]
    assert verify_run_attestation(envelope).valid
