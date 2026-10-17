from __future__ import annotations

import pytest

from metabot.approvals import DenyAll, Scripted
from metabot.decisions import Decider
from metabot.gate import Policy

SIGNER = "did:key:test"


def _ask(cls: object) -> dict:
    return {"op": "approve", "call_id": "c1", "tool": "t", "cls": cls, "preview": ""}


async def test_a_read_is_allowed_without_asking_anyone():
    # READ is 0, so a test for "parsed or not" read it as missing.
    approver = Scripted([])
    decider = Decider(Policy(), approver, SIGNER)
    reply = await decider(_ask("read"))
    assert reply == {"allow": True, "reason": "read"}
    assert approver.asked == 0
    assert decider.verdicts == []


async def test_a_read_is_allowed_with_no_human_present():
    decider = Decider(Policy(), DenyAll(), SIGNER)
    assert (await decider(_ask("read")))["allow"] is True


@pytest.mark.parametrize("cls", [None, "", "everything", 7, "READ ONLY"])
async def test_a_class_the_decider_cannot_read_is_destructive(cls):
    approver = Scripted([True])
    decider = Decider(Policy(allow_write=True), approver, SIGNER)
    reply = await decider(_ask(cls))
    assert approver.asked == 1
    assert approver.requests[0].cls.label == "destructive"
    assert reply["allow"] is True
    assert decider.verdicts[0]["result"] == "pass"


@pytest.mark.parametrize(
    ("policy", "allow"),
    [(Policy(allow_write=True), "allow_write"), (Policy(allows=frozenset({"t"})), "allows t")],
)
async def test_a_write_the_policy_allows_is_recorded_with_its_allow(policy, allow):
    approver = Scripted([])
    decider = Decider(policy, approver, SIGNER)
    reply = await decider(_ask("write"))
    assert reply["allow"] is True and approver.asked == 0
    assert decider.verdicts == [{"check": "scope_allow:c1", "result": "pass", "detail": allow}]


async def test_a_write_allowed_with_no_human_present_is_recorded_with_its_allow():
    decider = Decider(Policy(allows=frozenset({"t"})), DenyAll(), SIGNER)  # a routine
    assert (await decider(_ask("write")))["allow"] is True
    assert decider.verdicts == [{"check": "scope_allow:c1", "result": "pass", "detail": "allows t"}]


async def test_a_read_leaves_no_verdict_whatever_the_policy():
    decider = Decider(Policy(allow_write=True, allows=frozenset({"t"})), Scripted([]), SIGNER)
    assert (await decider(_ask("read")))["allow"] is True
    assert decider.verdicts == []


async def test_a_write_with_no_human_present_is_declined_and_recorded():
    decider = Decider(Policy(), DenyAll(), SIGNER)
    reply = await decider(_ask("write"))
    assert reply == {"allow": False, "reason": "no human present"}
    assert decider.verdicts[0]["detail"] == "declined: no human present"
