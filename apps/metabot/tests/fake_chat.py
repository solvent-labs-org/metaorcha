"""A stand-in openai-compatible chat server for tests: scripted replies, recorded requests."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def call(name: str, args: dict, call_id: str = "call_1") -> dict:
    fn = {"name": name, "arguments": json.dumps(args)}
    message = {"role": "assistant", "content": "", "tool_calls": [{"id": call_id, "function": fn}]}
    return {"choices": [{"message": message}]}


def answer(text: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": text}}], "usage": {"n": 1}}


class FakeChat:
    def __init__(
        self, script: list[dict] | None = None, repeat: dict | None = None, delay: float = 0.0
    ) -> None:
        self.script = list(script or [])
        self.repeat = repeat
        self.requests: list[dict] = []
        self.headers: list[dict] = []
        self.release = threading.Event()  # a slow server answers early once released
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append(body)
                fake.headers.append(dict(self.headers))
                if delay:
                    fake.release.wait(delay)
                reply = fake.script.pop(0) if fake.script else fake.repeat or answer("?")
                data = json.dumps(reply).encode()
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except OSError:  # the client went away (a cancelled run)
                    pass

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
