"""Seal a run into an ``orcha.run-attestation/v1`` receipt and keep its journal.

The receipt holds hashes only. The journal beside it holds what was hashed.
Every known secret is replaced by its marker *before* hashing, so the
journal re-derives every hash in the receipt and no secret reaches disk.

Verdicts mirror the platform's shapes: the model as a ``pass`` with
``<engine>/<model>``; an approval as ``pass`` naming the approver; a decline
as ``warn`` with ``declined`` (never ``fail``: a decline is the gate working,
not the run failing).
"""

from __future__ import annotations

import base64
import contextlib
import errno
import hashlib
import json
import os
import re
import stat
import tempfile
import time
from collections.abc import Mapping, Sequence
from itertools import islice
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from emerge.criteria import compose_policy_version, criteria_digest
from emerge.journal import declared_acceptance_verdict
from emerge.preimage import output_preimage, redact_credentials
from emerge.record import (
    EphemeralKeyRefused,
    hash_value,
    key_did,
    load_signing_key,
    resolve_key_path,
    seal_run,
)
from emerge.run_attestation import AttestationVerdict, verify_run_attestation

from metabot.gitcmd import toplevel
from metabot.paths import metabot_home, receipts_dir, run_dir, run_path, safe_id, write_private
from metabot.steps import ascii_text

POLICY_VERSION = "metabot/1.0"
# where the orcha-record plugin keeps its receipts too
PROJECT_RECEIPTS = Path(".orcha") / "receipts"
PROJECT_MODE = 0o600  # as the home copy; git keeps no read bits, so a commit shares it the same
JOURNAL_FORMAT = "metabot.journal/1"
INDEX_TEXT_LIMIT = 200
KEY_WAIT_S = 2.0  # how long a run waits for a key another run is still writing


class KeyFileError(ValueError):
    """The signing key cannot be read or made, so nothing can be signed; the command stops."""


def _reason(exc: BaseException, path: Path) -> str:
    text = str(exc)  # the SDK starts with the path, which the message names once
    return "it" + text[len(str(path)) :] if text.startswith(str(path)) else text


def ensure_key() -> tuple[Ed25519PrivateKey, str]:
    """The holder's signing key, created on first use (mode 0600).

    Runs that start at once on a new machine all sign with one key: a new key
    is written whole to a file of its own and then linked into place, which
    fails when another run's key is there first, and that key is used. Where
    the filesystem keeps no hard links, the key file is made with an exclusive
    create instead, and a run that finds it waits for it to be whole.

    A key file that is there but does not read is never replaced: the
    receipts it signed would then name a signer nobody holds.
    """
    path = resolve_key_path()
    try:
        key = _whole_key(path)
    except EphemeralKeyRefused:
        if path.exists() or path.is_symlink():  # there, but no file a key is read from
            raise KeyFileError(f"cannot read the signing key {path}: it is not a file") from None
        try:
            key = _new_key(path)
        except (ValueError, OSError) as exc:
            raise KeyFileError(
                f"cannot make the signing key {path}: {_reason(exc, path)}"
            ) from None
    except (ValueError, OSError) as exc:
        raise KeyFileError(f"cannot read the signing key {path}: {_reason(exc, path)}") from None
    return key, key_did(key)


def key_problem(path: Path) -> str | None:
    """Why the key file at ``path`` cannot sign, or None when it can. It never makes one."""
    if not path.is_file():
        return "it is not a file"
    try:
        load_signing_key(path)
    except (ValueError, OSError) as exc:
        return _reason(exc, path)
    return None


def _whole_key(path: Path) -> Ed25519PrivateKey:
    """The key at ``path``, waiting a moment for one another run is still writing."""
    deadline = time.monotonic() + KEY_WAIT_S
    while True:
        try:
            return load_signing_key(path)
        except ValueError:  # not a whole seed yet; or never, and then it is refused
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.02)


def _new_key(path: Path) -> Ed25519PrivateKey:
    """A new key at ``path``, or the key another run put there first.

    A key folder MetaBot makes is 0700, whatever the umask; one that is there
    already, such as a folder the person chose, keeps its mode.
    """
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    key = Ed25519PrivateKey.generate()
    seed = base64.b64encode(key.private_bytes_raw()).decode("ascii") + "\n"
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="ascii") as fh:  # mkstemp makes it 0600
            fh.write(seed)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.link(tmp, path)
        except FileExistsError:
            return _whole_key(path)
        except OSError:  # this filesystem keeps no hard links
            return _made_alone(path, seed, key)
        return key
    finally:
        os.unlink(tmp)


