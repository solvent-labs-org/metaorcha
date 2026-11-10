"""Receipt bundles: a run's receipts, its seal and its journals, as one folder or tarball
that anyone can check offline with ``metabot receipts verify --chain``.

A bundle holds:

- ``bundle.json``: its format, the run it is of, that run's kind (run, office, ladder
  or workflow), and the sha256 of every other file in it;
- ``receipts/<id>.json``: the run's receipt and, for a seal, each member's, byte for
  byte as this machine keeps them; for a workflow's seal, a step's ladder or office
  run brings its own members too;
- for a workflow run or a firing, ``workflows/<sha256>.json``, the workflow file's
  exact bytes, and ``approvals/<id>.json``, the receipt of the approval it ran under:
  the seal's ``workflow_run`` verdict signs the one's sha256 and the other's id and
  sha256. A revoke is never in a bundle: it stays on the machine that signed it, so a
  bundle cannot show that an approval was not revoked;
- ``journals/<id>.json``: for each of those receipts whose journal this machine keeps,
  what the receipt hashes: its steps' arguments and outputs, and the texts its
  ``content`` verdict commits to that a bundle shows (a prompt, an answer, a task).
  Each other committed text, a login's name or folder, is withheld, salt and all:
  the receipt still signs its salted digest. Only ``--journals`` puts them in; a
  plain export holds the receipts alone, which hold hashes and no text.

Nothing in a bundle is taken on its word, as ``meta`` sits outside a signature. A
member receipt is pinned by the digest its seal signs; the top receipt by its
signature, in the form MetaBot writes it; a journal by the hashes its receipt signs,
re-derived, in the form a bundle writes it; a workflow file and an approval receipt by
the sha256 the seal signs of each, and the approval by its own signature, its key the
seal's, over that workflow's sha256; ``bundle.json`` by what the rest re-derives. So
one byte changed in any file fails the check, and so does a file missing or one the
bundle does not list. A check reads no home, no key and no
network: it holds on any machine.

A bundle comes from someone else. Reading one writes nothing, follows no link inside
it, takes regular files alone, and stops at a limit of files and bytes.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import contextlib
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import stat
import sys
import tarfile
import zlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from emerge.criteria import criteria_digest, parse_policy_version
from emerge.record import hash_value
from emerge.run_attestation import verify_run_attestation

from metabot.approvals import printable
from metabot.cmdline import EXIT_FAILED, EXIT_OK, EXIT_USAGE
from metabot.paths import is_file, receipt_path, run_path, safe_id
from metabot.receipts import content_digest, content_pins
from metabot.seals import is_seal
from metabot.workflow_approval import APPROVE_CHECK, APPROVE_PREFIX, _detail
from metabot.workflow_firing import FIRING_CHECK, RUN_CHECK
from metabot.workflows import SHA_RE, SHORT, kept_path

BUNDLE_FORMAT = "metabot.bundle/1"
JOURNAL_FORMAT = "metabot.bundle-journal/1"
MANIFEST = "bundle.json"
RECEIPTS, JOURNALS = "receipts", "journals"
WORKFLOWS, APPROVALS = "workflows", "approvals"  # a workflow seal's file and its approval
KINDS = ("run", "office", "ladder", "workflow")
STEP_KINDS = ("ladder", "office")  # the seals a workflow's step makes, with members of their own
LINEAGE = ("sha256", "approval", "approval_receipt")  # what a workflow seal signs it ran under
DISCLOSED = frozenset({"prompt", "final", "task"})  # committed texts a bundle's journal shows
STEP_KEYS = ("call_id", "tool", "args", "output", "success", "latency_ms")
TOP_SUFFIX = ".bundle"  # a tarball holds one folder, <run id>.bundle

# What reading a bundle stops at.
MAX_FILES = 4096
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_TOTAL_BYTES = 128 * 1024 * 1024
MAX_DEPTH = 4  # parts of a name inside the bundle

_NONE = hash_value(None)  # a member its seal signs as having no receipt
_HEX64 = re.compile(r"[0-9a-f]{64}")
_GZIP = b"\x1f\x8b"
_MANIFEST_KEYS = frozenset({"format", "run_id", "kind", "files"})
_JOURNAL_KEYS = frozenset({"format", "run_id", "steps", "meta", "salts"})
_COMPARED = ("call_id", "tool", "success", "latency_ms")  # as the receipt signs them
# What a tar or gzip reader raises on bytes that are no archive.
_UNREADABLE = (tarfile.TarError, EOFError, OSError, zlib.error, ValueError, OverflowError)


class BundleError(Exception):
    """A bundle that cannot be read, and why, for whoever gave it.

    Not a ValueError: the tar reader passes its own errors through, never this one.
    """


@dataclass
class BundleReport:
    """What ``verify_bundle`` found: valid when there is no problem."""

    problems: list[str] = field(default_factory=list)
    run_id: str | None = None
    kind: str | None = None
    signer: str | None = None
    steps: int = 0
    members: int = 0  # with a receipt in the bundle, each checked
    unsealed: int = 0  # signed by the seal as having no receipt
    journals: int = 0
    withheld: list[str] = field(default_factory=list)
    workflow: tuple[str, str] | None = None  # a workflow seal's (name, sha256), checked
    approval: str | None = None  # the approval it ran under, checked
    firing: tuple[str, str] | None = None  # a firing's (number, routine), as its seal signs

    @property
    def valid(self) -> bool:
        return not self.problems


# ── forms ───────────────────────────────────────────────────────────────────


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json(data: bytes) -> Any:
    """``data`` read as UTF-8 JSON; ValueError when it is not, or nests past what reads."""
    try:
        return json.loads(data.decode("utf-8"))
    except RecursionError:
        raise ValueError("nested past what can be read") from None


def _is_safe(value: Any) -> bool:
    try:
        safe_id(value)
    except ValueError:
        return False
    return True


def _same(a: Any, b: Any) -> bool:
    """Equal, and of one type: ``1`` is not ``true``, nor ``1.0`` ``1``."""
    return type(a) is type(b) and a == b


def _receipt_text(envelope: Any) -> bytes:
    """A receipt as MetaBot writes it."""
    return (json.dumps(envelope, indent=2) + "\n").encode("ascii")


def _journal_text(journal: Any) -> bytes:
    """A bundle journal as a bundle writes it: keys sorted, so its bytes are fixed."""
    return (json.dumps(journal, indent=2, sort_keys=True) + "\n").encode("ascii")


def _manifest_text(run_id: str, kind: str, listed: Mapping[str, str]) -> bytes:
    body = {"format": BUNDLE_FORMAT, "run_id": run_id, "kind": kind, "files": dict(listed)}
    return (json.dumps(body, indent=2, sort_keys=True) + "\n").encode("ascii")


def _part(name: str) -> tuple[str, str] | None:
    """``(folder, run id)`` for ``receipts/<id>.json`` or ``journals/<id>.json``; else None."""
    folder, slash, rest = name.partition("/")
    if not slash or folder not in (RECEIPTS, JOURNALS) or not rest.endswith(".json"):
        return None
    run_id = rest[: -len(".json")]
    return (folder, run_id) if _is_safe(run_id) else None


def _receipt_name(run_id: str) -> str:
    return f"{RECEIPTS}/{run_id}.json"


def _journal_name(run_id: str) -> str:
    return f"{JOURNALS}/{run_id}.json"


def _canonical_b64(text: str) -> bool:
    """Whether ``text`` is base64 in its one written form: a decoder passes over the spare
    bits of the last character, so another form would decode to the same signature."""
    try:
        return base64.b64encode(base64.b64decode(text, validate=True)).decode("ascii") == text
    except (binascii.Error, ValueError):
        return False


def _receipt(data: bytes) -> tuple[dict[str, Any] | None, str | None]:
    """A receipt's envelope when it reads and verifies offline; else what is wrong."""
    try:
        envelope = _json(data)
    except ValueError:
        return None, "does not read as JSON"
    if not verify_run_attestation(envelope).valid:
        return None, "does not verify: its hashes or its signature do not hold"
    if not _canonical_b64(envelope["signature"]):
        return None, "its signature is not written in canonical base64"
    return envelope, None


