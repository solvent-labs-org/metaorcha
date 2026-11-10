"""Approving a workflow, and revoking that: each is a signed receipt over its sha256.

An approval is an ordinary receipt, kept as ``wfa-<16 digits of the sha256>-<time>-<hex>``,
whose ``workflow_approval`` verdict signs ``sha256=<the workflow's sha256>;name=<its
name>;surface=cli``. A revoke is kept as ``wfr-...`` and signs one ``workflow_revoke``
verdict, ``sha256=...;approval=<the approval's id>;surface=cli``, per approval it ends.
Neither is a run: both stay out of the run index and out of ``metabot cost``.

What counts is what is signed, never the journal's meta: an approval is in force when its
receipt verifies, is signed with this home's key, names its own id, and its verdict names
the workflow's sha256 as the bytes hash now; and no revoke names it. A revoke receipt for
the workflow that does not verify, or that another key signed, is taken as a revoke of
every approval of it: a run is refused rather than let through on a record that cannot be
read.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from emerge.journal import utc_now
from emerge.record import key_did, load_signing_key, resolve_key_path
from emerge.run_attestation import verify_run_attestation

from metabot.paths import kept_receipts
from metabot.receipts import ensure_key, seal_and_store
from metabot.workflows import SHA_RE, Workflow

APPROVE_CHECK = "workflow_approval"
REVOKE_CHECK = "workflow_revoke"
APPROVE_TOOL = "metabot.workflow.approve"
REVOKE_TOOL = "metabot.workflow.revoke"
APPROVE_PREFIX = "wfa"
REVOKE_PREFIX = "wfr"
SURFACE = "cli"  # approvals on the web come later, and sign surface=web
PREFIX_DIGITS = 16


class NotApproved(ValueError):
    """Why a workflow may not run: no approval in force for its bytes as they are now."""


@dataclass(frozen=True)
class Approval:
    run_id: str
    receipt: Path
    signed_at: str  # the receipt's finished_at, as signed
    signer: str

    @property
    def digest(self) -> str:
        """The sha256 of the approval receipt's file, as a run's seal names it."""
        return hashlib.sha256(self.receipt.read_bytes()).hexdigest()


@dataclass
class ApprovalState:
    sha256: str
    in_force: list[Approval] = field(default_factory=list)
    revoked: list[Approval] = field(default_factory=list)
    unreadable_revokes: list[str] = field(default_factory=list)  # taken as revoking all


def _id(prefix: str, sha256: str) -> str:
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return f"{prefix}-{sha256[:PREFIX_DIGITS]}-{stamp}-{secrets.token_hex(3)}"


@dataclass(frozen=True)
class _Signer:
    did: str
    public_key: bytes  # the raw 32 bytes


def _our_signer() -> _Signer | None:
    """This home's signer, read without making a key: None when there is none yet."""
    path = resolve_key_path()
    if not path.is_file():
        return None
    try:
        key = load_signing_key(path)
    except (ValueError, OSError):
        return None
    return _Signer(key_did(key), key.public_key().public_bytes_raw())


def _signed_by(signer: Any, our: _Signer | None) -> bool:
    """Whether a verified envelope's ``signer`` is this home's key, by its public key.

    The DID alone is a name any key can write: the signature is checked against
    ``public_key_b64``, so that is what must be ours.
    """
    if our is None or not isinstance(signer, dict) or signer.get("did") != our.did:
        return False
    raw = signer.get("public_key_b64")
    if not isinstance(raw, str):
        return False
    try:
        return base64.b64decode(raw, validate=True) == our.public_key
    except (binascii.Error, ValueError):
        return False


def _detail(text: Any) -> dict[str, str] | None:
    """A verdict's ``k=v;k=v`` detail as a table; None when it is not one, or a key repeats."""
    if not isinstance(text, str):
        return None
    out: dict[str, str] = {}
    for part in text.split(";"):
        key, sep, value = part.partition("=")
        if not sep or not key or key in out:
            return None
        out[key] = value
    return out


def _signed(path: Path, our: _Signer | None) -> dict[str, Any] | None:
    """The envelope at ``path`` when it verifies, names the run its file is kept as, and
    this home's key signed it, its public key and not only its DID; None otherwise."""
    try:
        envelope = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(envelope, dict) or not verify_run_attestation(envelope).valid:
        return None
    if not _signed_by(envelope.get("signer"), our):
        return None
    if envelope.get("run_id") != path.name[: -len(".json")]:
        return None
    return envelope