def _made_alone(path: Path, seed: str, key: Ed25519PrivateKey) -> Ed25519PrivateKey:
    """The key file made by an exclusive create, or the one another run made first."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        return _whole_key(path)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as fh:
            fh.write(seed)
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        with contextlib.suppress(OSError):
            path.unlink()  # never leave half a key for the next run to refuse
        raise
    return key


def model_verdict(engine: str, model: str | None) -> dict[str, str]:
    return {
        "check": "model",
        "result": "pass",
        "detail": ascii_text(f"{engine}/{model or 'default'}"),
    }


def approval_verdict(
    call_id: str,
    *,
    approved: bool,
    approver: str | None = None,
    reason: str | None = None,
) -> dict[str, str]:
    check = f"scope_approval:{ascii_text(call_id)}"
    if approved:
        return {"check": check, "result": "pass", "detail": ascii_text(approver or "human")}
    detail = f"declined: {reason}" if reason else "declined"
    return {"check": check, "result": "warn", "detail": ascii_text(detail)}


def allow_verdict(call_id: str, allow: str) -> dict[str, str]:
    """A write the run's policy let through without asking, and which allow did.

    Not a ``scope_approval``: on the platform that is a person's decision,
    and this allow was set before the run began.
    """
    return {
        "check": f"scope_allow:{ascii_text(call_id)}",
        "result": "pass",
        "detail": ascii_text(allow),
    }


CAVEAT = "The answer may say otherwise; the receipt's steps are what happened."


def declined_and_failed(steps: Sequence[Any], verdicts: Sequence[Any]) -> tuple[int, int]:
    """How many calls the record says were declined, and how many others did not succeed.

    A declined call's step failed too, so it is counted once, as declined.
    Read from a journal, so an item that is not an object counts for nothing.
    """
    declined = {
        str(v.get("check"))[len("scope_approval:") :]
        for v in verdicts
        if isinstance(v, Mapping)
        and str(v.get("check")).startswith("scope_approval:")
        and v.get("result") == "warn"
    }
    failed = sum(
        1
        for s in steps
        if isinstance(s, Mapping)
        and s.get("success") is not True
        and ascii_text(s.get("call_id")) not in declined  # as the verdict wrote it
    )
    return len(declined), failed


def answer_caveat(declined: int, failed: int) -> str | None:
    """The line beside an answer whose calls did not all go as asked; None when they did.

    A model may say a declined or failed call succeeded, as a local model has
    done here, and only someone who read the steps would know.
    """
    said = []
    if declined:
        said.append("1 call was declined" if declined == 1 else f"{declined} calls were declined")
    if failed:  # not "failed": a test run that exits 1, or a call never answered, counts here
        said.append("1 call did not succeed" if failed == 1 else f"{failed} calls did not succeed")
    return f"{' and '.join(said)}. {CAVEAT}" if said else None


PATH_WORD = re.compile(r"[\w./~-]*[./][\w./~-]*")
PATH_WORDS = 40  # the most words with a dot or a slash a prompt has looked up


def named_here(prompt: str, cwd: Path) -> list[str]:
    """The files and folders under ``cwd`` that ``prompt`` names, in order, each once.

    A word names one when it has a dot or a slash and resolves to something
    under ``cwd``; a word that leads out of it, by ``..`` or a link, does not.
    """
    root = cwd.resolve()
    out: list[str] = []
    seen: set[Path] = set()
    for found in islice(PATH_WORD.finditer(prompt), PATH_WORDS):
        word = found.group().rstrip(".")  # a sentence's full stop is not the name's
        if "." not in word and "/" not in word:
            continue
        try:
            path = (root / word).resolve()
            here = path != root and path.is_relative_to(root) and path.exists()
        except (OSError, ValueError):
            continue
        if here and path not in seen:
            seen.add(path)
            out.append(word)
    return out


def unread_caveat(named: Sequence[str]) -> str | None:
    """The line beside an answer that names files here when its run made no tool calls."""
    if not named:
        return None
    shown = list(named[:3]) + ([f"{len(named) - 3} more"] if len(named) > 3 else [])
    which = shown[0] if len(shown) == 1 else f"{', '.join(shown[:-1])} and {shown[-1]}"
    one = len(named) == 1
    return (
        f"{which} {'is' if one else 'are'} here, but this run made no tool calls, "
        f"so the receipt shows no read of {'it' if one else 'them'}."
    )


def run_caveat(run_id: str) -> str | None:
    """``answer_caveat`` for a run, read from its kept journal; None when every call went."""
    try:
        journal = json.loads((run_path(run_id) / "journal.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(journal, dict):
        return None
    lists = [
        x if isinstance(x, list) else [] for x in (journal.get("steps"), journal.get("verdicts"))
    ]
    return answer_caveat(*declined_and_failed(*lists))


def scrub(value: Any, secrets: Mapping[str, str] | None) -> Any:
    """The JSON value that gets hashed and kept: preimage, then redaction."""
    pre = output_preimage(value)
    return redact_credentials(pre, secrets) if secrets else pre


def _clean_step(raw: Mapping[str, Any], secrets: Mapping[str, str] | None) -> dict[str, Any]:
    args = scrub(raw.get("args", {}), secrets)
    if not isinstance(args, dict):
        args = {"value": args}
    latency = raw.get("latency_ms", 0)
    return {
        "call_id": ascii_text(raw.get("call_id", "")),
        "tool": ascii_text(raw.get("tool", "")),
        "args": args,
        "output": scrub(raw.get("output"), secrets),
        "success": bool(raw.get("success", False)),
        "latency_ms": latency if isinstance(latency, int) and latency >= 0 else 0,
    }


def _index_path() -> Path:
    return metabot_home() / "runs.jsonl"


def _append_index(row: dict[str, Any]) -> None:
    line = json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
    fd = os.open(_index_path(), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


def _index_meta(meta: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in meta.items():
        if isinstance(v, str):
            out[k] = v[:INDEX_TEXT_LIMIT]
        elif v is None or isinstance(v, (bool, int, float)):
            out[k] = v
    return out


def seal_and_store(
    *,
    run_id: str,
    steps: list[dict[str, Any]],
    verdicts: list[dict[str, Any]],
    started_at: str,
    finished_at: str,
    key: Ed25519PrivateKey,
    secrets: Mapping[str, str] | None = None,
    policy_version: str = POLICY_VERSION,
    meta: Mapping[str, Any] | None = None,
    criteria: Mapping[str, bool] | None = None,
) -> Path:
    """Seal, then write journal, receipt and index line. Returns the receipt path.

    Declared ``criteria`` are judged on every kept step by the one rule the
    platform uses (``emerge.criteria``): the verdict joins the others and the
    criteria's digest joins ``policy_version``, so a third party can judge the
    journal again and get the same answer.
    """
    safe_id(run_id)
    did = key_did(key)
    clean = [_clean_step(s, secrets) for s in steps]
    declared = dict(criteria or {})
    accepted = declared_acceptance_verdict(declared, clean)
    if declared:
        policy_version = compose_policy_version(policy_version, criteria_digest(declared))
    envelope = seal_run(
        {
            "run_id": run_id,
            "agent_dids": [did],
            "policy_version": policy_version,
            "steps": clean,
            "verdicts": [*verdicts, *([accepted] if accepted else [])],
            "started_at": started_at,
            "finished_at": finished_at,
        },
        key,
    )
    receipt_path = receipts_dir() / f"{run_id}.json"
    kept_meta = scrub(dict(meta or {}), secrets)
    journal = {
        "format": JOURNAL_FORMAT,
        "run_id": run_id,
        "receipt": str(receipt_path),
        "signer": did,
        "started_at": started_at,
        "finished_at": finished_at,
        "meta": kept_meta,
        **({"criteria": declared} if declared else {}),
        "steps": clean,
        "verdicts": envelope["verdicts"],
    }
    write_private(run_dir(run_id) / "journal.json", json.dumps(journal, indent=2) + "\n")
    receipt_text = json.dumps(envelope, indent=2) + "\n"
    write_private(receipt_path, receipt_text)
    _append_index(
        {
            **_index_meta(kept_meta),
            "run_id": run_id,
            "started_at": started_at,
            "finished_at": finished_at,
            "steps": len(clean),
            "signer": did,
            "receipt": str(receipt_path),
            "receipt_sha256": hashlib.sha256(receipt_text.encode("utf-8")).hexdigest(),
        }
    )
    return receipt_path


def acceptance_of(receipt: Path | str) -> str | None:
    """A receipt's ``declared_acceptance`` result, or None when it has none."""
    try:
        verdicts = json.loads(Path(receipt).read_text(encoding="utf-8")).get("verdicts") or []
    except (OSError, ValueError, AttributeError):
        return None
    for verdict in verdicts:
        if isinstance(verdict, dict) and verdict.get("check") == "declared_acceptance":
            return str(verdict.get("result"))
    return None