def _kind_of(envelope: Any) -> tuple[str, str | None]:
    """A receipt's kind, from the ``seal`` verdict it signs: a receipt with none is a run's."""
    if not isinstance(envelope, dict) or not is_seal(envelope):
        return "run", None
    seal = next(v for v in envelope["verdicts"] if isinstance(v, dict) and v.get("check") == "seal")
    detail = seal.get("detail")
    head = detail.split(";", 1)[0] if isinstance(detail, str) else ""
    kind = head[len("kind=") :] if head.startswith("kind=") else ""
    if kind not in KINDS or kind == "run":
        return "run", f"a seal of a kind this metabot does not know ({printable(head, 60)})"
    return kind, None


# ── what workflows add ──────────────────────────────────────────────────────


def _workflow_name(run_id: str) -> str:
    return f"{WORKFLOWS}/{run_id}.json"


def _approval_name(run_id: str) -> str:
    return f"{APPROVALS}/{run_id}.json"


def lineage(top: Any) -> tuple[dict[str, str] | None, str | None]:
    """What a workflow seal signs it ran under: the workflow's sha256, the approval's id
    and its receipt's sha256, from its one ``workflow_run`` verdict; else what is wrong."""
    verdicts = top.get("verdicts") if isinstance(top, dict) else None
    runs = [v for v in verdicts or [] if isinstance(v, dict) and v.get("check") == RUN_CHECK]
    if len(runs) != 1:
        return None, f"a workflow's seal signs one {RUN_CHECK} verdict, and this signs {len(runs)}"
    detail = _detail(runs[0].get("detail")) if runs[0].get("result") == "pass" else None
    said = {key: (detail or {}).get(key, "") for key in LINEAGE}
    if not (
        SHA_RE.fullmatch(said["sha256"])
        and _is_safe(said["approval"])
        and said["approval"].startswith(f"{APPROVE_PREFIX}-")
        and _HEX64.fullmatch(said["approval_receipt"])
    ):
        return None, (
            f"its {RUN_CHECK} verdict does not name a workflow sha256, an approval and its "
            "receipt's sha256"
        )
    return said, None


def _firing(top: Mapping[str, Any], said: Mapping[str, str]) -> tuple[dict[str, str] | None, bool]:
    """A firing's ``workflow_firing`` verdict, and whether it holds: none at all holds, for a
    run by hand; one must name the workflow and the approval ``workflow_run`` names."""
    verdicts = top.get("verdicts") or []
    fired = [v for v in verdicts if isinstance(v, dict) and v.get("check") == FIRING_CHECK]
    if not fired:
        return None, True
    detail = _detail(fired[0].get("detail")) if fired[0].get("result") == "pass" else None
    if len(fired) != 1 or detail is None or any(detail.get(k) != said[k] for k in LINEAGE):
        return None, False
    return detail, True


def extra_parts(top: Any, files: Mapping[str, bytes]) -> dict[str, bytes]:
    """The files a bundle of ``top`` holds beyond its receipts and journals, read from this
    machine: for a workflow's seal, the workflow's bytes as the store keeps them and the
    approval's receipt. One not here is left out, and the check says it is missing."""
    kind, _problem = _kind_of(top)
    said = lineage(top)[0] if kind == "workflow" else None
    if said is None:
        return {}
    out: dict[str, bytes] = {}
    for name, path in (
        (_workflow_name(said["sha256"]), kept_path(said["sha256"])),
        (_approval_name(said["approval"]), receipt_path(said["approval"])),
    ):
        if is_file(path):
            out[name] = _home_bytes(path)
    return out


