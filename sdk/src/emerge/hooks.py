"""Harness hook adapters: turn a coding session into a journal, then a receipt.

Thin shims with no logic of their own. Each adapter reads one hook payload
from stdin, maps it onto the journal in :mod:`emerge.journal`, and on the
session's stop event seals the receipt with the local key. Nothing is hosted,
nothing leaves the machine; the receipt verifies with ``orcha verify``.

Claude Code (verified on 2.1.278, 2026-09-21): a ``Bash`` call with a
non-zero exit fires ``PostToolUseFailure`` with ``error: "Exit code N"`` and
no ``tool_response``; a zero exit fires ``PostToolUse`` with ``tool_response
{stdout, stderr, interrupted, isImage, noOutputExpected}``. No event carries
a numeric exit code, so the adapter subscribes to both events and derives it:
``PostToolUse`` ⇒ ``exit_code 0``; ``PostToolUseFailure`` ⇒ the integer parsed
from ``error`` (``None`` when it does not parse, which ``exit_zero`` then
treats as not applicable — and a run of only such steps fails closed).
``tool_use_id`` is the step's ``call_id`` on both, ``session_id`` names the
run, ``cwd`` locates the journal. ``Stop`` seals.

A hook must never break the session it records: every path exits 0 (recorded)
or 1 (a non-blocking error the harness shows the user). Exit 2 — which would
block the tool call — is never used.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

from .journal import (
    DEFAULT_POLICY,
    append_step,
    journal_path,
    load_journal,
    new_journal,
    receipt_path,
    save_journal,
    seal_journal,
)
from .record import EphemeralKeyRefused, load_signing_key
from .run_attestation import compute_envelope_digest, verify_run_attestation

CLAUDE_CODE = "claude-code"
_EXIT_CODE_RE = re.compile(r"^Exit code (-?\d+)$")
_STEP_EVENTS = frozenset({"PostToolUse", "PostToolUseFailure"})
_SEAL_EVENTS = frozenset({"Stop"})


def claude_code_step(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Map a ``PostToolUse``/``PostToolUseFailure`` payload to a raw journal step.

    Returns ``None`` for any other event. ``args`` is the tool's input as the
    harness reported it; ``output`` is an object so the criteria can read an
    exit code from it. Only the fields below are copied — never
    ``transcript_path``, ``permission_mode`` or anything about the user.
    """
    event = payload.get("hook_event_name")
    if event not in _STEP_EVENTS:
        return None
    tool_name = payload.get("tool_name")
    call_id = payload.get("tool_use_id")
    if not isinstance(tool_name, str) or not isinstance(call_id, str):
        raise ValueError("payload lacks tool_name/tool_use_id")
    tool_input = payload.get("tool_input")
    args = tool_input if isinstance(tool_input, dict) else {"input": tool_input}
    latency = payload.get("duration_ms")
    latency_ms = latency if isinstance(latency, int) and latency >= 0 else 0

    if event == "PostToolUse":
        response = payload.get("tool_response")
        output: dict[str, Any] = (
            dict(response) if isinstance(response, dict) else {"response": response}
        )
        if tool_name == "Bash":
            output["exit_code"] = 0
        success = True
    else:
        error = payload.get("error")
        output = {"error": error if isinstance(error, str) else str(error)}
        match = _EXIT_CODE_RE.match(output["error"])
        if tool_name == "Bash" and match:
            output["exit_code"] = int(match.group(1))
        success = False

    return {
        "call_id": call_id,
        "tool": f"{CLAUDE_CODE}/{tool_name}",
        "args": args,
        "output": output,
        "success": success,
        "latency_ms": latency_ms,
    }


def run_claude_code_hook(
    payload: dict[str, Any],
    *,
    root: str | None = None,
    key_path: str | None = None,
    policy: str = DEFAULT_POLICY,
    criteria: dict[str, bool] | None = None,
    agent_did: str | None = None,
    err=None,
) -> int:
    """Handle one Claude Code hook payload. Returns the hook's exit code (0/1)."""
    err = sys.stderr if err is None else err
    prog = "orcha record hook claude-code"
    event = payload.get("hook_event_name")
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        print(f"{prog}: payload has no session_id; nothing recorded", file=err)
        return 1
    base = Path(root) if root else Path(payload.get("cwd") or ".")

    try:
        path = journal_path(base, session_id)
    except ValueError as exc:
        print(f"{prog}: {exc}", file=err)
        return 1

    if event in _STEP_EVENTS:
        try:
            step = claude_code_step(payload)
        except ValueError as exc:
            print(f"{prog}: {exc}", file=err)
            return 1
        if step is None:
            return 0
        try:
            journal = (
                load_journal(path)
                if path.is_file()
                else new_journal(
                    run_id=session_id,
                    agent_dids=[agent_did] if agent_did else [],
                    policy=policy,
                    criteria=criteria,
                    harness=CLAUDE_CODE,
                )
            )
            append_step(journal, step)
            save_journal(path, journal)
        except (OSError, ValueError) as exc:
            print(f"{prog}: could not record step: {exc}", file=err)
            return 1
        return 0

    if event in _SEAL_EVENTS:
        if not path.is_file():
            return 0  # a turn with no tool calls leaves nothing to seal
        try:
            journal = load_journal(path)
            private_key = load_signing_key(key_path)
            envelope = seal_journal(journal, private_key)
        except EphemeralKeyRefused as exc:
            print(f"{prog}: not sealed — {exc}", file=err)
            return 1
        except (OSError, ValueError) as exc:
            print(f"{prog}: not sealed — {exc}", file=err)
            return 1
        verdict = verify_run_attestation(envelope)
        if not verdict.valid:
            print(
                f"{prog}: internal error — sealed envelope failed verification "
                f"{verdict.checks}",
                file=err,
            )
            return 1
        out = receipt_path(base, session_id)
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(envelope, indent=2) + "\n", encoding="utf-8")
        except OSError as exc:
            print(f"{prog}: cannot write {out}: {exc}", file=err)
            return 1
        print(
            f"sealed run_id={envelope['run_id']} steps={len(envelope['steps'])} "
            f"signer={envelope['signer']['did']} "
            f"digest={compute_envelope_digest(envelope)}\n"
            f"verify with: orcha verify {out}",
            file=err,
        )
        return 0

    return 0  # PreToolUse, Notification, ... — not ours


def claude_code_settings(command: str) -> dict[str, Any]:
    """The ``hooks`` block to merge into ``.claude/settings.json``."""
    hook = [{"hooks": [{"type": "command", "command": command}]}]
    return {
        "hooks": {
            "PostToolUse": hook,
            "PostToolUseFailure": hook,
            "Stop": hook,
        }
    }