class ProjectCopyError(RuntimeError):
    pass


def project_root(folder: Path) -> Path:
    """The project a folder belongs to: its git top level, else the folder itself."""
    return toplevel(folder) or folder.resolve()


_WHY = {
    errno.ELOOP: "a folder on the way is a link",
    errno.ENOTDIR: "a folder on the way is not a folder",
    errno.EEXIST: "a file of that name is there already",
}


def copy_to_project(receipt: Path | str, folder: Path) -> Path:
    """Copy a sealed receipt into ``<project>/.orcha/receipts/``; the copy's path.

    The project is the folder's git top level, else the folder. Only the
    receipt goes, never its journal, which holds what was hashed. The folders
    on the way are opened without following a link, and a file there already
    is never replaced, so a link in the project cannot send the copy anywhere
    else. ProjectCopyError says why there is no copy; the receipt in the
    MetaBot home is the one that counts either way.
    """
    source = Path(receipt)
    top = project_root(folder)
    try:
        data = source.read_bytes()
        top_fd = os.open(top, os.O_RDONLY | os.O_DIRECTORY)
        try:
            _write_inside(top_fd, PROJECT_RECEIPTS.parts, source.name, data)
        finally:
            os.close(top_fd)
    except OSError as exc:
        why = _WHY.get(exc.errno or 0) or exc.strerror or type(exc).__name__
        raise ProjectCopyError(
            f"no copy of the receipt in {top / PROJECT_RECEIPTS}: {why}"
        ) from None
    return top / PROJECT_RECEIPTS / source.name


