"""The web UI's guard: a per-start token, this machine's address only, no cross-site writes."""

from __future__ import annotations

import os
import re
import select
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx2
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route, WebSocketRoute
from starlette.testclient import TestClient
from starlette.websockets import WebSocket, WebSocketDisconnect

from metabot.cli import main
from metabot.web_app import make_app
from metabot.web_guard import COOKIE, Guard, checked_form, guarded

PORT = 47001
TOKEN = "t" * 43
BASE = f"http://127.0.0.1:{PORT}"


def _guard() -> Guard:
    return Guard(TOKEN, PORT)


def _client(app, **kw) -> TestClient:
    return TestClient(app, base_url=BASE, follow_redirects=False, **kw)


def _app(tmp_path: Path, guard: Guard | None = None):
    return make_app(guard or _guard(), start_dir=tmp_path)


def _signed_in(app) -> TestClient:
    client = _client(app)
    assert client.get(f"/?token={TOKEN}").status_code == 303
    return client


def test_no_token_is_refused(tmp_path, home):
    response = _client(_app(tmp_path)).get("/")
    assert response.status_code == 401
    assert "metabot ui" in response.text and TOKEN not in response.text


def test_a_wrong_token_is_refused(tmp_path, home):
    response = _client(_app(tmp_path)).get("/?token=wrong")
    assert response.status_code == 401
    assert "set-cookie" not in response.headers


@pytest.mark.parametrize("almost", [TOKEN[:-1], TOKEN[:8], TOKEN + "x", "T" + TOKEN[1:]])
def test_a_token_that_is_not_whole_is_refused(tmp_path, home, almost):
    app = _app(tmp_path)
    assert _client(app).get(f"/?token={almost}").status_code == 401
    client = _client(app)
    client.cookies.set(COOKIE, almost)
    assert client.get("/").status_code == 401


def test_the_token_sets_a_strict_cookie_and_leaves_the_address(tmp_path, home):
    client = _client(_app(tmp_path))
    response = client.get(f"/runs?token={TOKEN}&x=1")
    assert response.status_code == 303 and response.headers["location"] == "/runs"
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{COOKIE}={TOKEN};")
    parts = {p.strip().lower() for p in cookie.split(";")[1:]}
    assert {"httponly", "path=/", "samesite=strict"} <= parts
    assert client.get("/").status_code == 200


def test_the_way_back_never_leaves_this_address(tmp_path, home):
    response = _client(_app(tmp_path)).get(f"{BASE}//evil.test/x?token={TOKEN}")
    assert response.status_code == 303 and response.headers["location"] == "/evil.test/x"


def test_the_way_back_cannot_break_a_header(tmp_path, home):
    response = _client(_app(tmp_path)).get(f"{BASE}/a%0d%0aSet-Cookie:%20x=1?token={TOKEN}")
    assert response.status_code == 303
    assert response.headers["location"] == "/a%0D%0ASet-Cookie%3A%20x%3D1"
    assert response.headers.get_list("set-cookie") == [
        f"{COOKIE}={TOKEN}; HttpOnly; Path=/; SameSite=Strict"
    ]


def test_another_host_is_refused(tmp_path, home):
    client = _signed_in(_app(tmp_path))
    for host in (f"evil.test:{PORT}", f"localhost:{PORT}", "127.0.0.1", f"127.0.0.1:{PORT + 1}"):
        response = client.get("/", headers={"host": host})
        assert response.status_code == 403, host
        assert BASE in response.text


def _poke_app(guard: Guard, calls: list):
    async def poke(request: Request):
        await checked_form(request, guard)
        calls.append(request.method)
        return PlainTextResponse("poked")

    return guarded(Starlette(routes=[Route("/poke", poke, methods=["POST"])]), guard)


def test_a_post_needs_the_origin_and_the_form_token():
    guard, calls = _guard(), []
    client = _client(_poke_app(guard, calls))
    client.cookies.set(COOKIE, TOKEN)
    good = {"csrf": guard.csrf}
    for headers, data in (
        ({}, good),
        ({"origin": "http://evil.test"}, good),
        ({"origin": "null"}, good),
        ({"origin": f"http://localhost:{PORT}"}, good),
        ({"origin": BASE}, {}),
        ({"origin": BASE}, {"csrf": "wrong"}),
    ):
        assert client.post("/poke", headers=headers, data=data).status_code == 403, headers
    assert calls == []
    assert client.post("/poke", headers={"origin": BASE}, data=good).status_code == 200
    assert calls == ["POST"]


