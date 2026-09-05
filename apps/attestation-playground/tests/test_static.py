from fastapi.testclient import TestClient
from playground.main import app


def test_index_served_at_root():
    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Attestation Playground" in resp.text


def test_static_assets_served():
    client = TestClient(app)
    assert client.get("/app.js").status_code == 200
    assert client.get("/style.css").status_code == 200


def test_api_routes_still_win_over_static():
    client = TestClient(app)
    assert client.get("/health").json() == {"status": "ok"}