def _workflow_problems(
    top: dict[str, Any], files: Mapping[str, bytes], report: BundleReport, flag: Any
) -> set[str]:
    """Check a workflow seal's file and approval; the names of the files they are.

    Each claim is one the seal signs or a hash re-derived from the bundle's bytes. Whether
    the approval was revoked is not here to check: no revoke is in a bundle.
    """
    top_name = _receipt_name(top["run_id"])
    said, problem = lineage(top)
    if said is None:
        flag(top_name, str(problem))
        return set()
    fired, holds = _firing(top, said)
    if not holds:
        flag(
            top_name,
            f"its {FIRING_CHECK} verdict names another workflow or approval than its "
            f"{RUN_CHECK} verdict",
        )
    sha, approval = said["sha256"], said["approval"]
    workflow, signed = _workflow_name(sha), _approval_name(approval)
    good = True
    data = files.get(workflow)
    if data is None:
        flag(workflow, "missing, and the seal signs its sha256")
        good = False
    elif _sha(data) != sha:
        flag(workflow, "not the bytes of the sha256 the seal signs")
        good = False
    problem = _approval_problem(files.get(signed), said, top["signer"])
    if problem is not None:
        flag(signed, problem)
        good = False
    if good and holds:
        seal = next(v for v in top["verdicts"] if isinstance(v, dict) and v.get("check") == "seal")
        name = (_detail(seal.get("detail")) or {}).get("name", "?")
        report.workflow, report.approval = (printable(name, 64), sha), approval
        if fired is not None:
            number, routine = fired.get("firing", "?"), fired.get("routine", "?")
            report.firing = (printable(number, 20), printable(routine, 40))
    return {workflow, signed}


def _approval_problem(
    data: bytes | None, said: Mapping[str, str], signer: Mapping[str, Any]
) -> str | None:
    """What does not hold of the approval receipt a workflow seal names; None when it holds.

    ``signer`` is the seal's. Its key is the public key it signs with, not its DID: a DID is
    a name any key can write, and a signature is checked against the key alone.
    """
    if data is None:
        return "missing, and the seal names it the approval its run went under"
    if _sha(data) != said["approval_receipt"]:
        return "not the approval receipt the seal signs by its sha256"
    envelope, why = _receipt(data)
    if envelope is None:
        return str(why)
    if envelope["run_id"] != said["approval"]:
        return f"the receipt of run {printable(envelope['run_id'], 130)}"
    theirs = envelope["signer"]
    if theirs["public_key_b64"] != signer["public_key_b64"] or theirs["did"] != signer["did"]:
        return "signed by another key than the seal's"
    verdicts = envelope.get("verdicts") or []
    approves = [v for v in verdicts if isinstance(v, dict) and v.get("check") == APPROVE_CHECK]
    detail = None
    if len(approves) == 1 and approves[0].get("result") == "pass":
        detail = _detail(approves[0].get("detail"))
    if detail is None or detail.get("sha256") != said["sha256"]:
        return "it does not approve the workflow sha256 the seal signs"
    return None


# ── verify ──────────────────────────────────────────────────────────────────


def verify_bundle(files: Mapping[str, bytes]) -> BundleReport:
    """Check a bundle given as ``{name: bytes}``. Nothing is read from disk or the network."""
    report = BundleReport()
    _verify(files, report)
    return report


def _verify(files: Mapping[str, bytes], report: BundleReport) -> None:
    problems = report.problems
    raw = files.get(MANIFEST)
    if raw is None:
        problems.append(f"{MANIFEST}: missing, so this is not a bundle")
        return
    manifest, problem = _read_manifest(raw)
    if manifest is None:
        problems.append(f"{MANIFEST}: {problem}")
        return
    run_id = manifest["run_id"]
    report.run_id = run_id
    top_name = _receipt_name(run_id)
    data = files.get(top_name)
    if data is None:
        problems.append(f"{top_name}: missing, and {MANIFEST} names it the bundle's run")
        return
    top, problem = _read_top(data, run_id)
    if top is None:
        problems.append(f"{top_name}: {problem}")
        return
    report.signer, report.steps = top["signer"]["did"], len(top["steps"])
    kind, problem = _kind_of(top)
    report.kind = kind
    troubled: set[str] = set()  # the files a problem names already

    def flag(name: str, text: str, *more: str) -> None:
        troubled.update((name, *more))
        problems.append(f"{printable(name, 300)}: {text}")

    if problem is not None:
        flag(top_name, problem)
    accounted = _workflow_problems(top, files, report, flag) if kind == "workflow" else set()
    for name in sorted(files):
        if name != MANIFEST and name not in accounted and _part(name) is None:
            flag(name, f"a file a {BUNDLE_FORMAT} bundle does not have")
    held: dict[str, dict[str, Any]] = {run_id: top}  # receipts here that hold, by run id
    listed = {run_id}  # the receipts the bundle's run holds, here or not
    if is_seal(top):
        _members(top, files, report, held, listed, troubled)
    for member, step_seal in list(held.items()) if kind == "workflow" else []:
        if member == run_id or not is_seal(step_seal):
            continue
        if _kind_of(step_seal)[0] not in STEP_KINDS:
            flag(_receipt_name(member), "a seal of a kind no workflow step makes")
            continue
        _members(step_seal, files, report, held, listed, troubled)
    for name in sorted(files):
        part = _part(name)
        if part is None or part[0] != RECEIPTS or name in troubled or part[1] in listed:
            continue
        flag(name, "a receipt this bundle's run does not hold", _journal_name(part[1]))
    withheld: set[str] = set()
    for name in sorted(files):
        part = _part(name)
        if part is None or part[0] != JOURNALS or name in troubled:
            continue
        envelope = held.get(part[1])
        if envelope is None:
            flag(name, "a journal of no receipt this bundle holds")
            continue
        problem, kept_back = _journal_problem(files[name], part[1], envelope)
        if problem is not None:
            flag(name, problem)
            continue
        report.journals += 1
        withheld |= kept_back
    report.withheld = sorted(withheld)
    _manifest_problems(files, manifest, kind, troubled, problems)


