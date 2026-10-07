"""PoC probe agent — the system-proof fixture for scripts/poc-e2e.sh.

A paid A2A agent built entirely on the emerge SDK. One deterministic
capability, a non-zero base_fee, and an optional HTTP-level failure-injection
mode. Together these let the PoC harness prove four claims no other test
covers: SDK registration round-trip, paid invocation → settlement row,
structural verification of the result, and the v1.3 transient retry-gate.

Run modes
---------
    python agent.py                # clean: serve on POC_PROBE_PORT (default 8930)
    python agent.py --flaky        # flaky: FIRST message/send gets HTTP 503,
                                   #        everything after is forwarded verbatim
    emerge publish agent.py --registry <url> --host <host>   # register (agent must be up)

Why the flake is an HTTP shim and not a raising handler: a handler-level
failure becomes a "failed" A2A task, which the SuperAgent returns as
"Error: ..." content — the *permanent* path that must NOT retry. The v1.3
retry-gate fires only on raised transient errors (timeout / connection /
5xx via httpx raise_for_status). A 503 on the wire simulates exactly the
infra blip the taxonomy classifies as transient.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import emerge

PORT = int(os.getenv("POC_PROBE_PORT", "8930"))  # advertised + registered port
INTERNAL_PORT = PORT + 1  # SDK server hides here in flaky mode


@emerge.agent(
    name="PoC Probe",
    description=(
        "Deterministic system-diagnostics probe. Given any task text it returns "
        "a single PROBE-REPORT line with a checksum — used to prove the Orcha "
        "loop end-to-end: registration, discovery, paid A2A invocation, "
        "verification, and settlement."
    ),
    base_fee="0.05",
    port=PORT,
    tags=["poc", "probe", "diagnostics", "system-check"],
    skills=[
        {
            "id": "probe-report",
            "name": "Probe Report",
            "description": (
                "Run a system probe: returns a deterministic PROBE-REPORT line "
                "(task length + checksum) for the given task text."
            ),
            "tags": ["probe", "diagnostics", "report", "system-check"],
            "examples": ["Run a system probe report for hello-world"],
        }
    ],
)
def probe(task: str) -> str:
    checksum = sum(ord(c) for c in task) % 9973
    return f"PROBE-REPORT ok task_len={len(task)} checksum={checksum}"


# ── flaky shim ────────────────────────────────────────────────────────────────
# Sits on PORT, forwards to the SDK server on INTERNAL_PORT. The first
# message/send per process gets a bare HTTP 503 (raises httpx.HTTPStatusError
# in the SuperAgent A2A handler → classified transient → retry-gate re-runs).

_flaky_lock = threading.Lock()
_flaky_armed = True

# Only an origin-form request target ("/path?query") is forwarded. The URL is
# the loopback address with the target appended, so a target that does not
# start with "/" runs on into the port: after port 601, "0/x" is port 6010,
# another service on this host.
_ORIGIN_FORM = r"/[!-~]*"


class _FlakyProxy(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: object) -> None:  # quiet
        pass

    def _forward(self, body: bytes | None) -> None:
        target = self.path
        if re.fullmatch(_ORIGIN_FORM, target):
            self._send_to_internal(target, body)
        else:
            self.send_error(400, "request target must be a path")

    def _send_to_internal(self, target: str, body: bytes | None) -> None:
        req = urllib.request.Request(
            f"http://127.0.0.1:{INTERNAL_PORT}{target}",
            data=body,
            method=self.command,
            headers={"Content-Type": self.headers.get("Content-Type", "application/json")},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = resp.read()
                self.send_response(resp.status)
                self.send_header("Content-Type", resp.headers.get("Content-Type", "application/json"))
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            self.send_response(exc.code)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    def do_GET(self) -> None:  # /health, /.well-known/agent.json
        self._forward(None)

    def do_POST(self) -> None:
        global _flaky_armed
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        if b'"message/send"' in body:
            with _flaky_lock:
                if _flaky_armed:
                    _flaky_armed = False
                    print("poc-probe: FLAKY — returning 503 for first message/send", flush=True)
                    payload = b'{"error": "poc-probe flaky mode: injected transient failure"}'
                    self.send_response(503)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
        self._forward(body)


def _run_flaky() -> int:
    from emerge.server import serve_agent

    spec = emerge.registered_agents()[0]
    spec.port = INTERNAL_PORT  # SDK server moves to the internal port
    serve_agent(spec, block=False)
    print(f"✓ SDK server on http://localhost:{INTERNAL_PORT} (internal)", flush=True)
    shim = ThreadingHTTPServer(("0.0.0.0", PORT), _FlakyProxy)  # noqa: S104
    print(f"✓ Flaky shim on http://localhost:{PORT} — first message/send will 503", flush=True)
    try:
        shim.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="PoC probe agent")
    parser.add_argument(
        "--flaky",
        action="store_true",
        help="inject an HTTP 503 on the first message/send (retry-gate proof)",
    )
    args = parser.parse_args()
    flaky = args.flaky or os.getenv("POC_FLAKY", "").lower() in ("1", "true", "first_call")
    if flaky:
        return _run_flaky()
    return emerge.run()


if __name__ == "__main__":
    raise SystemExit(main())
