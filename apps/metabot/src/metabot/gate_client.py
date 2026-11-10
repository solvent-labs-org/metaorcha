"""The gate the engine runs before every tool call: the standard library alone (G1).

It reads the engine's hook JSON on stdin, hands the call as it came to the run's control
socket, and prints the runner's decision as the engine's ``PreToolUse`` output. The runner
holds the policy and the classifier (``metabot.hook.gate_call``): a read passes there, the
switchboard's own tools pass there, and anything else is decided as it always was. Nothing
here imports another MetaBot module, so a call pays for the interpreter and this file.

Any failure blocks the call, exit 2 with a line on stderr and nothing on stdout: a runner
that cannot be reached (for a read too), a reply that is not ``{"allow": true|false}``,
input that is not a JSON object, or arguments other than
``--control PATH [--prefix P] [--switchboard]``. A broken gate never fails open.
"""

from __future__ import annotations

import json
import socket
import sys

BLOCKING_EXIT = 2
REQUEST_TIMEOUT_S = 870.0  # under the engine's own hook timeout (900 s)
MAX_REPLY = 1 << 20


def _args(argv: list[str]) -> tuple[str, str, bool]:
    control, prefix, switchboard = "", "claude-code", False
    i = 0
    while i < len(argv):
        word = argv[i]
        if word == "--switchboard":
            switchboard, i = True, i + 1
        elif word in ("--control", "--prefix") and i + 1 < len(argv):
            if word == "--control":
                control = argv[i + 1]
            else:
                prefix = argv[i + 1]
            i += 2
        else:
            raise ValueError(f"unknown argument {word!r}")
    if not control:
        raise ValueError("no --control socket")
    return control, prefix, switchboard


def ask(
    control: str, msg: dict[str, object], timeout: float = REQUEST_TIMEOUT_S
) -> dict[str, object]:
    """One request on the control socket; the reply must be a decision."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(control)
        sock.sendall(json.dumps(msg).encode("utf-8") + b"\n")
        data = b""
        while not data.endswith(b"\n"):
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
            if len(data) > MAX_REPLY:
                raise ValueError("the reply is too large")
    reply = json.loads(data)
    if not isinstance(reply, dict) or not isinstance(reply.get("allow"), bool):
        raise ValueError(f"the runner gave no decision: {str(reply)[:200]}")
    return reply


def decision(allow: bool, reason: str) -> dict[str, object]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow" if allow else "deny",
            "permissionDecisionReason": f"metabot: {reason}",
        }
    }


def main(argv: list[str] | None = None) -> int:
    try:
        control, prefix, switchboard = _args(sys.argv[1:] if argv is None else argv)
        payload = json.loads(sys.stdin.read())
        if not isinstance(payload, dict):
            raise ValueError("the hook input is not an object")
        msg = {"op": "gate", "prefix": prefix, "switchboard": switchboard, "payload": payload}
        reply = ask(control, msg)
        out = decision(reply["allow"] is True, str(reply.get("reason") or ""))
    except Exception as exc:  # fail closed: exit 2 blocks the tool call
        print(f"metabot gate unavailable, call blocked: {exc}", file=sys.stderr)
        return BLOCKING_EXIT
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
