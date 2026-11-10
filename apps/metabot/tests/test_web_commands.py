"""``metabot ui``'s server: a stop signal stops it, open event streams and all."""

from __future__ import annotations

import asyncio
import http.client
import signal
import socket
import threading
import time
from urllib.parse import urlencode

import uvicorn

from metabot.web_app import make_ui
from metabot.web_commands import LOOPBACK, _server
from metabot.web_guard import COOKIE, Guard
from test_web_runs import TOKEN, Sleeps

SETTLE_S = 5.0  # a stop that takes longer than this is a stop that waits on the browser


def _listening() -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind((LOOPBACK, 0))
    sock.listen(8)
    return sock


def _start_run(guard: Guard, folder: str) -> str:
    conn = http.client.HTTPConnection(LOOPBACK, guard.port, timeout=SETTLE_S)
    form = {"prompt": "do the task", "engine": "openai", "folder": folder, "csrf": guard.csrf}
    body = urlencode(form)
    conn.request(
        "POST",
        "/chat",
        body=body,
        headers={
            "Host": guard.host,
            "Origin": guard.origin,
            "Cookie": f"{COOKIE}={TOKEN}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    response = conn.getresponse()
    location = response.getheader("location") or ""
    response.read()
    conn.close()
    assert response.status == 303, response.status
    return location.removeprefix("/live/")


def _open_stream(guard: Guard, run_id: str) -> socket.socket:
    """The run's event stream on a raw socket, read up to its first event."""
    sock = socket.create_connection((LOOPBACK, guard.port), timeout=SETTLE_S)
    sock.sendall(
        f"GET /live/{run_id}/events HTTP/1.1\r\nHost: {guard.host}\r\n"
        f"Cookie: {COOKIE}={TOKEN}\r\n\r\n".encode()
    )
    got = b""
    while b"data:" not in got:
        chunk = sock.recv(4096)
        assert chunk, got
        got += chunk
    assert got.startswith(b"HTTP/1.1 200"), got
    return sock


def _rest(sock: socket.socket) -> bytes:
    got = b""
    while True:
        try:
            chunk = sock.recv(4096)
        except TimeoutError:
            return got
        if not chunk:
            return got
        got += chunk


def test_one_stop_signal_ends_an_open_stream_and_stops_the_ui(tmp_path):
    sock = _listening()
    guard = Guard(TOKEN, sock.getsockname()[1])
    app, broker = make_ui(guard, start_dir=tmp_path, make_engine=lambda name: Sleeps())
    server = _server(app, uvicorn, broker.close)
    thread = threading.Thread(target=lambda: asyncio.run(server.serve(sockets=[sock])), daemon=True)
    thread.start()
    stream = None
    try:
        deadline = time.monotonic() + SETTLE_S
        while not server.started:
            assert time.monotonic() < deadline, "the UI did not start"
            time.sleep(0.02)
        run_id = _start_run(guard, str(tmp_path))
        stream = _open_stream(guard, run_id)
        time.sleep(0.1)
        began = time.monotonic()
        server.handle_exit(signal.SIGINT, None)  # one Ctrl-C, with a run's page open
        thread.join(SETTLE_S)
        assert not thread.is_alive(), "one stop signal did not stop the UI"
        assert time.monotonic() - began < SETTLE_S
        tail = _rest(stream)
        assert b"event: end\ndata: {}\n\n" in tail, tail
        assert broker.closed and broker.runs[run_id].state == "stopped"
    finally:
        server.force_exit = server.should_exit = True
        thread.join(SETTLE_S)
        if stream is not None:
            stream.close()
        sock.close()


async def test_closing_the_broker_twice_is_harmless(tmp_path):
    guard = Guard(TOKEN, 1)
    _app, broker = make_ui(guard, start_dir=tmp_path)
    await broker.close()
    await broker.close()  # the server closes it before the lifespan does, once more
    assert broker.closed
