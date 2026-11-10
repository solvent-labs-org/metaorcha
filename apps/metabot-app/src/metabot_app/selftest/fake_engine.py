"""The scripted fake engine behind every self-test journey.

An OpenAI-compatible chat server, standard library only, bound to 127.0.0.1 only.
It answers from a **scenario**: a JSON Lines file, one line per model turn. The
request's ``model`` names the scenario (``tools_offline`` reads
``tools_offline.jsonl`` in the scenario folder), and the turn is the number of
assistant messages already in the request, so one engine serves concurrent runs
with no per-run state and a resumed session lands on the right line.

Scenario lines (``delay_ms`` may be added to any of them):

    {"answer": "text", "usage": {"prompt_tokens": 7, "completion_tokens": 3}}
    {"tool_call": {"name": "write_file", "args": {"path": "a.txt", "content": "hi"}}}
    {"tool_calls": [{"name": ..., "args": {...}}, ...]}
    {"refuse": "message"}            -> 400, as a server refusing the request
    {"error": 500}                   -> that status, as a failing server
    {"truncate": true}               -> a valid answer cut off mid-body

Past the last line the engine answers OVER, so a run always ends. An unknown
model is answered by ``echo``: the last user message quoted back, no tools.

    python -m metabot_app.selftest.fake_engine --port 0 --port-file PATH --scenarios DIR
"""
from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ECHO = "echo"
OVER = "The scenario has no more turns; this is its closing answer."
REFUSAL = "response_format is not supported"
USAGE = {"prompt_tokens": 12, "completion_tokens": 4}
BODY_LIMIT = 4 * 1024 * 1024
SCENARIOS = Path(__file__).resolve().parent / "scenarios"


# ── scenario lines, for a runner that writes them ───────────────────────────

def answer(text: str, usage: dict | None = None, delay_ms: int = 0) -> dict:
    line = {"answer": text, "usage": dict(usage or USAGE)}
    return {**line, "delay_ms": delay_ms} if delay_ms else line


def call(name: str, args: dict, delay_ms: int = 0) -> dict:
    line = {"tool_call": {"name": name, "args": dict(args)}}
    return {**line, "delay_ms": delay_ms} if delay_ms else line


def refused(message: str = REFUSAL) -> dict:
    return {"refuse": message}


def error(status: int = 500) -> dict:
    return {"error": status}


def truncated() -> dict:
    return {"truncate": True}


def write_scenario(folder: Path, name: str, lines: list[dict]) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.jsonl"
    path.write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
    return path


def scenarios(folder: Path) -> list[str]:
    try:
        return sorted(p.stem for p in folder.glob("*.jsonl") if p.is_file())
    except OSError:
        return []


def load(folder: Path, name: str) -> list[dict] | None:
    if not name or "/" in name or name.startswith(".") or name == ECHO:
        return None
    path = folder / f"{name}.jsonl"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    lines = []
    for raw in text.splitlines():
        if raw.strip():
            try:
                row = json.loads(raw)
            except ValueError:
                continue
            if isinstance(row, dict):
                lines.append(row)
    return lines


# ── one reply ───────────────────────────────────────────────────────────────

def last_user(messages: list) -> str:
    for m in reversed(messages):
        if isinstance(m, dict) and m.get("role") == "user":
            c = m.get("content")
            return c if isinstance(c, str) else json.dumps(c)
    return ""


def turn_of(messages: list) -> int:
    return sum(1 for m in messages if isinstance(m, dict) and m.get("role") == "assistant")


def completion(model: str, message: dict, usage: dict) -> dict:
    u = {k: int(usage.get(k) or 0) for k in ("prompt_tokens", "completion_tokens")}
    u["total_tokens"] = u["prompt_tokens"] + u["completion_tokens"]
    finish = "tool_calls" if message.get("tool_calls") else "stop"
    return {"id": "chatcmpl-selftest", "object": "chat.completion", "created": int(time.time()),
            "model": model, "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": u}


def reply_for(line: dict | None, model: str, turn: int, messages: list) -> tuple[int, bytes, str]:
    """(status, body, kind) for one scenario line; kind names it for the request log."""
    if line is None:
        text = (f"Fake answer: you asked {last_user(messages)[:200]!r}. No tools were used."
                if model == ECHO else OVER)
        msg = {"role": "assistant", "content": text}
        return 200, json.dumps(completion(model, msg, USAGE)).encode(), "echo" if model == ECHO else "over"
    if "error" in line:
        status = int(line["error"] or 500)
        return status, json.dumps({"error": {"message": f"the server failed with {status}"}}).encode(), "error"
    if "refuse" in line:
        return 400, json.dumps({"error": {"message": str(line["refuse"])}}).encode(), "refuse"
    calls = line.get("tool_calls")
    if "tool_call" in line:
        calls = [line["tool_call"]]
    usage = line.get("usage") if isinstance(line.get("usage"), dict) else USAGE
    if calls:
        tool_calls = []
        for i, c in enumerate(calls):
            if not isinstance(c, dict):
                continue
            fn = {"name": str(c.get("name") or ""), "arguments": json.dumps(c.get("args") or {})}
            tool_calls.append({"id": f"call-{turn}-{i}", "type": "function", "function": fn})
        msg = {"role": "assistant", "content": "", "tool_calls": tool_calls}
        kind = "tool_call"
    else:
        msg = {"role": "assistant", "content": str(line.get("answer") or "")}
        kind = "answer"
    body = json.dumps(completion(model, msg, usage)).encode()
    if line.get("truncate"):
        return 200, body[: max(1, len(body) // 2)], "truncate"
    return 200, body, kind


# ── the server ──────────────────────────────────────────────────────────────

class FakeEngine:
    def __init__(self, folder: Path | None = None, port: int = 0, log: Path | None = None) -> None:
        self.folder = Path(folder) if folder else SCENARIOS
        self.log = log
        self._lock = threading.Lock()
        engine = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "metabot-fake-engine/1"
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):
                pass

            def _send(self, status, body, ctype="application/json"):
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except OSError:  # the client went away (a cancelled run)
                    pass

            def _json(self, status, obj):
                self._send(status, json.dumps(obj).encode())

            def do_GET(self):
                if self.path.rstrip("/").endswith("/models"):
                    names = [ECHO] + scenarios(engine.folder)
                    data = [{"id": n, "object": "model", "owned_by": "selftest"} for n in names]
                    return self._json(200, {"object": "list", "data": data})
                self._json(404, {"error": {"message": "not here"}})

            def do_POST(self):
                if not self.path.rstrip("/").endswith("/chat/completions"):
                    return self._json(404, {"error": {"message": "not here"}})
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    n = 0
                if n < 0 or n > BODY_LIMIT:
                    return self._json(413, {"error": {"message": "too large"}})
                try:
                    req = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
                except ValueError:
                    return self._json(400, {"error": {"message": "bad json"}})
                if not isinstance(req, dict):
                    return self._json(400, {"error": {"message": "bad json"}})
                if req.get("stream"):
                    return self._json(400, {"error": {"message": "streaming is not supported"}})
                model = str(req.get("model") or ECHO)
                messages = req.get("messages") if isinstance(req.get("messages"), list) else []
                turn = turn_of(messages)
                lines = load(engine.folder, model)
                line = lines[turn] if lines is not None and turn < len(lines) else None
                if lines is None:
                    model = ECHO
                delay = line.get("delay_ms") if line else 0
                status, body, kind = reply_for(line, model, turn, messages)
                engine._record({"ts": time.time(), "model": model, "turn": turn, "reply": kind,
                                "status": status, "messages": messages})
                if isinstance(delay, (int, float)) and delay > 0:
                    time.sleep(min(float(delay), 120_000) / 1000)
                self._send(status, body)

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/v1"
        self._thread: threading.Thread | None = None

    def _record(self, row: dict) -> None:
        if not self.log:
            return
        with self._lock:
            try:
                with open(self.log, "a", encoding="utf-8") as f:
                    f.write(json.dumps(row) + "\n")
            except OSError:
                pass

    def start(self) -> "FakeEngine":
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def serve(self) -> None:
        self.server.serve_forever()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="the self-test's scripted fake engine (loopback only)")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--port-file", default="")
    ap.add_argument("--scenarios", default=str(SCENARIOS))
    ap.add_argument("--log", default="")
    a = ap.parse_args(argv)
    engine = FakeEngine(Path(a.scenarios), a.port, Path(a.log) if a.log else None)
    if a.port_file:
        Path(a.port_file).write_text(f"{engine.port}\n", encoding="utf-8")
    sys.stderr.write(f"fake engine: listening on 127.0.0.1:{engine.port}\n")
    sys.stderr.flush()

    def stop(*_):
        threading.Thread(target=engine.server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    engine.serve()
    engine.server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