def test_a_post_without_the_cookie_is_refused():
    guard, calls = _guard(), []
    response = _client(_poke_app(guard, calls)).post(
        "/poke", headers={"origin": BASE}, data={"csrf": guard.csrf}
    )
    assert response.status_code == 401 and calls == []


def _csp_nonce(response) -> str:
    csp = response.headers["content-security-policy"]
    assert "default-src 'none'" in csp and "frame-ancestors 'none'" in csp
    assert "form-action 'self'" in csp and "base-uri 'none'" in csp
    assert "script-src" not in csp  # no script runs: default-src 'none' covers it
    return re.search(r"style-src 'nonce-([^']+)'", csp).group(1)


def test_every_response_carries_the_headers(tmp_path, home):
    app = _app(tmp_path)
    responses = [
        _client(app).get("/"),  # 401
        _client(app).get("/", headers={"host": "evil.test"}),  # 403
        _signed_in(app).get("/"),  # 200
        _signed_in(app).get("/no-such-page"),  # 404
    ]
    assert [r.status_code for r in responses] == [401, 403, 200, 404]
    nonces = set()
    for response in responses:
        nonces.add(_csp_nonce(response))
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["cache-control"] == "no-store"
    assert len(nonces) == len(responses)


def test_a_page_uses_its_own_nonce(tmp_path, home):
    response = _signed_in(_app(tmp_path)).get("/")
    nonce = _csp_nonce(response)
    assert f'<style nonce="{nonce}">' in response.text
    assert "<script" not in response.text


def test_a_websocket_is_never_let_through():
    async def echo(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_text("in")
        await websocket.close()

    app = guarded(Starlette(routes=[WebSocketRoute("/ws", echo)]), _guard())
    client = _client(app)
    client.cookies.set(COOKIE, TOKEN)
    with pytest.raises(WebSocketDisconnect), client.websocket_connect("/ws") as ws:
        ws.receive_text()


@pytest.mark.parametrize("port", ["-1", "65536"])
def test_a_port_out_of_range_is_refused(port, capsys):
    assert main(["ui", "--port", port]) == 2
    assert "is not a port" in capsys.readouterr().err


def test_a_port_in_use_is_refused(capsys):
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen(1)
        port = taken.getsockname()[1]
        assert main(["ui", "--port", str(port)]) == 2
    assert f"cannot listen on 127.0.0.1:{port}" in capsys.readouterr().err


def test_ui_has_no_host_option():
    with pytest.raises(SystemExit) as exc:
        main(["ui", "--host", "0.0.0.0"])
    assert exc.value.code == 2


def _first_line(proc: subprocess.Popen, timeout: float = 30.0) -> str:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        ready, _, _ = select.select([proc.stdout], [], [], 0.2)
        if ready:
            return proc.stdout.readline().decode("utf-8")
        assert proc.poll() is None, "metabot ui ended before it printed its address"
    raise AssertionError("metabot ui printed no address")


def test_ui_serves_loopback_and_keeps_the_token_off_disk_and_logs(tmp_path, home):
    err_path = tmp_path / "ui.err"
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    with err_path.open("wb") as err:
        proc = subprocess.Popen(
            [sys.executable, "-m", "metabot", "ui"],
            cwd=tmp_path,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=err,
            start_new_session=True,
        )
    try:
        line = _first_line(proc)
        match = re.search(r"http://127\.0\.0\.1:(\d+)/\?token=([A-Za-z0-9_-]{43})", line)
        assert match, line
        port, token = int(match.group(1)), match.group(2)
        with httpx2.Client(base_url=f"http://127.0.0.1:{port}") as client:
            assert client.get("/").status_code == 401
            assert client.get(f"/?token={token}").status_code == 303
            assert client.get("/").status_code == 200
        os.kill(proc.pid, signal.SIGTERM)
        assert proc.wait(timeout=20) == 0
        rest = proc.stdout.read().decode("utf-8", "replace")  # after the address line
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        proc.stdout.close()
    assert token not in rest
    assert token not in err_path.read_text(encoding="utf-8", errors="replace")
    for path in home.rglob("*"):
        if path.is_file():
            assert token.encode() not in path.read_bytes(), path
