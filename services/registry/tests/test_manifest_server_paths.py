"""The test manifest server serves and stores only bare ``*.yaml`` fixture names.

It listens on all interfaces and takes every filename from the request, so a
``../`` name must never read or write a file outside its fixtures directory.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from services.registry.tests import manifest_server

MANIFEST = "identity:\n  name: Probe\n"


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    (fixtures / "probe.yaml").write_text(MANIFEST)
    (tmp_path / "outside.yaml").write_text("leaked: true\n")
    monkeypatch.setattr(manifest_server, "FIXTURES_DIR", fixtures)
    return TestClient(manifest_server.app)


def test_a_fixture_name_is_served(client: TestClient) -> None:
    resp = client.get("/manifest", params={"filename": "probe.yaml"})
    assert resp.status_code == 200
    assert resp.json() == {"identity": {"name": "Probe"}}


@pytest.mark.parametrize(
    "name",
    ["../outside.yaml", "sub/../../outside.yaml", "/etc/hostname", ".", ".."],
)
def test_a_name_outside_the_fixtures_is_not_read(client: TestClient, name: str) -> None:
    resp = client.get("/manifest", params={"filename": name})
    assert resp.status_code == 404
    assert "leaked" not in resp.text


def test_an_upload_cannot_write_outside_the_fixtures(
    client: TestClient, tmp_path: Path
) -> None:
    resp = client.post(
        "/upload",
        files={"file": ("m.yaml", MANIFEST.encode())},
        data={"filename": "../escaped.yaml"},
    )
    assert resp.status_code == 400
    assert not (tmp_path / "escaped.yaml").exists()


def test_an_upload_with_a_bare_name_lands_in_the_fixtures(
    client: TestClient, tmp_path: Path
) -> None:
    resp = client.post(
        "/upload",
        files={"file": ("m.yaml", MANIFEST.encode())},
        data={"filename": "new.yaml"},
    )
    assert resp.status_code == 200
    assert (tmp_path / "fixtures" / "new.yaml").read_text() == MANIFEST