def _read_manifest(raw: bytes) -> tuple[dict[str, Any] | None, str | None]:
    try:
        manifest = _json(raw)
    except ValueError:
        return None, "does not read as JSON"
    if not isinstance(manifest, dict):
        return None, f"not a {BUNDLE_FORMAT} manifest"
    said = manifest.get("format")
    if isinstance(said, str) and said != BUNDLE_FORMAT and said.startswith("metabot.bundle/"):
        return None, f"a {printable(said, 40)} bundle, and this metabot reads {BUNDLE_FORMAT}"
    if said != BUNDLE_FORMAT or set(manifest) != _MANIFEST_KEYS:
        return None, f"not a {BUNDLE_FORMAT} manifest: it holds format, run_id, kind and files"
    if not _is_safe(manifest["run_id"]):
        return None, "its run_id is no run id"
    if manifest["kind"] not in KINDS:
        return None, f"its kind is none of {', '.join(KINDS)}"
    listed = manifest["files"]
    if not isinstance(listed, dict) or not all(
        isinstance(v, str) and _HEX64.fullmatch(v) for v in listed.values()
    ):
        return None, "its files are not names, each with its sha256"
    return manifest, None


def _read_top(data: bytes, run_id: str) -> tuple[dict[str, Any] | None, str | None]:
    envelope, problem = _receipt(data)
    if envelope is None:
        return None, problem
    if envelope["run_id"] != run_id:
        return None, f"the receipt of run {printable(envelope['run_id'], 130)}, not {run_id}"
    if data != _receipt_text(envelope):
        return None, "not in the form metabot writes a receipt"
    return envelope, None


def _members(
    top: dict[str, Any],
    files: Mapping[str, bytes],
    report: BundleReport,
    held: dict[str, dict[str, Any]],
    listed: set[str],
    troubled: set[str],
) -> None:
    """What ``seals.member_problems`` checks, against the bundle and never this machine."""
    seen: set[str] = set()
    for step in top["steps"]:
        said = step["call_id"]
        if not _is_safe(said):
            report.problems.append(f"{printable(said, 130)}: not a run id")
            continue
        if step["args_hash"] != hash_value({"run_id": said}):
            report.problems.append(f"{said}: its step does not name it")
            continue
        if said in seen:
            report.problems.append(f"{said}: the seal lists it twice")
            continue
        seen.add(said)
        name = _receipt_name(said)
        data = files.get(name)
        problem = None
        if step["output_hash"] == _NONE:
            report.unsealed += 1
            if data is None:
                continue
            problem = "the seal says it has no receipt, but one is here"
        else:
            listed.add(said)
            if data is None:
                problem = "its receipt is missing"
            elif hash_value(_sha(data)) != step["output_hash"]:
                problem = "its receipt is not the one sealed"
            else:
                envelope, _why = _receipt(data)
                if envelope is None:
                    problem = "its receipt does not verify"
                elif envelope["run_id"] != said:
                    problem = f"its receipt is run {printable(envelope['run_id'], 130)}'s"
                else:
                    held[said] = envelope
                    report.members += 1
                    continue
        troubled.update((name, _journal_name(said)))
        report.problems.append(f"{said}: {problem}")


def _journal_problem(
    data: bytes, run_id: str, envelope: Mapping[str, Any]
) -> tuple[str | None, set[str]]:
    """What does not hold between a bundle journal and its receipt; and the texts the
    receipt commits to that the journal withholds."""
    try:
        journal = _json(data)
    except ValueError:
        return "does not read as JSON", set()
    shape = set(journal) if isinstance(journal, dict) else set()
    if not _JOURNAL_KEYS <= shape <= _JOURNAL_KEYS | {"criteria"}:
        return f"not a {JOURNAL_FORMAT} journal", set()
    if journal["format"] != JOURNAL_FORMAT:
        return f"not a {JOURNAL_FORMAT} journal", set()
    if not _same(journal["run_id"], run_id):
        return "the journal of another run", set()
    try:
        if _journal_text(journal) != data:
            return "not in the form a bundle writes a journal", set()
        problem = _steps_problem(journal["steps"], envelope["steps"])
        if problem is None:
            problem = _texts_problem(journal, envelope)
    except RecursionError:  # a value nested as deep as a parser goes
        return "nested past what can be read", set()
    if problem is not None:
        return problem, set()
    return None, set(content_pins(envelope["verdicts"]) or {}) - set(journal["meta"])


def _steps_problem(kept: Any, sealed: list[dict[str, Any]]) -> str | None:
    if not isinstance(kept, list) or not all(
        isinstance(step, dict) and set(step) == set(STEP_KEYS) for step in kept
    ):
        return "its steps are not what a bundle journal holds"
    if len(kept) != len(sealed):
        return f"it holds {len(kept)} steps, and its receipt signs {len(sealed)}"
    for n, (k, s) in enumerate(zip(kept, sealed, strict=True), start=1):
        for key in _COMPARED:
            if not _same(k[key], s[key]):
                return f"step {n}'s {key} is not the one its receipt signs"
        if hash_value(k["args"]) != s["args_hash"]:
            return f"step {n}'s args do not re-derive the hash its receipt signs"
        if hash_value(k["output"]) != s["output_hash"]:
            return f"step {n}'s output does not re-derive the hash its receipt signs"
    return None