def _verdicts(envelope: Mapping[str, Any], check: str) -> list[dict[str, str]]:
    found = []
    for verdict in envelope.get("verdicts") or []:
        if isinstance(verdict, dict) and verdict.get("check") == check:
            detail = _detail(verdict.get("detail"))
            if detail is not None and verdict.get("result") == "pass":
                found.append(detail)
    return found


def _kept(prefix: str, sha256: str) -> list[Path]:
    folder = kept_receipts()
    if not folder.is_dir():
        return []
    return sorted(folder.glob(f"{prefix}-{sha256[:PREFIX_DIGITS]}-*.json"))


def approval_state(sha256: str) -> ApprovalState:
    """Every approval of ``sha256`` this home signed, in force or revoked."""
    if not SHA_RE.match(sha256):
        raise ValueError(f"not a sha256: {sha256!r}")
    our = _our_signer()
    state = ApprovalState(sha256)
    revoked: set[str] = set()
    for path in _kept(REVOKE_PREFIX, sha256):
        envelope = _signed(path, our)
        if envelope is None:
            state.unreadable_revokes.append(path.name[: -len(".json")])
            continue
        for detail in _verdicts(envelope, REVOKE_CHECK):
            if detail.get("sha256") == sha256 and detail.get("approval"):
                revoked.add(detail["approval"])
    for path in _kept(APPROVE_PREFIX, sha256):
        envelope = _signed(path, our)
        if envelope is None:
            continue  # an approval that does not verify, or another key's, approves nothing
        names = _verdicts(envelope, APPROVE_CHECK)
        if len(names) != 1 or names[0].get("sha256") != sha256:
            continue
        approval = Approval(
            str(envelope["run_id"]), path, str(envelope.get("finished_at")), str(our and our.did)
        )
        (state.revoked if approval.run_id in revoked else state.in_force).append(approval)
    return state


def approval_for_run(wf: Workflow) -> Approval:
    """The approval a run of ``wf`` goes under: the newest in force for its sha256.

    NotApproved says why there is none.
    """
    state = approval_state(wf.sha256)
    if state.unreadable_revokes:
        raise NotApproved(
            f"a revoke receipt for it does not verify ({', '.join(state.unreadable_revokes)}), "
            "so every approval of it is taken as revoked"
        )
    if state.in_force:
        return max(state.in_force, key=lambda a: (a.signed_at, a.run_id))
    if state.revoked:
        raise NotApproved(
            f"its approval was revoked; approve it again: metabot workflow approve {wf.name}"
        )
    raise NotApproved(
        f"it is not approved: read it with metabot workflow show {wf.name}, then "
        f"metabot workflow approve {wf.name}"
    )


def approve(wf: Workflow) -> Approval:
    """Sign an approval of ``wf``'s bytes, as they hash now."""
    key, did = ensure_key()
    run_id = _id(APPROVE_PREFIX, wf.sha256)
    now = utc_now()
    receipt = seal_and_store(
        run_id=run_id,
        steps=[
            {
                "call_id": "approve",
                "tool": APPROVE_TOOL,
                "args": {"sha256": wf.sha256, "name": wf.name},
                "output": {"approved": True},
                "success": True,
                "latency_ms": 0,
            }
        ],
        verdicts=[
            {
                "check": APPROVE_CHECK,
                "result": "pass",
                "detail": f"sha256={wf.sha256};name={wf.name};surface={SURFACE}",
            }
        ],
        started_at=now,
        finished_at=now,
        key=key,
        meta={
            "engine": "approval",
            "sealed": APPROVE_CHECK,
            "workflow": wf.name,
            "workflow_sha256": wf.sha256,
            "surface": SURFACE,
        },
        index=False,
    )
    return Approval(run_id, receipt, now, did)


def revoke(sha256: str, approvals: list[Approval]) -> Path:
    """Sign a revoke of each of ``approvals`` of ``sha256``; the revoke receipt's path."""
    if not approvals:
        raise ValueError("nothing to revoke")
    key, _did = ensure_key()
    now = utc_now()
    return seal_and_store(
        run_id=_id(REVOKE_PREFIX, sha256),
        steps=[
            {
                "call_id": a.run_id,
                "tool": REVOKE_TOOL,
                "args": {"sha256": sha256, "approval": a.run_id, "approval_receipt": a.digest},
                "output": {"revoked": True},
                "success": True,
                "latency_ms": 0,
            }
            for a in approvals
        ],
        verdicts=[
            {
                "check": REVOKE_CHECK,
                "result": "pass",
                "detail": f"sha256={sha256};approval={a.run_id};surface={SURFACE}",
            }
            for a in approvals
        ],
        started_at=now,
        finished_at=now,
        key=key,
        meta={"engine": "revoke", "sealed": REVOKE_CHECK, "workflow_sha256": sha256},
        index=False,
    )
