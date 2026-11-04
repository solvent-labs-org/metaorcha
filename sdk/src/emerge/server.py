"""A dependency-light A2A-compatible HTTP server for SDK agents.

Implements the same wire contract the Metaorcha SuperAgent speaks to A2A agents,
using only the standard library so ``emerge run`` has no heavy dependencies:

- ``GET  /health``                  → liveness probe
- ``GET  /.well-known/agent.json``  → A2A agent card (registry harvests skills)
- ``POST /``                        → JSON-RPC 2.0: ``message/send`` + ``tasks/get``

Tasks complete synchronously, so ``message/send`` returns a completed task and
``tasks/get`` reads it back from a small in-memory store.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .sdk import AgentSpec

logger = logging.getLogger("emerge.server")


def agent_card(spec: AgentSpec, host: str = "localhost") -> dict[str, Any]:
    """Build the A2A agent card served at /.well-known/agent.json."""
    return {
        "schemaVersion": "1.0",
        "name": spec.name,
        "description": spec.description,
        "url": f"http://{host}:{spec.port}",
        "version": spec.version,
        "capabilities": {"streaming": False, "pushNotifications": False},
        "skills": [
            {
                "id": s.id,
                "name": s.name,
                "description": s.description,
                "tags": s.tags,
                "examples": s.examples,
            }
            for s in spec.skills
        ],
    }


def _extract_text(message: dict[str, Any]) -> str:
    parts = message.get("parts", []) or []
    texts = [
        p.get("text", "")
        for p in parts
        if p.get("kind") == "text" or p.get("type") == "text"
    ]
    return " ".join(texts).strip()


def _completed_task(task_id: str, answer: str) -> dict[str, Any]:
    return {
        "id": task_id,
        "status": {"state": "completed"},
        "artifacts": [{"parts": [{"kind": "text", "text": answer}]}],
    }


def _failed_task(task_id: str, error: str) -> dict[str, Any]:
    return {
        "id": task_id,
        "status": {
            "state": "failed",
            "message": {
                "role": "agent",
                "parts": [{"kind": "text", "text": f"Error: {error}"}],
            },
        },
    }


def _make_handler(spec: AgentSpec, host: str):
    card = agent_card(spec, host=host)
    task_store: dict[str, dict[str, Any]] = {}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:  # quieter logs
            logger.debug("%s - %s", self.address_string(), fmt % args)

        def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path == "/health":
                self._send_json(
                    {"status": "healthy", "agent": spec.name, "version": spec.version}
                )
            elif self.path == "/.well-known/agent.json":
                self._send_json(card)
            else:
                self._send_json({"error": "not found"}, status=404)

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", 0) or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                self._send_json(
                    {
                        "jsonrpc": "2.0",
                        "id": "",
                        "error": {"code": -32700, "message": "Parse error"},
                    },
                    status=400,
                )
                return

            rpc_id = body.get("id", "")
            method = body.get("method", "")
            params = body.get("params", {}) or {}

            if method == "message/send":
                message = params.get("message", {}) or {}
                task_id = (
                    params.get("taskId") or message.get("taskId") or str(uuid.uuid4())
                )
                query = _extract_text(message)
                try:
                    answer = asyncio.run(spec.invoke(query))
                    task = _completed_task(task_id, answer)
                except Exception as exc:  # a handler that raises is a failed task
                    logger.exception("Handler raised for task %s", task_id)
                    task = _failed_task(task_id, str(exc))
                task_store[task_id] = task
                self._send_json({"jsonrpc": "2.0", "id": rpc_id, "result": task})
            elif method == "tasks/get":
                task_id = params.get("id", "")
                found = task_store.get(task_id)
                if found is None:
                    self._send_json(
                        {
                            "jsonrpc": "2.0",
                            "id": rpc_id,
                            "error": {
                                "code": -32602,
                                "message": f"Task {task_id!r} not found",
                            },
                        }
                    )
                else:
                    self._send_json({"jsonrpc": "2.0", "id": rpc_id, "result": found})
            else:
                self._send_json(
                    {
                        "jsonrpc": "2.0",
                        "id": rpc_id,
                        "error": {
                            "code": -32601,
                            "message": f"Method not found: {method!r}",
                        },
                    }
                )

    return Handler


def serve_agent(
    spec: AgentSpec, host: str = "0.0.0.0", *, block: bool = True
) -> ThreadingHTTPServer:
    """Start an HTTP server for a single agent. Returns the server.

    When ``block`` is False the server runs in a daemon thread (useful for tests
    and multi-agent serving); otherwise this call blocks via ``serve_forever``.
    """
    card_host = os.getenv("EMERGE_ADVERTISE_HOST", "localhost")
    httpd = ThreadingHTTPServer((host, spec.port), _make_handler(spec, card_host))
    if block:
        httpd.serve_forever()
    else:
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd
