"""Setup: a hosted engine refuses the key (401). The first run fails fast, its receipt still
verifies and records the failure, and the app's lane message says to check the key and
where the setting is, instead of a bare status."""
import json
import time

from .. import fake_engine as fe

AXIS = "setup"
NEEDS = ()
BUDGET = {"the run fails fast": 5_000}
SCENARIOS = {"wrong_key": [fe.error(401)]}
HINT = "Check this engine's key in Settings → Accounts."
MESSAGE_WAIT_S = 20


def run(s):
    J = "wrong_key"

    def fails_fast():
        r = s.core(["ask", "-e", "openai", "-m", "wrong_key", "--read-only", "--cwd", str(s.job),
                    "--json", "-q", "hello"], timeout=60)
        d = json.loads(r.stdout or "{}")
        assert d.get("ok") is False, f"ok={d.get('ok')}"
        assert "401" in (d.get("error") or ""), f"the error does not name the 401: {d.get('error')!r}"
        valid, sha = s.verify_cli(s.receipt_path(d["run_id"]))
        assert valid, "the receipt of the refused run must still verify"
        return {"_report": True, "value": {"error": d.get("error")}, "run_id": d.get("run_id"), "receipt_sha256": sha}
    s.step(J, "the run fails fast", fails_fast, budget_ms=BUDGET["the run fails fast"])

    s.set_model("wrong_key")

    def app_says():
        status, body = s.api("POST", "/api/run", {"task": "hello", "engine": "openai", "mode": "read-only",
                                                   "cwd": str(s.job), "label": "wk"})
        assert status == 200, f"/api/run {status}: {body.get('error')}"
        desk = body["name"]
        row = s.wait_run(desk)
        deadline = time.monotonic() + MESSAGE_WAIT_S
        said = []
        while time.monotonic() < deadline:
            status, h = s.api("GET", "/api/history")
            said = [m.get("message") or "" for m in (h.get("messages") or []) if status == 200
                    and m.get("target") == desk and (m.get("message") or "").startswith("The run failed")]
            if said:
                break
            time.sleep(0.5)
        assert said, f"the lane never heard how the run ended ({MESSAGE_WAIT_S} s)"
        assert "401" in said[-1], f"the message does not name the 401: {said[-1]!r}"
        assert said[-1].endswith(HINT), f"no hint where to fix the key: {said[-1]!r}"
        return {"_report": True, "value": {"message": said[-1]}, "run_id": row.get("run_id")}
    s.step(J, "the app says to check the key", app_says)
    s.set_model("ask")