def _texts_problem(journal: Mapping[str, Any], envelope: Mapping[str, Any]) -> str | None:
    """Whether each text the journal shows re-derives the digest its receipt signs, and its
    criteria the digest its receipt's policy carries."""
    pins = content_pins(envelope["verdicts"])
    if pins is None:
        return "its receipt's content verdict does not read, so no text of it can be checked"
    meta, salts = journal["meta"], journal["salts"]
    if not isinstance(meta, dict) or not isinstance(salts, dict) or set(meta) != set(salts):
        return "its meta and its salts do not name the same texts"
    for name in sorted(meta):
        if name not in pins:
            return f"its {printable(name, 40)} is not a text its receipt signs"
        salt = salts[name]
        if not (isinstance(salt, str) and _HEX64.fullmatch(salt)):
            return f"its salt for {name} is not 64 hex digits"
        if content_digest(name, meta[name], salt) != pins[name]:
            return f"its {name} does not re-derive the digest its receipt signs"
    _policy, digest = parse_policy_version(envelope["policy_version"])
    if digest is None:
        if "criteria" in journal:
            return "it holds criteria its receipt does not declare"
        return None
    criteria = journal.get("criteria")
    if not isinstance(criteria, dict) or criteria_digest(criteria) != digest:
        return "its criteria do not re-derive the digest its receipt's policy signs"
    return None


def _manifest_problems(
    files: Mapping[str, bytes],
    manifest: Mapping[str, Any],
    kind: str,
    troubled: set[str],
    problems: list[str],
) -> None:
    """``bundle.json`` must be what the rest of the bundle re-derives, byte for byte."""
    present = {name: _sha(data) for name, data in files.items() if name != MANIFEST}
    if _manifest_text(manifest["run_id"], kind, present) == files[MANIFEST]:
        return
    listed: dict[str, str] = manifest["files"]
    differs = False
    if manifest["kind"] != kind:
        problems.append(
            f"{MANIFEST}: it says {manifest['kind']}, and its run's receipt is a {kind}'s"
        )
        differs = True
    for name in sorted(set(listed) | set(present)):
        if listed.get(name) == present.get(name):
            continue
        differs = True
        if name in troubled:  # its own problem says why
            continue
        if name not in present:
            problems.append(
                f"{printable(name, 300)}: {MANIFEST} lists it, and the bundle does not hold it"
            )
        elif name not in listed:
            problems.append(
                f"{printable(name, 300)}: in the bundle, and {MANIFEST} does not list it"
            )
        else:
            problems.append(f"{printable(name, 300)}: not the file {MANIFEST} lists")
    if not differs:
        problems.append(f"{MANIFEST}: not in the form metabot writes it")


# ── read ────────────────────────────────────────────────────────────────────


def read_bundle(path: Path | str) -> dict[str, bytes]:
    """A bundle's files as ``{name: bytes}``, from a folder or a tarball, gzipped or plain.

    Nothing is written and no link inside it is followed; reading stops at a limit of
    files and bytes. BundleError says what is wrong.
    """
    path = Path(path)
    try:
        mode = os.stat(path).st_mode
    except OSError as exc:
        raise BundleError(f"the bundle: cannot be read ({exc.strerror})") from None
    if stat.S_ISDIR(mode):
        return _read_folder(path)
    if not stat.S_ISREG(mode):
        raise BundleError("the bundle: neither a folder nor a file")
    return _read_tarball(path)


def _read_folder(path: Path) -> dict[str, bytes]:
    try:
        top = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    except OSError as exc:
        raise BundleError(f"the folder: cannot be read ({exc.strerror})") from None
    files: dict[str, bytes] = {}
    try:
        _walk(top, "", 1, files, [0])
    finally:
        os.close(top)
    return files


def _walk(dir_fd: int, prefix: str, depth: int, files: dict[str, bytes], total: list[int]) -> None:
    try:
        names = sorted(os.listdir(dir_fd))
    except OSError as exc:
        raise BundleError(
            f"{printable(prefix or 'the folder', 300)}: cannot be read ({exc.strerror})"
        ) from None
    for name in names:
        rel = prefix + name
        shown = printable(rel, 300)
        try:
            mode = os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode
        except OSError as exc:
            raise BundleError(f"{shown}: cannot be read ({exc.strerror})") from None
        if stat.S_ISLNK(mode):
            raise BundleError(f"{shown}: a link; a bundle holds files")
        if stat.S_ISDIR(mode):
            if depth >= MAX_DEPTH:
                raise BundleError(f"{shown}: nested deeper than a bundle goes")
            try:
                fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)
            except OSError as exc:
                raise BundleError(f"{shown}: cannot be read ({exc.strerror})") from None
            try:
                _walk(fd, rel + "/", depth + 1, files, total)
            finally:
                os.close(fd)
            continue
        if not stat.S_ISREG(mode):
            raise BundleError(f"{shown}: not a regular file; a bundle holds files")
        if len(files) >= MAX_FILES:
            raise BundleError(f"the folder: more than {MAX_FILES} files")
        data = _read_at(dir_fd, name, shown)
        total[0] += len(data)
        if total[0] > MAX_TOTAL_BYTES:
            raise BundleError(f"the folder: more than {MAX_TOTAL_BYTES} bytes in all")
        files[rel] = data


