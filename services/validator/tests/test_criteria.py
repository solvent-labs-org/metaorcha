"""Declared-criteria digest composition on policy_version (FR-4/5/6)."""

from __future__ import annotations

import pytest
from emerge.run_attestation import verify_run_attestation
from validator.criteria import (
    compose_policy_version,
    criteria_digest,
    parse_policy_version,
)
from validator.run_envelope import verify_run_envelope
from validator.run_observer import DEFAULT_POLICY_VERSION, RunAttestationObserver

from conftest import FakeDB, _step_result


def test_compose_and_parse_roundtrip() -> None:
    digest = "a" * 64
    composed = compose_policy_version("run-attestation/1.0", digest)
    assert composed == f"run-attestation/1.0+criteria:{digest}"
    policy, recovered = parse_policy_version(composed)
    assert policy == "run-attestation/1.0"
    assert recovered == digest


def test_no_digest_is_unchanged() -> None:
    assert compose_policy_version("run-attestation/1.0", None) == "run-attestation/1.0"
    assert parse_policy_version("run-attestation/1.0") == ("run-attestation/1.0", None)


def test_digest_changes_when_criteria_change() -> None:
    a = criteria_digest({"citations_required": True})
    b = criteria_digest({"citations_required": False})
    assert a != b
    assert len(a) == 64
    assert criteria_digest({"citations_required": True}) == a


@pytest.mark.asyncio
async def test_declared_acceptance_in_signed_bytes() -> None:
    digest = criteria_digest({"citations_required": True})
    observer = RunAttestationObserver(db=FakeDB())
    await observer.on_step_complete(
        _step_result(
            "c1",
            session_id="sess-crit",
            metadata={
                "declared_acceptance": {"result": "pass", "detail": "ok"},
                "criteria_digest": digest,
            },
        )
    )
    await observer.on_run_complete("sess-crit")
    envelope = next(iter(observer.envelopes.values()))
    assert verify_run_envelope(envelope) is True
    assert verify_run_attestation(envelope).valid is True
    policy, recovered = parse_policy_version(envelope["policy_version"])
    assert policy == DEFAULT_POLICY_VERSION
    assert recovered == digest
    assert {"check": "declared_acceptance", "result": "pass", "detail": "ok"} in envelope[
        "verdicts"
    ]


@pytest.mark.asyncio
async def test_two_runs_different_criteria_both_recoverable() -> None:
    observer = RunAttestationObserver(db=FakeDB())
    first = criteria_digest({"citations_required": True})
    second = criteria_digest({"exit_zero": True})
    await observer.on_step_complete(
        _step_result(
            "a",
            session_id="sess-a",
            metadata={
                "declared_acceptance": {"result": "fail", "detail": "missing citations"},
                "criteria_digest": first,
            },
        )
    )
    await observer.on_run_complete("sess-a")
    await observer.on_step_complete(
        _step_result(
            "b",
            session_id="sess-b",
            metadata={
                "declared_acceptance": {"result": "pass", "detail": "ok"},
                "criteria_digest": second,
            },
        )
    )
    await observer.on_run_complete("sess-b")
    envelopes = list(observer.envelopes.values())
    assert len(envelopes) == 2
    parsed = [parse_policy_version(e["policy_version"]) for e in envelopes]
    digests = {d for _, d in parsed}
    policies = {p for p, _ in parsed}
    assert digests == {first, second}
    assert policies == {DEFAULT_POLICY_VERSION}
    for envelope in envelopes:
        assert verify_run_attestation(envelope).valid is True


@pytest.mark.asyncio
async def test_no_criteria_policy_version_is_today() -> None:
    observer = RunAttestationObserver(db=FakeDB())
    await observer.on_step_complete(_step_result("c1", session_id="sess-plain"))
    await observer.on_run_complete("sess-plain")
    envelope = next(iter(observer.envelopes.values()))
    assert envelope["policy_version"] == DEFAULT_POLICY_VERSION
    assert "+criteria:" not in envelope["policy_version"]
    assert all(v["check"] != "declared_acceptance" for v in envelope["verdicts"])


def _declared(result: str, detail: str, **criteria: str) -> dict[str, object]:
    return {"result": result, "detail": detail, "criteria": criteria}


async def _run_verdict(
    steps: list[dict[str, object]], session: str
) -> dict[str, object]:
    observer = RunAttestationObserver(db=FakeDB())
    digest = criteria_digest({"exit_zero": True})
    for i, declared in enumerate(steps):
        await observer.on_step_complete(
            _step_result(
                f"c{i}",
                session_id=session,
                metadata={"declared_acceptance": declared, "criteria_digest": digest},
            )
        )
    await observer.on_run_complete(session)
    envelope = next(iter(observer.envelopes.values()))
    assert verify_run_attestation(envelope).valid is True
    return next(v for v in envelope["verdicts"] if v["check"] == "declared_acceptance")


@pytest.mark.asyncio
async def test_prd_journey_read_patch_tests_exit_zero_passes() -> None:
    # read (n/a) → patch (n/a) → tests exit 0 (pass): the run passes.
    verdict = await _run_verdict(
        [
            _declared("n/a", "no exit code in step output", exit_zero="n/a"),
            _declared("n/a", "no exit code in step output", exit_zero="n/a"),
            _declared("pass", "ok", exit_zero="pass"),
        ],
        "sess-journey-pass",
    )
    assert verdict == {"check": "declared_acceptance", "result": "pass", "detail": "ok"}


@pytest.mark.asyncio
async def test_prd_journey_tests_exit_one_fails_and_names_the_step_detail() -> None:
    verdict = await _run_verdict(
        [
            _declared("n/a", "no exit code in step output", exit_zero="n/a"),
            _declared("fail", "nonzero exit: 1", exit_zero="fail"),
        ],
        "sess-journey-fail",
    )
    assert verdict["result"] == "fail"
    assert verdict["detail"] == "exit_zero: nonzero exit: 1"


@pytest.mark.asyncio
async def test_no_step_reported_an_exit_code_fails_the_run() -> None:
    # Declaring exit_zero and never running anything is not acceptance.
    verdict = await _run_verdict(
        [
            _declared("n/a", "no exit code in step output", exit_zero="n/a"),
            _declared("n/a", "no exit code in step output", exit_zero="n/a"),
        ],
        "sess-journey-none",
    )
    assert verdict == {
        "check": "declared_acceptance",
        "result": "fail",
        "detail": "no step reported an exit code",
    }


@pytest.mark.asyncio
async def test_fail_wins_across_steps_even_after_a_pass() -> None:
    verdict = await _run_verdict(
        [
            _declared("pass", "ok", exit_zero="pass"),
            _declared("fail", "nonzero exit: 3", exit_zero="fail"),
        ],
        "sess-fail-after-pass",
    )
    assert verdict["result"] == "fail"
    assert verdict["detail"] == "exit_zero: nonzero exit: 3"


@pytest.mark.asyncio
async def test_citations_fail_on_one_step_fails_the_run_with_exit_zero_passing() -> (
    None
):
    verdict = await _run_verdict(
        [
            _declared(
                "fail", "missing citations", citations_required="fail", exit_zero="n/a"
            ),
            _declared("pass", "ok", citations_required="pass", exit_zero="pass"),
        ],
        "sess-mixed",
    )
    assert verdict["result"] == "fail"
    assert verdict["detail"] == "citations_required: missing citations"
