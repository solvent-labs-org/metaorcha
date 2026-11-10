"""``metabot workflow approve``, ``revoke`` and ``run``: an approval is a signed receipt over a
workflow's bytes, and a run goes only under one in force for the bytes as they are now."""

from __future__ import annotations

import base64
import io
import json
import os
import re
from pathlib import Path

import pytest

from fake_chat import answer
from metabot import office_run, workflow_run
from metabot.approvals import DenyAll
from metabot.cli import main
from metabot.config import Config, save_config
from metabot.paths import kept_receipts
from metabot.receipts import ensure_key, seal_and_store, verify_file
from metabot.seals import seal_state
from metabot.workflow_approval import (
    APPROVE_CHECK,
    REVOKE_CHECK,
    NotApproved,
    approval_for_run,
    approval_state,
)
from metabot.workflows import FORMAT, load_workflow, resolve, store_workflow
from test_office_run import DeskEngine, _repo


class Terminal(io.StringIO):
    """stdin as a terminal a person types into."""

    def isatty(self) -> bool:
        return True


@pytest.fixture(autouse=True)
def _nobody_answers_the_gate(monkeypatch):
    monkeypatch.setattr(workflow_run, "pick_approver", DenyAll)


def _cli(capsys, *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def _typed(monkeypatch, text: str) -> None:
    monkeypatch.setattr("sys.stdin", Terminal(text))


def _keep(tmp_path, name: str = "date-fix", steps: list | None = None, **top) -> str:
    data = {
        "format": FORMAT,
        "name": name,
        "cwd": str(tmp_path),
        "steps": steps or [{"name": "say", "kind": "ask", "prompt": "Say hi.", "engine": "openai"}],
        **top,
    }
    return store_workflow(json.dumps(data).encode()).sha256


def _approve(capsys, monkeypatch, ref: str = "date-fix") -> str:
    _typed(monkeypatch, "y\n")
    code, out, err = _cli(capsys, "workflow", "approve", ref)
    assert code == 0, err
    return out


def _receipt(run_id: str) -> dict:
    return json.loads((kept_receipts() / f"{run_id}.json").read_text())


def _verdict(envelope: dict, check: str) -> dict:
    [found] = [v for v in envelope["verdicts"] if v["check"] == check]
    return found


# ── approve ─────────────────────────────────────────────────────────────────


def test_approve_shows_the_facts_asks_and_signs_the_sha256(tmp_path, capsys, monkeypatch):
    sha = _keep(tmp_path)
    out = _approve(capsys, monkeypatch)
    assert "say (ask): openai" in out and "approve date-fix?" in out
    [approval] = approval_state(sha).in_force
    assert verify_file(approval.receipt).valid
    envelope = _receipt(approval.run_id)
    verdict = _verdict(envelope, APPROVE_CHECK)
    assert verdict["result"] == "pass"
    assert verdict["detail"] == f"sha256={sha};name=date-fix;surface=cli"
    assert f"approved date-fix {sha[:12]}" in out and "next: metabot workflow run date-fix" in out
    code, out, _err = _cli(capsys, "receipts", "verify", approval.run_id)
    assert code == 0 and out.startswith("VALID")


def test_approve_without_a_terminal_is_refused_even_with_yes(tmp_path, capsys, monkeypatch):
    sha = _keep(tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO("y\n"))
    code, _out, err = _cli(capsys, "workflow", "approve", "--yes", "date-fix")
    assert code == 2 and "approve asks you at a terminal" in err
    assert approval_state(sha).in_force == []


def test_approve_answered_no_signs_nothing(tmp_path, capsys, monkeypatch):
    sha = _keep(tmp_path)
    _typed(monkeypatch, "n\n")
    code, out, _err = _cli(capsys, "workflow", "approve", "date-fix")
    assert code == 1 and "not approved" in out
    assert approval_state(sha).in_force == []
    assert not list(kept_receipts().glob("wfa-*")) if kept_receipts().exists() else True


def test_approving_an_approved_workflow_again_signs_nothing_new(tmp_path, capsys, monkeypatch):
    sha = _keep(tmp_path)
    _approve(capsys, monkeypatch)
    out = _approve(capsys, monkeypatch)
    assert "already approved" in out
    assert len(approval_state(sha).in_force) == 1


def test_approve_refuses_bytes_changed_since_they_were_kept(tmp_path, capsys, monkeypatch):
    _keep(tmp_path)
    load_workflow("date-fix").path.write_text("{}")
    _typed(monkeypatch, "y\n")
    code, _out, err = _cli(capsys, "workflow", "approve", "date-fix")
    assert code == 1 and "changed since it was stored" in err


def test_show_and_list_say_whether_each_version_is_approved(tmp_path, capsys, monkeypatch):
    _keep(tmp_path)
    code, out, _err = _cli(capsys, "workflow", "show", "date-fix")
    assert "approval  not approved" in out and "next: metabot workflow approve date-fix" in out
    _approve(capsys, monkeypatch)
    code, out, _err = _cli(capsys, "workflow", "show", "date-fix")
    assert "approval  approved at " in out and "receipt wfa-" in out
    assert "next: metabot workflow run date-fix" in out
    code, out, _err = _cli(capsys, "workflow", "list")
    assert re.search(r"(?<!not) approved ", out)


# ── run ─────────────────────────────────────────────────────────────────────


def test_an_unapproved_workflow_never_runs(tmp_path, capsys, chat):
    fake = chat([answer("hi")])
    save_config(Config(openai_base_url=fake.url))
    _keep(tmp_path)
    code, _out, err = _cli(capsys, "workflow", "run", "date-fix")
    assert code == 1 and "workflow not run: it is not approved" in err
    assert fake.requests == []


def test_an_approved_workflow_runs_and_one_seal_signs_its_steps(
    tmp_path, capsys, monkeypatch, chat
):
    fake = chat([answer("hi"), answer("bye")])
    save_config(Config(openai_base_url=fake.url))
    steps = [
        {"name": "one", "kind": "ask", "prompt": "Say hi.", "engine": "openai"},
        {"name": "two", "kind": "ask", "prompt": "Say bye.", "engine": "openai"},
    ]
    sha = _keep(tmp_path, steps=steps)
    _approve(capsys, monkeypatch)
    [approval] = approval_state(sha).in_force
    code, out, err = _cli(capsys, "workflow", "run", "date-fix")
    assert code == 0, err
    assert [r["messages"][-1]["content"] for r in fake.requests] == ["Say hi.", "Say bye."]
    assert "answer of two:\nbye" in out
    run_id = next(w for w in out.split() if w.startswith("mb-"))
    seal = kept_receipts() / f"{run_id}.json"
    assert seal_state(seal, run_id) == "valid, 2 members"
    envelope = _receipt(run_id)
    signed = _verdict(envelope, "workflow_run")["detail"]
    digest = __import__("hashlib").sha256(approval.receipt.read_bytes()).hexdigest()
    assert signed == (
        f"sha256={sha};approval={approval.run_id};approval_receipt={digest};steps=2;ran=2;"
        "spent_musd=0"
    )
    assert _verdict(envelope, "seal")["result"] == "pass"
    assert [s["tool"] for s in envelope["steps"]] == ["metabot.member.step"] * 2
    code, out, _err = _cli(capsys, "receipts", "verify", run_id)
    assert code == 0 and out.startswith("VALID") and "each present, unchanged, and valid" in out


def test_bytes_changed_after_approval_never_run(tmp_path, capsys, monkeypatch, chat):
    fake = chat([answer("hi")])
    save_config(Config(openai_base_url=fake.url))
    _keep(tmp_path)
    _approve(capsys, monkeypatch)
    kept = load_workflow("date-fix").path
    kept.write_bytes(kept.read_bytes().replace(b"Say hi.", b"Say rm."))
    code, _out, err = _cli(capsys, "workflow", "run", "date-fix")
    assert code == 1 and "changed since it was stored" in err
    assert fake.requests == []


def test_an_approval_of_one_sha256_never_runs_another(tmp_path, capsys, monkeypatch, chat):
    fake = chat([answer("hi")])
    save_config(Config(openai_base_url=fake.url))
    a = _keep(tmp_path)
    _approve(capsys, monkeypatch)
    b = _keep(tmp_path, about="the same name, other bytes")
    assert resolve("date-fix") == b
    code, _out, err = _cli(capsys, "workflow", "run", "date-fix")
    assert code == 1 and "it is not approved" in err and fake.requests == []
    with pytest.raises(NotApproved):
        approval_for_run(load_workflow(b))
    code, _out, err = _cli(capsys, "workflow", "run", a[:12])  # the approved one, by its sha256
    assert code == 0, err
    assert len(fake.requests) == 1


def test_a_revoked_approval_never_runs_and_approving_again_does(
    tmp_path, capsys, monkeypatch, chat
):
    fake = chat([answer("hi")])
    save_config(Config(openai_base_url=fake.url))
    sha = _keep(tmp_path)
    _approve(capsys, monkeypatch)
    [approval] = approval_state(sha).in_force
    monkeypatch.setattr("sys.stdin", io.StringIO(""))  # not a terminal: revoke takes --yes
    code, _out, err = _cli(capsys, "workflow", "revoke", "date-fix")
    assert code == 2 and "pass --yes" in err
    code, out, err = _cli(capsys, "workflow", "revoke", "--yes", "date-fix")
    assert code == 0, err
    state = approval_state(sha)
    assert state.in_force == [] and [a.run_id for a in state.revoked] == [approval.run_id]
    revoke_id = next(w for w in out.split() if w.startswith("wfr-"))
    verdict = _verdict(_receipt(revoke_id), REVOKE_CHECK)
    assert verdict["detail"] == f"sha256={sha};approval={approval.run_id};surface=cli"
    assert verify_file(kept_receipts() / f"{revoke_id}.json").valid
    code, _out, err = _cli(capsys, "workflow", "run", "date-fix")
    assert code == 1 and "its approval was revoked" in err and fake.requests == []
    _approve(capsys, monkeypatch)
    assert _cli(capsys, "workflow", "run", "date-fix")[0] == 0
    assert len(fake.requests) == 1


def test_revoke_by_name_revokes_every_version_of_it(tmp_path, capsys, monkeypatch):
    a = _keep(tmp_path)
    _approve(capsys, monkeypatch)
    b = _keep(tmp_path, about="v2")
    _approve(capsys, monkeypatch)
    _typed(monkeypatch, "y\n")
    assert _cli(capsys, "workflow", "revoke", "date-fix")[0] == 0
    assert approval_state(a).in_force == [] and approval_state(b).in_force == []


def test_an_approval_receipt_changed_by_hand_counts_for_nothing(tmp_path, capsys, monkeypatch):
    sha = _keep(tmp_path)
    _approve(capsys, monkeypatch)
    other = _keep(tmp_path, about="another")
    [approval] = approval_state(sha).in_force
    text = approval.receipt.read_text().replace(sha, other)  # now it says the other one
    approval.receipt.write_text(text)
    assert approval_state(sha).in_force == [] and approval_state(other).in_force == []
    with pytest.raises(NotApproved):
        approval_for_run(load_workflow(other))


def test_an_approval_signed_with_another_key_is_not_this_machine_s(tmp_path, capsys, monkeypatch):
    sha = _keep(tmp_path)
    _approve(capsys, monkeypatch)
    monkeypatch.setenv("ORCHA_KEY_PATH", str(tmp_path / "another-key"))
    ensure_key()  # this home now signs with another key, which signed no approval
    with pytest.raises(NotApproved, match="not approved"):
        approval_for_run(load_workflow(sha))


def test_an_approval_another_key_signed_under_this_home_s_did_is_not_this_machine_s(
    tmp_path, monkeypatch
):
    from emerge.run_attestation import compute_envelope_digest, verify_run_attestation

    from metabot.workflow_approval import approve

    sha = _keep(tmp_path)
    _key, our_did = ensure_key()
    ours = os.environ["ORCHA_KEY_PATH"]
    monkeypatch.setenv("ORCHA_KEY_PATH", str(tmp_path / "another-key"))
    planted = approve(load_workflow(sha))
    other, _other_did = ensure_key()
    monkeypatch.setenv("ORCHA_KEY_PATH", ours)
    envelope = json.loads(planted.receipt.read_text())
    envelope["signer"]["did"], envelope["agent_dids"] = our_did, [our_did]  # a DID is a name
    digest = compute_envelope_digest(envelope).encode()
    envelope["signature"] = base64.b64encode(other.sign(digest)).decode()
    assert verify_run_attestation(envelope).valid  # it verifies, under the other key
    planted.receipt.write_text(json.dumps(envelope, indent=2) + "\n")
    assert approval_state(sha).in_force == []
    with pytest.raises(NotApproved, match="not approved"):
        approval_for_run(load_workflow(sha))


def test_an_approval_kept_under_one_sha256_but_signing_another_approves_neither(tmp_path):
    a = _keep(tmp_path)
    b = _keep(tmp_path, about="another")
    key, _did = ensure_key()
    now = "2026-11-04T00:00:00Z"
    seal_and_store(
        run_id=f"wfa-{b[:16]}-20261104T000000Z-000000",  # filed as b's, signing a
        steps=[{"call_id": "approve", "tool": "metabot.workflow.approve",
                "args": {"sha256": a}, "output": {"approved": True}, "success": True}],
        verdicts=[{"check": APPROVE_CHECK, "result": "pass",
                   "detail": f"sha256={a};name=date-fix;surface=cli"}],
        started_at=now, finished_at=now, key=key, index=False,
    )  # fmt: skip
    assert approval_state(a).in_force == [] and approval_state(b).in_force == []


def test_a_revoke_receipt_that_does_not_verify_stops_the_run(tmp_path, capsys, monkeypatch):
    sha = _keep(tmp_path)
    _approve(capsys, monkeypatch)
    (kept_receipts() / f"wfr-{sha[:16]}-x.json").write_text("{}")
    with pytest.raises(NotApproved, match="does not verify"):
        approval_for_run(load_workflow(sha))


def test_a_kept_file_that_is_no_workflow_never_runs(tmp_path, home, capsys):
    raw = b'{"format": "metabot.workflow/1", "name": "odd"}'
    sha = __import__("hashlib").sha256(raw).hexdigest()
    folder = home / "workflows"
    folder.mkdir(parents=True)
    (folder / f"{sha}.json").write_bytes(raw)
    (folder / "index.json").write_text(json.dumps({sha: {"name": "odd", "seq": 1}}))
    code, _out, err = _cli(capsys, "workflow", "run", "odd")
    assert code == 1 and "workflow not run: cwd: an absolute path" in err


def test_a_workflow_that_cannot_run_here_runs_no_step(tmp_path, capsys, monkeypatch, chat):
    fake = chat([answer("hi")])
    save_config(Config(openai_base_url=fake.url))
    steps = [
        {"name": "one", "kind": "ask", "prompt": "Say hi.", "engine": "openai"},
        {"name": "two", "kind": "ask", "prompt": "Go.", "engine": "claude", "login": "work"},
    ]
    _keep(tmp_path, steps=steps)
    _approve(capsys, monkeypatch)
    code, _out, err = _cli(capsys, "workflow", "run", "date-fix")
    assert code == 1 and "cannot run here: step two:" in err and "'work'" in err
    assert fake.requests == []


def test_a_step_that_fails_stops_the_workflow_and_the_seal_says_so(
    tmp_path, capsys, monkeypatch, chat
):
    fake = chat([{"__status__": 500, "error": {"message": "down"}}, answer("never")])
    save_config(Config(openai_base_url=fake.url))
    steps = [
        {"name": "one", "kind": "ask", "prompt": "Say hi.", "engine": "openai"},
        {"name": "two", "kind": "ask", "prompt": "Say bye.", "engine": "openai"},
    ]
    sha = _keep(tmp_path, steps=steps)
    _approve(capsys, monkeypatch)
    code, out, _err = _cli(capsys, "workflow", "run", "date-fix")
    assert code == 1
    assert len(fake.requests) == 1  # the second step never started
    assert re.search(r"^  two +ask +not run$", out, re.M)
    run_id = next(w for w in out.split() if w.startswith("mb-"))
    envelope = _receipt(run_id)
    assert _verdict(envelope, "seal")["result"] == "fail"
    detail = _verdict(envelope, "workflow_run")["detail"]
    assert f"sha256={sha};" in detail and ";steps=2;ran=1;" in detail and "stopped=one" in detail
    assert seal_state(kept_receipts() / f"{run_id}.json", run_id) == "valid, 1 members"


def test_a_ladder_step_runs_as_a_ladder_and_its_seal_is_a_member(
    tmp_path, capsys, monkeypatch, chat
):
    fake = chat([answer("four")])
    save_config(Config(openai_base_url=fake.url, ladder=[{"engine": "openai", "model": "tiny"}]))
    steps = [{"name": "climb", "kind": "ladder", "prompt": "2+2?"}]
    _keep(tmp_path, steps=steps)
    _approve(capsys, monkeypatch)
    code, out, err = _cli(capsys, "workflow", "run", "date-fix")
    assert code == 0, err
    assert "answer of climb:\nfour" in out
    run_id = next(w for w in out.split() if w.startswith("mb-") and "." not in w)
    [member] = _receipt(run_id)["steps"]
    assert _verdict(_receipt(member["call_id"]), "seal")["detail"].startswith("kind=ladder")
    assert fake.requests[0]["model"] == "tiny"


def test_an_office_step_runs_the_office_it_pinned(tmp_path, capsys, monkeypatch):
    engines = {
        "claude": DeskEngine("claude", act=lambda job: "PLAN: write it"),
        "codex": DeskEngine("codex", act=lambda job: "BUILT"),
        "cursor": DeskEngine("cursor", act=lambda job: "REVIEW: fine"),
    }
    monkeypatch.setattr(office_run, "get_engine", lambda name: engines[name])
    root = _repo(tmp_path)
    code, _out, err = _cli(
        capsys, "workflow", "new", "ship", "--office", str(root), "--cwd", str(root), "Ship it."
    )
    assert code == 0, err
    _approve(capsys, monkeypatch, "ship")
    code, out, err = _cli(capsys, "workflow", "run", "ship")
    assert code == 0, err
    assert "answer of office:\nREVIEW: fine" in out
    assert [len(e.jobs) for e in engines.values()] == [1, 1, 1]
    run_id = next(w for w in out.split() if w.startswith("mb-") and "." not in w)
    [member] = _receipt(run_id)["steps"]
    assert _verdict(_receipt(member["call_id"]), "seal")["detail"].startswith("kind=office")
    (root / "office.yaml").write_text(
        (root / "office.yaml").read_text() + "# changed\n", encoding="utf-8"
    )
    code, _out, err = _cli(capsys, "workflow", "run", "ship")
    assert code == 1 and "office.yaml has changed since the workflow named it" in err
    assert [len(e.jobs) for e in engines.values()] == [1, 1, 1]


def test_run_takes_a_name_or_a_sha256_and_names_one_it_cannot_find(capsys):
    code, _out, err = _cli(capsys, "workflow", "run", "nothing")
    assert code == 2 and "no workflow named 'nothing'" in err


def test_the_approval_never_reaches_the_run_index(tmp_path, capsys, monkeypatch):
    _keep(tmp_path)
    _approve(capsys, monkeypatch)
    index = Path(kept_receipts().parent / "runs.jsonl")
    assert not index.exists() or "wfa-" not in index.read_text()