def _read_at(dir_fd: int, name: str, shown: str) -> bytes:
    """A regular file in a folder, read up to the limit; never a link or a pipe."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    except OSError as exc:
        raise BundleError(f"{shown}: cannot be read ({exc.strerror})") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise BundleError(f"{shown}: not a regular file; a bundle holds files")
        chunks: list[bytes] = []
        size = 0
        while chunk := os.read(fd, min(1 << 20, MAX_FILE_BYTES + 1 - size)):
            size += len(chunk)
            if size > MAX_FILE_BYTES:
                raise BundleError(f"{shown}: larger than {MAX_FILE_BYTES} bytes")
            chunks.append(chunk)
        return b"".join(chunks)
    except OSError as exc:
        raise BundleError(f"{shown}: cannot be read ({exc.strerror})") from None
    finally:
        os.close(fd)


class _Capped:
    """A stream read through a cap: past ``limit`` bytes it raises BundleError."""

    def __init__(self, raw: Any, limit: int) -> None:
        self._raw, self._limit, self._count = raw, limit, 0

    def read(self, size: int | None = -1) -> bytes:
        room = self._limit + 1 - self._count
        data: bytes = self._raw.read(room if size is None or size < 0 else min(size, room))
        self._count += len(data)
        if self._count > self._limit:
            raise BundleError(f"the tarball: more than {self._limit} bytes once unpacked")
        return data

    def drain(self) -> None:
        """Read to the end, where a gzip's length and checksum are checked."""
        while self.read(1 << 16):
            pass


def _unpacked_limit() -> int:
    """What a tarball may unpack to: its files, and headers and padding for each entry."""
    return MAX_TOTAL_BYTES + 2 * MAX_FILES * 4 * tarfile.BLOCKSIZE + 2 * tarfile.RECORDSIZE


def _read_tarball(path: Path) -> dict[str, bytes]:
    try:
        fh = open(path, "rb")  # noqa: SIM115 -- closed below, after the gzip over it
    except OSError as exc:
        raise BundleError(f"the tarball: cannot be read ({exc.strerror})") from None
    with fh:
        try:
            gzipped = fh.read(2) == _GZIP
            fh.seek(0)
            with contextlib.ExitStack() as stack:
                stream: Any = fh
                if gzipped:
                    stream = stack.enter_context(gzip.GzipFile(fileobj=fh, mode="rb"))
                capped = _Capped(stream, _unpacked_limit())
                files = _tar_members(capped)
                capped.drain()
        except _UNREADABLE as exc:
            why = printable(str(exc) or type(exc).__name__, 120)
            raise BundleError(
                f"the tarball: no tar archive, gzipped or plain, reads from it ({why})"
            ) from None
    return files


def _tar_members(stream: _Capped) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    seen: set[str] = set()
    top: str | None = None
    total = folders = 0
    with tarfile.open(fileobj=stream, mode="r|") as tar:  # type: ignore[call-overload]
        for member in tar:
            name = member.name
            shown = printable(name, 300)
            if name.startswith("/"):
                raise BundleError(f"the tarball: {shown} is a name from the root")
            parts = name.split("/")
            if ".." in parts:
                raise BundleError(f"the tarball: {shown} climbs out of its folder")
            if any(part in ("", ".") for part in parts):
                raise BundleError(f"the tarball: {shown} has an empty part")
            top = parts[0] if top is None else top
            if parts[0] != top:
                raise BundleError(
                    f"the tarball: {shown} is outside its one folder, {printable(top, 140)}"
                )
            if len(parts) > MAX_DEPTH + 1:
                raise BundleError(f"the tarball: {shown} is nested deeper than a bundle goes")
            if name in seen:
                raise BundleError(f"the tarball: {shown} is in it twice")
            seen.add(name)
            if member.issym() or member.islnk():
                raise BundleError(f"the tarball: {shown} is a link; a bundle holds files")
            if member.isdir():
                folders += 1
                if folders > MAX_FILES:
                    raise BundleError(f"the tarball: more than {MAX_FILES} folders")
                continue
            if not member.isfile() or member.issparse():
                raise BundleError(
                    f"the tarball: {shown} is not a regular file; a bundle holds files"
                )
            rel = "/".join(parts[1:])
            if not rel:
                raise BundleError(
                    f"the tarball: {shown} is a file, and a bundle's tarball holds one folder"
                )
            if len(files) >= MAX_FILES:
                raise BundleError(f"the tarball: more than {MAX_FILES} files")
            if not 0 <= member.size <= MAX_FILE_BYTES:
                raise BundleError(
                    f"the tarball: {printable(rel, 300)} is larger than {MAX_FILE_BYTES} bytes"
                )
            if total + member.size > MAX_TOTAL_BYTES:
                raise BundleError(f"the tarball: more than {MAX_TOTAL_BYTES} bytes in all")
            got = tar.extractfile(member)
            if got is None:
                raise BundleError(
                    f"the tarball: {shown} is not a regular file; a bundle holds files"
                )
            with got:
                data = got.read()
            if len(data) != member.size:
                raise BundleError(f"the tarball: {shown} is cut short")
            total += len(data)
            files[rel] = data
    return files


# ── export ──────────────────────────────────────────────────────────────────


def _home_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise BundleError(
            f"{printable(str(path), 2000)}: cannot be read ({exc.strerror})"
        ) from None


def _bundle_journal(run_id: str, home: Any, envelope: Any) -> bytes:
    """A journal kept here, cut to what its receipt hashes: its steps, the texts a bundle
    shows with their salts, and its criteria. No path, login or field outside a hash."""
    if not isinstance(home, dict) or not isinstance(envelope, dict):
        raise ValueError("not a journal and a receipt")
    pins = content_pins(envelope.get("verdicts")) or {}
    shown = sorted(name for name in pins if name in DISCLOSED)
    meta: dict[str, Any] = home["meta"] if isinstance(home.get("meta"), dict) else {}
    salts: dict[str, Any] = home["salts"] if isinstance(home.get("salts"), dict) else {}
    steps = home.get("steps")
    if isinstance(steps, list):  # another shape is kept as it is, and fails the check
        steps = [{k: s.get(k) for k in STEP_KEYS} if isinstance(s, dict) else s for s in steps]
    journal: dict[str, Any] = {
        "format": JOURNAL_FORMAT,
        "run_id": run_id,
        "steps": steps,
        "meta": {name: meta.get(name) for name in shown},
        "salts": {name: salts.get(name) for name in shown},
    }
    if "criteria" in home:
        journal["criteria"] = home["criteria"]
    return _journal_text(journal)