def _write_inside(dir_fd: int, parts: tuple[str, ...], name: str, data: bytes) -> None:
    """Make and enter each folder in ``parts`` without following a link, then write ``name``."""
    if not parts:
        _write_new(dir_fd, name, data)
        return
    with contextlib.suppress(FileExistsError):
        os.mkdir(parts[0], 0o755, dir_fd=dir_fd)
    fd = _open_folder(parts[0], dir_fd)
    try:
        _write_inside(fd, parts[1:], name, data)
    finally:
        os.close(fd)


def _open_folder(name: str, dir_fd: int) -> int:
    """Open a folder inside another without following a link; a link raises ELOOP."""
    try:
        return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)
    except NotADirectoryError:
        # Linux asks "is it a folder" before "is it a link", so a link says ENOTDIR.
        if stat.S_ISLNK(os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode):
            raise OSError(errno.ELOOP, os.strerror(errno.ELOOP)) from None
        raise


def _write_new(dir_fd: int, name: str, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    out = os.open(name, flags, PROJECT_MODE, dir_fd=dir_fd)
    try:
        os.fchmod(out, PROJECT_MODE)
        view = memoryview(data)
        while view:
            view = view[os.write(out, view) :]
    except OSError:
        os.close(out)
        with contextlib.suppress(OSError):
            os.unlink(name, dir_fd=dir_fd)
        raise
    os.close(out)


def project_copy(receipt: Path, project: Path | None) -> tuple[Path | None, str | None]:
    """The project copy of a receipt when ``project`` is given: its path, or why there is none."""
    if project is None:
        return None, None
    try:
        return copy_to_project(receipt, project), None
    except ProjectCopyError as exc:
        return None, str(exc)


def verify_file(path: Path | str) -> AttestationVerdict:
    try:
        envelope = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return AttestationVerdict(valid=False)
    return verify_run_attestation(envelope)


def _step_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(step, dict) for step in value)


_COPIED = ("run_id", "verdicts", "started_at", "finished_at")  # into the journal as signed


def journal_matches(run_id: str) -> bool:
    """True when the kept journal re-derives every hash in the sealed receipt.

    What the journal copies from the receipt, the verdicts, the signer and the
    times among them, must also be what the receipt signs: a page shows them
    from the journal, beside the receipt's state.
    """
    try:
        journal = json.loads((run_path(run_id) / "journal.json").read_text(encoding="utf-8"))
        receipt = receipts_dir() / f"{safe_id(run_id)}.json"
        envelope = json.loads(receipt.read_text(encoding="utf-8"))
        sealed = envelope["steps"]
        kept = journal["steps"]
        copies = [journal[k] == envelope[k] for k in _COPIED]
        copies.append(journal["signer"] == envelope["signer"]["did"])
    except (OSError, ValueError, KeyError, TypeError):
        return False
    if not all(copies):
        return False
    if not (_step_list(sealed) and _step_list(kept)) or len(sealed) != len(kept):
        return False  # a file edited by hand into another shape matches nothing
    for s, k in zip(sealed, kept, strict=True):
        if (
            s.get("args_hash") != hash_value(k.get("args"))
            or s.get("output_hash") != hash_value(k.get("output"))
            or s.get("call_id") != k.get("call_id")
            or s.get("tool") != k.get("tool")
            or s.get("success") != k.get("success")
        ):
            return False
    return verify_run_attestation(envelope).valid


def list_index() -> list[dict[str, Any]]:
    path = _index_path()
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and "run_id" in row:
            rows.append(row)
    return rows
