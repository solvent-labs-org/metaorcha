import json

from fastapi.testclient import TestClient
from playground.main import app
from playground.verify import REPO_ROOT

GOLDEN_PATH = (
    REPO_ROOT / "docs" / "spec" / "test-vectors" / "run-attestation-golden.json"
)


def _golden() -> dict:
    return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))


def test_golden_endpoint_serves_vector():
    client = TestClient(app)
    resp = client.get("/golden")
    assert resp.status_code == 200
    body = resp.json()
    assert body["valid"]["format"] == "orcha.run-attestation/v1"
    assert (
        body["tampered"]["steps"][0]["latency_ms"]
        != body["valid"]["steps"][0]["latency_ms"]
    )


def test_verify_golden_valid_is_green():
    client = TestClient(app)
    resp = client.post("/verify", json=_golden()["valid"])
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["verdict"] == "valid"
    assert body["checks"] == {
        "schema": True,
        "steps_root": True,
        "steps_merkle_root": True,
        "signature": True,
    }
    assert body["reason"] == "ok"


def test_verify_golden_tampered_is_red_with_chain_reason():
    client = TestClient(app)
    resp = client.post("/verify", json=_golden()["tampered"])
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["checks"]["steps_root"] is False
    assert body["reason"] == "step hash chain does not match steps_root"


def test_verify_non_object_json():
    client = TestClient(app)
    resp = client.post("/verify", json=[1, 2, 3])
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["reason"] == "envelope must be a JSON object"
