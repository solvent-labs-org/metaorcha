"""Local run journal: the raw steps behind a sealed RFC 0003 receipt.

A journal is one JSON file per run under ``<root>/.orcha/runs/<run_id>.json``.
It holds what the receipt only commits to: the raw ``args`` and ``output`` of
every step, so the holder can later reveal a step's preimage against its
``args_hash`` / ``output_hash``. The journal never leaves the machine; the
receipt (``<root>/.orcha/receipts/<run_id>.json``) is what travels and is
what ``orcha verify`` checks.

Sealing a journal applies the same declared-acceptance rules the platform
applies (``services/superagent/.../middleware/criteria.py`` and
``validator.run_observer._declared_acceptance``), so a locally sealed receipt
reads exactly like a platform one:

- ``policy_version`` is ``<policy>+criteria:<sha256 of the canonical criteria
  document>`` when criteria were declared, else ``<policy>``.
- Per step each criterion is ``pass`` | ``fail`` | ``n/a``. ``exit_zero``
  applies only to a step whose output carries an integer exit code
  (``exit_code`` / ``returncode`` / ``exit``); a step without one is ``n/a``.
  ``citations_required`` applies to every step.
- Run level, per criterion: ``fail`` if any applicable step failed; ``pass``
  if at least one applied and none failed; a criterion no step applied to
  fails the run (``no step reported an exit code``) — declaring ``exit_zero``
  and never running anything is not acceptance. One ``declared_acceptance``
  verdict carries the outcome.

The step and verdict shapes, the canonical bytes and the signature are those
of :mod:`emerge.record`; nothing here adds an envelope field.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .record import key_did, seal_run

JOURNAL_FORMAT = "orcha.run-journal/v1"
RUNS_DIR = Path(".orcha") / "runs"
RECEIPTS_DIR = Path(".orcha") / "receipts"
DEFAULT_POLICY = "local-session/1.0"

SUPPORTED_CRITERIA = frozenset({"citations_required", "exit_zero"})
CRITERIA_MARKER = "+criteria:"
NOT_APPLICABLE = "n/a"

_EXIT_KEYS = ("exit_code", "returncode", "exit")
_CITATION_REQUIRED_FIELDS = ("chunk_id", "source_title", "excerpt")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def utc_now() -> str:
    """RFC 3339 UTC with the Z designator, second precision (the envelope rule)."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Criteria (byte-identical digest, same per-step and run-level rules) ─────


