"""Clarity: one run through the app, its receipt verified on this machine by the
app's verify route and by the core's own verifier, within budget."""
from .. import fake_engine as fe

AXIS = "clarity"
NEEDS = ("desks",)          # the app's run path is a terminal desk until the no-tmux path lands
BUDGET = {"run done": 10_000}
SCENARIOS = {"ask": [fe.answer("Forty-two.")]}


def run(s):
    s.set_model("ask")
    J = "ask_receipt"

    def start():
        status, body = s.api("POST", "/api/run", {"task": "what is six times seven?", "engine": "openai",
                                                   "mode": "read-only", "cwd": str(s.job), "label": "ask"})
        assert status == 200, f"/api/run {status}: {body.get('error')}"
        assert body.get("name") == "run-ask", f"desk {body.get('name')!r}"
        return body["name"]
    desk = s.step(J, "start run", start)
    if not desk:
        return

    row = s.step(J, "run done", lambda: s.wait_run(desk), budget_ms=BUDGET["run done"])
    if not row:
        return
    run_id = row.get("run_id")

    def view():
        status, body = s.api("GET", f"/api/mb/run/{run_id}")
        assert status == 200, f"/api/mb/run {status}"
        assert body.get("receipt_state"), "no receipt_state"
        v = body.get("run") or {}
        assert v.get("run_id") == run_id, "the run view is for another run"
        return body
    s.step(J, "run view", view, run_id=run_id)

    def verify_app():
        status, body = s.api("POST", "/api/mb/verify", {"id": run_id})
        assert status == 200, f"/api/mb/verify {status}"
        pc = body.get("pc") or {}
        assert pc.get("valid") is True, f"the app's verifier said {pc!r}"
        assert body.get("index_sha_ok") is True, "the index digest does not match the receipt"
        return {"_report": True, "value": body, "run_id": run_id, "receipt_sha256": body.get("sha256")}
    s.step(J, "verify (app)", verify_app)

    def verify_cli():
        ok, sha = s.verify_cli(s.receipt_path(run_id))
        assert ok, "metabot receipts verify did not say VALID"
        return {"_report": True, "value": sha, "run_id": run_id, "receipt_sha256": sha}
    s.step(J, "verify (core)", verify_cli)
