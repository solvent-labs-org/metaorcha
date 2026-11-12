"""Clarity: what a run did is readable and checkable. Every step row of the run view names
its tool, its time and its verdict (the step passed; every run verdict passes); the run's signed start and finish are shown; the verify
route says the receipt is valid; a tampered copy is reported invalid; the receipt bytes the
app serves are the file on disk."""
import json

from .. import fake_engine as fe

AXIS = "clarity"
NEEDS = ()
BUDGET = {}
SCENARIOS = {"clarity": [fe.call("list_dir", {"path": "."}),
                         fe.call("read_file", {"path": "c.txt"}),
                         fe.answer("c.txt says: clear as day.")]}


def tampered(receipt_text: str) -> str:
    """The same receipt with one step's recorded output changed: a copy that must not verify."""
    env = json.loads(receipt_text)
    steps = env.get("steps") or []
    assert steps, "the receipt has no steps to tamper with"
    steps[0]["output"] = str(steps[0].get("output", "")) + " (edited)"
    return json.dumps(env, indent=2) + "\n"


def run(s):
    J = "receipt_view_clarity"
    s.set_model("clarity")
    (s.job / "c.txt").write_text("clear as day\n")
    box = {}

    def a_run():   # read-only, as the app runs by default: every call gets a scope verdict
        status, body = s.api("POST", "/api/run", {"task": "what does c.txt say?", "engine": "openai",
                                                   "mode": "read-only", "cwd": str(s.job), "label": "cl"})
        assert status == 200, f"/api/run {status}: {body.get('error')}"
        row = s.wait_run(body["name"])
        box["run_id"] = row["run_id"]
        return {"_report": True, "value": {"run_id": row["run_id"]}, "run_id": row["run_id"]}
    if not s.step(J, "a run with tool steps", a_run):
        s.set_model("ask")
        return
    rid = box["run_id"]

    def rows():
        status, body = s.api("GET", f"/api/mb/run/{rid}")
        assert status == 200, f"/api/mb/run {status}"
        v = body.get("run") or {}
        steps = v.get("steps") or []
        assert len(steps) == 2, f"{len(steps)} step rows, not 2"
        verdict_of = {str(x.get("check")): x.get("result") for x in v.get("verdicts") or []}
        for st in steps:
            assert st.get("tool"), f"a step row without a tool: {st}"
            assert isinstance(st.get("latency_ms"), int), f"a step row without its time: {st}"
            # a step's verdict is its own pass; a scoped run (a scope file) adds scope_allow per call
            assert st.get("success") is True, f"a step row without a pass: {st}"
            scoped = verdict_of.get(f"scope_allow:{st.get('call_id')}")
            assert scoped in (None, "pass"), f"the scope verdict for {st.get('call_id')} is {scoped!r}"
            assert st.get("approval") is not None, f"a step row without its approval column: {st}"
        assert all(r == "pass" for r in verdict_of.values()), f"a failing verdict: {verdict_of}"
        assert v.get("started_at") and v.get("finished_at"), "the run view shows no start and finish"
        assert v.get("state", "").startswith("valid"), f"state {v.get('state')!r}"
        return {"_report": True, "value": {"tools": [st["tool"] for st in steps], "state": v.get("state")}, "run_id": rid}
    s.step(J, "every step row shows tool, time and verdict", rows)

    def valid():
        status, body = s.api("POST", "/api/mb/verify", {"id": rid})
        assert status == 200, f"/api/mb/verify {status}"
        assert (body.get("pc") or {}).get("valid") is True, f"the verifier did not say valid: {body.get('pc')}"
        assert body.get("index_sha_ok") is True, "the index row's digest is not the file's"
        return {"_report": True, "value": {"sha256": body.get("sha256")}, "run_id": rid, "receipt_sha256": body.get("sha256")}
    s.step(J, "the verify route says valid", valid)

    def served_bytes():
        status, raw = s.api_raw("GET", f"/api/mb/receipt/{rid}")
        assert status == 200, f"/api/mb/receipt {status}"
        assert raw == s.receipt_path(rid).read_bytes(), "the served receipt is not the file on disk"
        return {"_report": True, "value": {"bytes": len(raw)}, "run_id": rid}
    s.step(J, "the served receipt is the file on disk", served_bytes)

    def invalid_copy():
        copy = tampered(s.receipt_path(rid).read_text(encoding="utf-8"))
        status, body = s.api("POST", "/api/sdk/verify", {"raw": copy})
        assert status == 200, f"/api/sdk/verify {status}"
        pc = body.get("pc") or {}
        assert pc.get("valid") is False, f"a tampered copy was not reported invalid: {pc}"
        return {"_report": True, "value": {"valid": pc.get("valid")}, "run_id": rid}
    s.step(J, "a tampered copy is reported invalid", invalid_copy)
    s.set_model("ask")