def criteria_digest(criteria: dict[str, Any]) -> str:
    """sha256 hex of the canonical criteria document (matches the platform)."""
    raw = json.dumps(criteria, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def compose_policy_version(policy: str, digest: str | None) -> str:
    """``policy`` or ``<policy>+criteria:<digest>`` — both halves recoverable."""
    return f"{policy}{CRITERIA_MARKER}{digest}" if digest else policy


def parse_criteria(spec: str | None) -> dict[str, bool]:
    """``"exit_zero,citations_required"`` → ``{key: True}``; unknown keys raise."""
    if not spec:
        return {}
    criteria: dict[str, bool] = {}
    for raw in spec.split(","):
        key = raw.strip()
        if not key:
            continue
        if key not in SUPPORTED_CRITERIA:
            raise ValueError(
                f"unsupported criterion {key!r}; supported: {sorted(SUPPORTED_CRITERIA)}"
            )
        criteria[key] = True
    return criteria


def parse_exit_code(output: Any) -> int | None:
    """Integer exit code from a step output object, or ``None``.

    Reads ``exit_code`` / ``returncode`` / ``exit``. Bool is not an exit code.
    A JSON string is decoded first so a step recorded as text behaves like the
    platform's ``raw_output`` path.
    """
    payload = output
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return None
    if not isinstance(payload, dict):
        return None
    for key in _EXIT_KEYS:
        raw = payload.get(key)
        if type(raw) is int:
            return raw
        if isinstance(raw, str) and raw.lstrip("-").isdigit():
            return int(raw)
    return None


def has_valid_citations(output: Any) -> bool:
    payload = output
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return False
    if not isinstance(payload, dict):
        return False
    citations = payload.get("citations")
    if not isinstance(citations, list) or not citations:
        return False
    return all(
        isinstance(c, dict) and all(c.get(field) for field in _CITATION_REQUIRED_FIELDS)
        for c in citations
    )


def evaluate_step(criteria: dict[str, Any], output: Any) -> dict[str, dict[str, str]]:
    """Per-criterion result for one step: ``{key: {"result", "detail"}}``."""
    results: dict[str, dict[str, str]] = {}
    for key in criteria:
        if key not in SUPPORTED_CRITERIA:
            results[key] = {"result": "fail", "detail": f"unsupported criterion: {key}"}
            continue
        if not criteria.get(key):
            continue
        if key == "citations_required":
            ok = has_valid_citations(output)
            results[key] = {
                "result": "pass" if ok else "fail",
                "detail": "ok" if ok else "missing citations",
            }
        elif key == "exit_zero":
            code = parse_exit_code(output)
            if code is None:
                results[key] = {
                    "result": NOT_APPLICABLE,
                    "detail": "no exit code in step output",
                }
            elif code != 0:
                results[key] = {"result": "fail", "detail": f"nonzero exit: {code}"}
            else:
                results[key] = {"result": "pass", "detail": "ok"}
    return results


def declared_acceptance_verdict(
    criteria: dict[str, Any], steps: list[dict[str, Any]]
) -> dict[str, str] | None:
    """The run-level ``declared_acceptance`` verdict, or ``None`` without criteria."""
    if not criteria:
        return None
    per_step = [evaluate_step(criteria, step.get("output")) for step in steps]
    keys = [k for k in criteria if criteria.get(k) or k not in SUPPORTED_CRITERIA]
    per_key: dict[str, str] = {}
    for key in keys:
        results = [p[key]["result"] for p in per_step if key in p]
        if "fail" in results:
            per_key[key] = "fail"
        elif "pass" in results:
            per_key[key] = "pass"
        else:
            per_key[key] = NOT_APPLICABLE
    failing = [k for k, v in per_key.items() if v == "fail"]
    unapplied = [k for k, v in per_key.items() if v == NOT_APPLICABLE]
    if failing:
        key = failing[0]
        step_detail = next(
            (
                p[key]["detail"]
                for p in per_step
                if p.get(key, {}).get("result") == "fail"
            ),
            "fail",
        )
        return {
            "check": "declared_acceptance",
            "result": "fail",
            "detail": f"{key}: {step_detail}",
        }
    if unapplied:
        key = unapplied[0]
        detail = (
            "no step reported an exit code"
            if key == "exit_zero"
            else f"no step was applicable to {key}"
        )
        return {"check": "declared_acceptance", "result": "fail", "detail": detail}
    return {"check": "declared_acceptance", "result": "pass", "detail": "ok"}


# ── Journal file ─────────────────────────────────────────────────────────────


def _safe_id(value: str, what: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID_RE.match(value):
        raise ValueError(
            f"{what} must be 1-128 chars of [A-Za-z0-9._-] to name a file (got {value!r})"
        )
    return value


def journal_path(root: str | os.PathLike[str], run_id: str) -> Path:
    return Path(root) / RUNS_DIR / f"{_safe_id(run_id, 'run_id')}.json"


def receipt_path(root: str | os.PathLike[str], run_id: str) -> Path:
    return Path(root) / RECEIPTS_DIR / f"{_safe_id(run_id, 'run_id')}.json"


def new_journal(
    *,
    run_id: str,
    agent_dids: list[str],
    policy: str = DEFAULT_POLICY,
    criteria: dict[str, bool] | None = None,
    started_at: str | None = None,
    harness: str | None = None,
) -> dict[str, Any]:
    journal: dict[str, Any] = {
        "format": JOURNAL_FORMAT,
        "run_id": _safe_id(run_id, "run_id"),
        "agent_dids": list(agent_dids),
        "policy": policy,
        "criteria": dict(criteria or {}),
        "started_at": started_at or utc_now(),
        "steps": [],
    }
    if harness:
        journal["harness"] = harness
    return journal


def load_journal(path: Path) -> dict[str, Any]:
    journal = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(journal, dict) or journal.get("format") != JOURNAL_FORMAT:
        raise ValueError(f"{path} is not a {JOURNAL_FORMAT} journal")
    if not isinstance(journal.get("steps"), list):
        raise ValueError(f"{path}: steps must be a list")
    return journal


def save_journal(path: Path, journal: dict[str, Any]) -> None:
    """Write atomically (rename) with owner-only permissions on a new file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(journal, fh, indent=2)
        fh.write("\n")
    tmp.replace(path)


def append_step(journal: dict[str, Any], step: dict[str, Any]) -> None:
    """Append one raw step ``{call_id, tool, args, output, success, latency_ms}``.

    A repeated ``call_id`` (a hook delivered twice) is dropped, so a receipt
    never counts one tool call as two.
    """
    if any(s.get("call_id") == step.get("call_id") for s in journal["steps"]):
        return
    journal["steps"].append(step)


def seal_journal(
    journal: dict[str, Any],
    private_key: Ed25519PrivateKey,
    *,
    finished_at: str | None = None,
) -> dict[str, Any]:
    """Build and sign the receipt for a journal (same rules as the platform).

    ``agent_dids`` defaults to the key's DID when the journal names none.
    Raises ValueError when the journal cannot make a conformant envelope.
    """
    criteria = journal.get("criteria") or {}
    verdict = declared_acceptance_verdict(criteria, journal["steps"])
    run = {
        "run_id": journal["run_id"],
        "agent_dids": journal.get("agent_dids") or [key_did(private_key)],
        "policy_version": compose_policy_version(
            journal.get("policy") or DEFAULT_POLICY,
            criteria_digest(criteria) if criteria else None,
        ),
        "steps": [
            {
                k: s[k]
                for k in ("call_id", "tool", "args", "output", "success", "latency_ms")
            }
            for s in journal["steps"]
        ],
        "verdicts": [verdict] if verdict else [],
        "started_at": journal["started_at"],
        "finished_at": finished_at or utc_now(),
    }
    return seal_run(run, private_key)
