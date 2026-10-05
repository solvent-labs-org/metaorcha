"""Local run journal: the raw steps behind a sealed RFC 0003 receipt.

A journal is one JSON file per run under ``<root>/.orcha/runs/<run_id>.json``.
It holds what the receipt only commits to: the raw ``args`` and ``output`` of
every step, so the holder can later reveal a step's preimage against its
``args_hash`` / ``output_hash``. The journal never leaves the machine; the
receipt (``<root>/.orcha/receipts/<run_id>.json``) is what travels and is
what ``orcha verify`` checks.

Sealing a journal applies the declared-acceptance rules of
:mod:`emerge.criteria` — the same module the platform's run observer reads
(AD-20), so a locally sealed receipt carries the same ``policy_version``
digest and the same ``declared_acceptance`` verdict a platform run with the
same criteria would. Nothing here evaluates a criterion itself.

The step and verdict shapes, the canonical bytes and the signature are those
of :mod:`emerge.record`; nothing here adds an envelope field.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .criteria import (
    NOT_APPLICABLE,
    SUPPORTED_CRITERIA,
    compose_policy_version,
    criteria_digest,
    run_declared_acceptance,
    step_declared_acceptance,
)
from .record import key_did, seal_run

JOURNAL_FORMAT = "orcha.run-journal/v1"
RUNS_DIR = Path(".orcha") / "runs"
RECEIPTS_DIR = Path(".orcha") / "receipts"
DEFAULT_POLICY = "local-session/1.0"

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def utc_now() -> str:
    """RFC 3339 UTC with the Z designator, second precision (the envelope rule)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Criteria ─────────────────────────────────────────────────────────────────


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


def declared_acceptance_verdict(
    criteria: dict[str, Any], steps: list[dict[str, Any]]
) -> dict[str, str] | None:
    """The run-level ``declared_acceptance`` verdict, or ``None`` without criteria.

    Each journaled step is judged on its raw ``output`` by the one step rule,
    and the run rule folds those entries exactly as the platform does.
    """
    if not criteria:
        return None
    if not steps:
        # declaring a criterion and running nothing is not acceptance: the run
        # rule sees every declared key as applied to no step and fails it
        declared = {
            k: NOT_APPLICABLE
            for k, v in criteria.items()
            if v or k not in SUPPORTED_CRITERIA
        }
        entries = [
            {"result": NOT_APPLICABLE, "detail": "no steps", "criteria": declared}
        ]
        return run_declared_acceptance(entries)
    return run_declared_acceptance(
        [step_declared_acceptance(criteria, step.get("output")) for step in steps]
    )


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