def _add_members(seal: Any, files: dict[str, bytes], ids: list[str]) -> None:
    """Each member receipt ``seal`` signs that this machine keeps, into ``files`` and ``ids``."""
    steps = seal.get("steps") if isinstance(seal, dict) and is_seal(seal) else None
    for step in steps if isinstance(steps, list) else []:
        member = step.get("call_id") if isinstance(step, dict) else None
        if not isinstance(member, str) or not _is_safe(member) or member in ids:
            continue
        path = receipt_path(member)
        if step.get("output_hash") != _NONE and is_file(path):  # one not here: it is missing
            files[_receipt_name(member)] = _home_bytes(path)
            ids.append(member)


def build_bundle(run_id: str, *, journals: bool = False) -> dict[str, bytes]:
    """The files of ``run_id``'s bundle, from what this machine keeps; nothing is written.

    It is not checked here: ``verify_bundle`` says whether it holds.
    """
    top_bytes = _home_bytes(receipt_path(run_id))
    files = {_receipt_name(run_id): top_bytes}
    top = _try_json(top_bytes)
    ids = [run_id]
    _add_members(top, files, ids)
    if _kind_of(top)[0] == "workflow":  # a step's ladder or office run brings its members
        for member in ids[1:]:
            envelope = _try_json(files[_receipt_name(member)])
            if _kind_of(envelope)[0] in STEP_KINDS:
                _add_members(envelope, files, ids)
    for each in ids if journals else []:
        path = run_path(each) / "journal.json"
        if not is_file(path):
            continue
        try:
            home = _json(_home_bytes(path))
            files[_journal_name(each)] = _bundle_journal(
                each, home, _try_json(files[_receipt_name(each)])
            )
        except (ValueError, RecursionError):
            raise BundleError(
                f"{_journal_name(each)}: the journal kept here does not read"
            ) from None
    files.update(extra_parts(top, files))
    kind, _problem = _kind_of(top)
    files[MANIFEST] = _manifest_text(run_id, kind, {n: _sha(d) for n, d in files.items()})
    return files


def _past_limits(files: Mapping[str, bytes]) -> str | None:
    """Why a bundle could not be read back, as ``read_bundle`` stops at its limits."""
    if len(files) > MAX_FILES:
        return f"it holds more than {MAX_FILES} files"
    big = next((name for name, data in sorted(files.items()) if len(data) > MAX_FILE_BYTES), None)
    if big is not None:
        return f"{big} is larger than {MAX_FILE_BYTES} bytes"
    if sum(len(data) for data in files.values()) > MAX_TOTAL_BYTES:
        return f"it holds more than {MAX_TOTAL_BYTES} bytes in all"
    return None


