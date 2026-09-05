import asyncio

from emerge.run_attestation import verify_run_attestation
from fastapi.testclient import TestClient
from playground.main import app, sse_line
from playground.sim_runner import (
    SIM_SIGNER_DID,
    build_sim_envelope,
    chain_events,
    run_sim,
)
from playground.state import run_store
from validator.run_envelope import compute_steps_root


def test_sim_envelope_verifies_via_sdk():
    envelope = build_sim_envelope("summarize the attestation spec")
    assert envelope["signer"]["did"] == SIM_SIGNER_DID
    assert envelope["format"] == "orcha.run-attestation/v1"
    assert compute_steps_root(envelope["steps"]) == envelope["steps_root"]
    verdict = verify_run_attestation(envelope)
    assert verdict.valid, verdict.checks


def test_sim_envelope_uses_provided_run_id():
    assert build_sim_envelope("x", run_id="sim-fixed1")["run_id"] == "sim-fixed1"


def test_sim_envelope_tamper_fails():
    envelope = build_sim_envelope("tamper target")
    envelope["steps"][0]["latency_ms"] += 1
    verdict = verify_run_attestation(envelope)
    assert not verdict.valid
    assert verdict.checks["steps_root"] is False


def test_chain_events_match_envelope_chain():
    envelope = build_sim_envelope("chain events")
    events = chain_events(envelope)
    assert [e["step_index"] for e in events] == list(range(len(envelope["steps"])))
    assert events[-1]["chain_hash"] == envelope["steps_root"]


def test_sse_line_format():
    assert sse_line({"type": "step", "i": 1}) == 'data: {"type":"step","i":1}\n\n'


def test_run_sim_endpoints_flow():
    app.state.step_delay = 0.0
    client = TestClient(app)
    resp = client.post("/runs", json={"prompt": "demo run", "mode": "sim"})
    assert resp.status_code == 200
    run_id = resp.json()["run_id"]
    assert resp.json()["mode"] == "sim"

    envelope = None
    for _ in range(100):
        r = client.get(f"/runs/{run_id}/envelope")
        if r.status_code == 200:
            envelope = r.json()
            break
        assert r.status_code == 409
    assert envelope is not None
    assert verify_run_attestation(envelope).valid
    assert run_store.get(run_id)["status"] == "done"


def test_unknown_run_404():
    client = TestClient(app)
    assert client.get("/runs/nope/envelope").status_code == 404


def test_run_sim_failed_marks_store(monkeypatch):
    def boom(prompt: str, **_kwargs) -> dict:
        raise RuntimeError("sim exploded")

    monkeypatch.setattr("playground.sim_runner.build_sim_envelope", boom)
    run_store.create("sim-fail-1", "sim")
    asyncio.run(run_sim("sim-fail-1", "x", step_delay=0.0))
    run = run_store.get("sim-fail-1")
    assert run["status"] == "failed"
    assert run["error"] == "sim exploded"


def test_real_mode_409_when_superagent_unavailable():
    app.state.mode = "real"
    app.state.real_available = False
    try:
        client = TestClient(app)
        resp = client.post("/runs", json={"prompt": "x", "mode": "real"})
        assert resp.status_code == 409
        assert "mode=sim" in resp.json()["detail"]
    finally:
        app.state.mode = "sim"
        app.state.real_available = False
