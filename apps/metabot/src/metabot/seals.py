"""Seals: one signed receipt over every run of an office run or a ladder.

Each desk, round, advisor, rung and checks run seals its own receipt, and the
receipt signs where the run sits. A seal commits to the set. It is an
ordinary receipt, kept under the office run's or the ladder's id, whose steps
are the member runs in the order they ran: a step's ``call_id`` is the
member's run id, its args are ``{"run_id": <that id>}``, and its output is
the sha256 of the member's receipt file, or null for a run that never sealed one.
The envelope's Merkle root over the steps and its signature then cover which
runs belong and in what order, so anyone holding the seal and the receipts
can check offline that none was dropped, changed or added.

The seal's verdicts sign what governed the run: ``seal`` (its kind, how many
members, and whether it delivered), ``workflow`` (an office run's office.yaml
by its sha256, the checks, the rounds and the allow; a ladder's privacy),
``content`` (the task as kept, with a connection's token redacted, by a
salted digest, as a run's prompt is), ``budget`` when a money cap held it,
and a ``plugin`` verdict per plugin pin. A seal is not a run, so it stays out
of the run index and out of ``metabot cost``.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from emerge.journal import utc_now
from emerge.record import hash_value

from metabot.paths import kept_receipts, safe_id
from metabot.receipts import ensure_key, seal_and_store, verify_file

MEMBER_TOOL = "metabot.member"  # then .desk, .advisor, .checks or .rung


@dataclass(frozen=True)
class Member:
    run_id: str
    role: str  # desk, advisor, checks or rung
    receipt: Path | None  # None: the run never sealed one
    ok: bool  # what the office or the ladder made of it


def receipt_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def seal_runs(
    seal_id: str,
    members: Sequence[Member],
    *,
    delivered: bool,
    kind_detail: str,
    verdicts: Sequence[dict[str, str]],
    started_at: str,
    meta: Mapping[str, Any],
) -> Path:
    """Sign one receipt over ``members``, in order; returns the seal's path."""
    key, _did = ensure_key()
    steps = [
        {
            "call_id": m.run_id,
            "tool": f"{MEMBER_TOOL}.{m.role}",
            "args": {"run_id": m.run_id},
            "output": receipt_digest(m.receipt)
            if m.receipt is not None and m.receipt.is_file()
            else None,
            "success": m.ok,
            "latency_ms": 0,
        }
        for m in members
    ]
    seal = {
        "check": "seal",
        "result": "pass" if delivered else "fail",
        "detail": f"{kind_detail};members={len(members)}",
    }
    return seal_and_store(
        run_id=seal_id,
        steps=steps,
        verdicts=[seal, *verdicts],
        started_at=started_at,
        finished_at=utc_now(),
        key=key,
        meta={"engine": "seal", **meta, "members": [m.run_id for m in members]},
        index=False,
        commit=("task",) if "task" in meta else (),
    )


def is_seal(envelope: Mapping[str, Any]) -> bool:
    verdicts = envelope.get("verdicts")
    return isinstance(verdicts, list) and any(
        isinstance(v, dict) and v.get("check") == "seal" for v in verdicts
    )


def member_problems(envelope: Mapping[str, Any], beside: Path | None) -> list[str]:
    """What does not hold between a seal and its members' receipts; empty when all does.

    A member's receipt is looked for beside the seal first, then in this
    machine's receipts; nothing is created to look. Every member must be
    there unchanged and must verify, one the seal says has no receipt must
    have none, no member may be listed twice, and no receipt of the run may
    be there that the seal does not hold. The seal's own signature is checked
    apart from this, by ``verify_file``.
    """
    folders = list(dict.fromkeys(p for p in (beside, kept_receipts()) if p is not None))
    problems: list[str] = []
    listed: set[str] = set()
    steps = envelope.get("steps")
    for step in steps if isinstance(steps, list) else []:
        said = step.get("call_id") if isinstance(step, dict) else None
        try:
            run_id = safe_id(said) if isinstance(said, str) else ""  # it names no path
        except ValueError:
            run_id = ""
        if not run_id:
            problems.append(f"{said}: not a run id")
            continue
        if step.get("args_hash") != hash_value({"run_id": run_id}):
            problems.append(f"{run_id}: its step does not name it")
            continue
        if run_id in listed:  # and its receipt is read once, however often it is listed
            problems.append(f"{run_id}: the seal lists it twice")
            continue
        listed.add(run_id)
        try:
            problem = _member_problem(run_id, step.get("output_hash"), folders)
        except OSError:
            problem = f"{run_id}: its receipt cannot be read"
        if problem is not None:
            problems.append(problem)
    seal_id = envelope.get("run_id")
    try:
        seal_id = safe_id(seal_id) if isinstance(seal_id, str) else None
    except ValueError:
        seal_id = None
    if seal_id is None:
        return problems
    extra: set[str] = set()
    for folder in folders:
        try:  # listed outright: a glob passes over a folder it cannot list in silence
            with os.scandir(folder) as entries:
                names = [entry.name for entry in entries]
        except (FileNotFoundError, NotADirectoryError):
            continue  # no such folder: nothing of the run is there
        except OSError:
            problems.append(f"{folder}: cannot be read")
            continue
        prefix = f"{seal_id}."  # <seal>.<member>.json, never the seal itself
        extra |= {
            n[:-5] for n in names if n.startswith(prefix) and n[len(prefix) :].endswith(".json")
        }
    problems += [
        f"{run_id}: a receipt of this run that the seal does not hold"
        for run_id in sorted(extra - listed)
    ]
    return problems


def _member_problem(run_id: str, sealed: Any, folders: list[Path]) -> str | None:
    found = next((f / f"{run_id}.json" for f in folders if (f / f"{run_id}.json").is_file()), None)
    if sealed == hash_value(None):
        if found is not None:
            return f"{run_id}: the seal says it has no receipt, but one is here"
        return None
    if found is None:
        return f"{run_id}: its receipt is missing"
    if hash_value(receipt_digest(found)) != sealed:
        return f"{run_id}: its receipt is not the one sealed"
    if not verify_file(found).valid:
        return f"{run_id}: its receipt does not verify"
    return None


def unsealed_members(envelope: Mapping[str, Any]) -> list[str]:
    """The members the seal signs as having no receipt: runs that never sealed one."""
    steps = envelope.get("steps")
    return [
        str(step.get("call_id"))
        for step in (steps if isinstance(steps, list) else [])
        if isinstance(step, dict) and step.get("output_hash") == hash_value(None)
    ]


def seal_state(path: Path | str, run_id: str) -> str:
    """The seal of ``run_id`` as one line: valid with its members, or what does not hold."""
    path = Path(path)
    if not verify_file(path).valid:
        return "INVALID"
    try:
        envelope = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return "INVALID"
    if not isinstance(envelope, dict) or envelope.get("run_id") != run_id:
        return "INVALID: not this run's seal"
    problems = member_problems(envelope, path.parent)
    steps = envelope.get("steps")
    count = len(steps) if isinstance(steps, list) else 0
    if problems:
        return f"{len(problems)} problem(s) with its {count} members"
    none = len(unsealed_members(envelope))
    return f"valid, {count} members" + (f", {none} with no receipt" if none else "")
