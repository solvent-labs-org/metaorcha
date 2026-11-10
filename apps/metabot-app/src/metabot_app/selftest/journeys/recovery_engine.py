"""Recovery: the model server fails, refuses, cuts off or dawdles. Every run ends,
says plainly why, and its receipt still verifies and records the failure (ok false;
lost true when the cost is unknown). Nothing hangs: the slow one ends in budget."""
import json

from .. import fake_engine as fe

AXIS = "recovery"
NEEDS = ("desks",)
BUDGET = {"slow": 10_000}
SCENARIOS = {"error_500": [fe.error(500)], "truncate": [fe.truncated()],
             "refuse": [fe.refused("wrong key")], "slow": [fe.answer("late", delay_ms=2000)]}
CASES = (("error_500", False, True, "500"), ("truncate", False, True, "openai:"),
         ("refuse", False, False, "400"), ("slow", True, False, ""))


def run(s):
    J = "recovery_engine"
    for scenario, ok, lost, said in CASES:
        def case(scenario=scenario, ok=ok, lost=lost, said=said):
            r = s.core(["ask", "-e", "openai", "-m", scenario, "--read-only", "--cwd", str(s.job),
                        "--json", "-q", "hello"], timeout=60)
            d = json.loads(r.stdout or "{}")
            assert d.get("ok") is ok, f"ok={d.get('ok')} error={d.get('error')!r}"
            assert d.get("lost") is lost, f"lost={d.get('lost')}"
            if said:
                assert said in (d.get("error") or ""), f"the error does not say {said!r}: {d.get('error')!r}"
            rp = s.receipt_path(d["run_id"])
            valid, sha = s.verify_cli(rp)
            assert valid, "the receipt of a failed run must still verify"
            return {"_report": True, "value": d, "run_id": d.get("run_id"), "receipt_sha256": sha}
        s.step(J, scenario, case, budget_ms=BUDGET.get(scenario))

    # through the app: a failing engine run still lands in the index as done, with its receipt
    s.set_model("error_500")

    def app_fail():
        status, body = s.api("POST", "/api/run", {"task": "hello", "engine": "openai", "mode": "read-only",
                                                   "cwd": str(s.job), "label": "err"})
        assert status == 200, f"/api/run {status}: {body.get('error')}"
        row = s.wait_run(body["name"])
        run_id = row["run_id"]
        status, view = s.api("GET", f"/api/mb/run/{run_id}")
        assert status == 200, f"/api/mb/run {status}"
        v = view.get("run") or {}
        assert v.get("status") != "ok", f"a failed run shows status {v.get('status')!r}"
        ok, sha = s.verify_cli(s.receipt_path(run_id))
        assert ok, "the receipt does not verify"
        return {"_report": True, "value": v, "run_id": run_id, "receipt_sha256": sha}
    s.step(J, "app run on a failing engine", app_fail)
    s.set_model("ask")