def _stamp(envelope: Any) -> int:
    """When a receipt says its run finished, as its tarball's time: one bundle, one set of bytes."""
    finished = envelope.get("finished_at") if isinstance(envelope, dict) else None
    try:
        when = datetime.strptime(str(finished), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        return max(0, int(when.timestamp()))
    except (ValueError, OverflowError):
        return 0


def _entry(name: str, kind: bytes, mode: int, mtime: int, size: int = 0) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type, info.mode, info.mtime, info.size = kind, mode, mtime, size
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def _tarball_bytes(top: str, files: Mapping[str, bytes], mtime: int, gzipped: bool) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        folders = sorted({name.split("/")[0] for name in files if "/" in name})
        for folder in [top, *(f"{top}/{f}" for f in folders)]:
            tar.addfile(_entry(folder, tarfile.DIRTYPE, 0o700, mtime))
        for name in sorted(files):
            data = files[name]
            tar.addfile(
                _entry(f"{top}/{name}", tarfile.REGTYPE, 0o600, mtime, len(data)), io.BytesIO(data)
            )
    body = raw.getvalue()
    return gzip.compress(body, mtime=mtime) if gzipped else body


def _write_new(path: Path, data: bytes) -> None:
    """Write a file that is not there yet, owner-only."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as fh:
        os.fchmod(fh.fileno(), 0o600)
        fh.write(data)


def _write_folder(out: Path, files: Mapping[str, bytes]) -> None:
    os.mkdir(out, 0o700)  # it is not there: never a folder someone has
    try:
        os.chmod(out, 0o700)
        for name in sorted(files):
            target = out.joinpath(*name.split("/"))
            if not target.parent.is_dir():
                os.mkdir(target.parent, 0o700)
                os.chmod(target.parent, 0o700)
            _write_new(target, files[name])
    except BaseException:
        shutil.rmtree(out, ignore_errors=True)  # what this made, and nothing it did not
        raise


def _write_tarball(out: Path, run_id: str, files: Mapping[str, bytes], gzipped: bool) -> None:
    mtime = _stamp(_try_json(files[_receipt_name(run_id)]))
    data = _tarball_bytes(run_id + TOP_SUFFIX, files, mtime, gzipped)
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            os.fchmod(fh.fileno(), 0o600)
            fh.write(data)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(out)
        raise


def _try_json(data: bytes) -> Any:
    try:
        return _json(data)
    except ValueError:
        return None


def _container(out: Path) -> str:
    """What ``--out`` names: a gzipped tarball, a plain one, or a folder."""
    name = out.name.lower()
    if name.endswith((".tar.gz", ".tgz")):
        return "gzip"
    return "tar" if name.endswith(".tar") else "folder"


def _err(text: str) -> None:
    print(f"metabot: {text}", file=sys.stderr)


def cmd_receipts_export(args: argparse.Namespace) -> int:
    run_id = args.run
    try:
        found = _is_safe(run_id) and is_file(receipt_path(run_id))
    except OSError:
        found = False
    if not found:
        _err(
            f"no receipt {printable(str(run_id), 130)} here "
            "(give a run, an office run, a ladder, a workflow run or a firing)"
        )
        return EXIT_USAGE
    out = Path(args.out) if args.out else Path(f"{run_id}{TOP_SUFFIX}.tar.gz")
    shown = printable(str(out), 2000)
    if os.path.lexists(out):
        _err(f"{shown} is there already; give --out a name that is not")
        return EXIT_USAGE
    if not os.path.isdir(out.parent):
        _err(f"no folder {printable(str(out.parent), 2000)} to write {shown} in")
        return EXIT_USAGE
    try:
        files = build_bundle(run_id, journals=args.journals)
    except BundleError as exc:
        return _refused(run_id, [str(exc)])
    report = verify_bundle(files)
    if not report.valid:
        return _refused(run_id, report.problems)
    too_big = _past_limits(files)
    if too_big is not None:
        return _refused(run_id, [f"the bundle: {too_big}, past what a bundle reads"])
    container = _container(out)
    try:
        if container == "folder":
            _write_folder(out, files)
        else:
            _write_tarball(out, run_id, files, container == "gzip")
    except FileExistsError:
        _err(f"{shown} is there already; give --out a name that is not")
        return EXIT_USAGE
    except OSError as exc:
        _err(f"cannot write {shown} ({exc.strerror})")
        return EXIT_FAILED
    _say_written(shown, report)
    return EXIT_OK


def _revoked_here(report: BundleReport) -> bool:
    """Whether this machine takes the approval a workflow bundle names as revoked now."""
    from metabot.workflow_approval import approval_state

    if report.workflow is None or report.approval is None:
        return False
    try:
        state = approval_state(report.workflow[1])
    except (OSError, ValueError):
        return True  # what cannot be read is taken as revoked, as a run takes it
    return bool(state.unreadable_revokes) or report.approval in {a.run_id for a in state.revoked}


def _refused(run_id: str, problems: list[str]) -> int:
    _err(f"{run_id} is not exported: what this machine keeps of it does not hold")
    for problem in problems:
        print(f"  {problem}", file=sys.stderr)
    if any(problem.startswith(f"{JOURNALS}/") for problem in problems):
        _err("leave out --journals to export the receipts alone")
    return EXIT_FAILED


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" + ("" if count == 1 else "s")


def _say_written(shown: str, report: BundleReport) -> None:
    print(f"wrote {shown}")
    if report.kind == "run":
        held = f"run {report.run_id}: its receipt"
    else:
        members = _plural(report.members, "member receipt")
        held = f"{report.kind} {report.run_id}: its seal and {members}"
    print(f"  {held}, {_plural(report.journals, 'journal') if report.journals else 'no journals'}")
    if report.workflow is not None:
        print(
            f"  the workflow file {report.workflow[1][:SHORT]} and its approval {report.approval}"
        )
        if _revoked_here(report):
            print(
                f"  this machine takes approval {report.approval} as revoked now; "
                "the bundle holds no revoke"
            )
    if report.journals:
        print("  the journals hold the prompts, answers and tool calls as kept")
        print("  leave out --journals to export the receipts alone, which hold hashes")
    else:
        print("  the receipts hold hashes, and no text")
        print("  --journals puts in the prompts, answers and tool calls as kept")
    if report.withheld:
        withheld = ", ".join(report.withheld)
        print(f"  withheld: {withheld}, signed as salted digests; the texts stay here")
    print(f"next: metabot receipts verify --chain {shown}")


# ── verify --chain ──────────────────────────────────────────────────────────


def report_lines(report: BundleReport) -> list[str]:
    """What ``verify --chain`` prints under its verdict."""
    if not report.valid:
        return [f"  {problem}" for problem in report.problems]
    signer = printable(str(report.signer), 300)
    if report.kind == "run":
        lines = [f"  run {report.run_id}, signed by {signer}, {_plural(report.steps, 'step')}"]
    else:
        lines = [f"  {report.kind} {report.run_id}, signed by {signer}"]
        count = report.members + report.unsealed
        if report.unsealed:
            lines.append(
                f"  {count} members: {report.members} present, unchanged, and valid; "
                f"{report.unsealed} sealed with no receipt"
            )
        else:
            lines.append(f"  {count} members: each present, unchanged, and valid")
    if report.workflow is not None:
        name, sha = report.workflow
        lines += [
            f"  workflow {name} {sha[:SHORT]}: its file hashes to the sha256 the seal signs",
            f"  approval {report.approval}: signed by the same key, over that sha256, and its "
            "receipt hashes to the digest the seal signs",
        ]
        if report.firing is not None:
            number, routine = report.firing
            lines.append(
                f"  firing {number} of routine {routine}, under the same workflow and approval"
            )
        lines.append(
            "  not shown: whether that approval was revoked; a revoke stays on the machine that "
            "signed it, and no bundle holds one"
        )
    if report.journals == 1:
        lines.append("  1 journal: it re-derives every hash its receipt signs")
    elif report.journals:
        lines.append(f"  {report.journals} journals: each re-derives every hash its receipt signs")
    else:
        lines.append("  no journals")
    if report.withheld:
        lines.append(
            f"  withheld: {', '.join(report.withheld)} (each receipt signs a salted digest)"
        )
    return lines


def cmd_verify_chain(args: argparse.Namespace) -> int:
    path = Path(args.ref)
    shown = printable(str(path), 2000)
    if not os.path.lexists(path):
        _err(f"no bundle at {shown} (give a folder or a tarball that receipts export wrote)")
        return EXIT_USAGE
    try:
        files = read_bundle(path)
    except BundleError as exc:
        print(f"INVALID  {shown}")
        print(f"  {exc}")
        return EXIT_FAILED
    report = verify_bundle(files)
    print(f"{'VALID' if report.valid else 'INVALID'}  {shown}")
    for line in report_lines(report):
        print(line)
    return EXIT_OK if report.valid else EXIT_FAILED
