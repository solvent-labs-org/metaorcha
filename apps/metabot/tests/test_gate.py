from __future__ import annotations

from metabot.gate import Policy, decide
from metabot.scope_classes import ScopeClass

TOOL = "did:orcha:agent:conn-1#create_issue"


def test_read_allows():
    d = decide(TOOL, ScopeClass.READ, Policy())
    assert d.action == "allow"


def test_write_asks_by_default():
    assert decide(TOOL, ScopeClass.WRITE, Policy()).action == "ask"


def test_write_allowed_by_job():
    assert decide(TOOL, ScopeClass.WRITE, Policy(allow_write=True)).action == "allow"


def test_write_allowed_for_exactly_one_tool():
    policy = Policy(allows=frozenset({TOOL}))
    assert decide(TOOL, ScopeClass.WRITE, policy).action == "allow"
    assert decide("did:orcha:agent:conn-1#update_issue", ScopeClass.WRITE, policy).action == "ask"


def test_a_write_the_policy_allows_names_the_allow():
    assert decide(TOOL, ScopeClass.WRITE, Policy(allow_write=True)).allow == "allow_write"
    exact = decide(TOOL, ScopeClass.WRITE, Policy(allows=frozenset({TOOL})))
    assert exact.allow == f"allows {TOOL}"
    both = Policy(allow_write=True, allows=frozenset({TOOL}))
    assert decide(TOOL, ScopeClass.WRITE, both).allow == "allow_write"  # checked first
    assert decide(TOOL, ScopeClass.READ, both).allow == ""  # a read needs no allow
    assert decide(TOOL, ScopeClass.WRITE, Policy()).allow == ""


def test_destructive_never_allowed_by_policy():
    policy = Policy(allow_write=True, allows=frozenset({TOOL}))
    assert decide(TOOL, ScopeClass.DESTRUCTIVE, policy).action == "ask"


def test_no_human_means_deny():
    write = decide(TOOL, ScopeClass.WRITE, Policy(interactive=False))
    assert (write.action, write.reason) == ("deny", "no human present")
    destructive = decide(TOOL, ScopeClass.DESTRUCTIVE, Policy(interactive=False))
    assert destructive.action == "deny"
    assert decide(TOOL, ScopeClass.READ, Policy(interactive=False)).action == "allow"


def test_policy_json_roundtrip():
    policy = Policy(allow_write=True, allows=frozenset({TOOL}), interactive=False)
    assert Policy.from_json(policy.to_json()) == policy
