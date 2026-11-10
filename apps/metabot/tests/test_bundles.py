"""Receipt bundles: ``receipts export`` writes one for a run, an office run or a ladder,
and ``receipts verify --chain`` checks it on any machine, offline.

Every byte of a bundle is pinned: a member receipt by the digest its seal
signs, the top receipt by its signature and the form MetaBot writes it in, a
journal by the hashes its receipt signs, and ``bundle.json`` by what the rest
re-derives. So flipping any byte of any file fails the check, and so does a
missing file or one the bundle does not list. A bundle comes from someone
else: reading one writes nothing, follows no link, and stops at a limit.
"""

from __future__ import annotations

import asyncio
import base64
import gzip
import hashlib
import io
import json
import os
import socket
import tarfile
from pathlib import Path

import pytest

from conftest import FIXTURES
from fake_chat import answer, text_reply
from fake_engines import wrap
from metabot.approvals import DenyAll
from metabot.cli import main
from metabot.config import Config, save_config
from metabot.engines.claude import ClaudeEngine
from metabot.gate import Policy
from metabot.paths import receipt_path, run_path
from metabot.receipts import ensure_key, model_verdict, seal_and_store
from metabot.runner import run_job

import metabot.bundles as bundles  # isort: skip
from metabot.bundles import BundleError, read_bundle, verify_bundle  # isort: skip


def _run(tmp_path: Path, monkeypatch) -> str:
    """One plain run, sealed with its prompt and answer pinned."""
    monkeypatch.setenv("FAKE_FIXTURE", str(FIXTURES / "claude-echo.jsonl"))
    result = asyncio.run(
        run_job(
            engine=wrap(ClaudeEngine),
            prompt="say metabot-probe",
            cwd=tmp_path,
            policy=Policy(),
            approver=DenyAll(),
        )  # fmt: skip
    )
    assert result.ok, result.error
    return result.run_id


def _ladder(chat, tmp_path: Path, capsys) -> str:
    """A ladder sealed over its rung and the rung's checks run."""
    fake = chat([answer("four")])
    save_config(Config(openai_base_url=fake.url, ladder=[{"engine": "openai", "model": "tiny"}]))
    argv = ["ask", "--ladder", "--json", "--check", "true", "--cwd", str(tmp_path), "2+2"]
    assert main(argv) == 0
    return json.loads(capsys.readouterr().out)["ladder"]["ladder_id"]


def _office(tmp_path: Path) -> str:
    from test_seals import _looped

    _root, office = asyncio.run(_looped(tmp_path))
    return office


def _export(capsys, run_id: str, out: Path, *extra: str, journals: bool = True) -> str:
    more = ["--journals"] if journals else []
    code = main(["receipts", "export", run_id, "--out", str(out), *extra, *more])
    said = capsys.readouterr()
    assert code == 0, said.err
    return said.out


def _verify(capsys, path: Path) -> tuple[int, list[str]]:
    code = main(["receipts", "verify", "--chain", str(path)])
    said = capsys.readouterr()
    assert "Traceback" not in said.out + said.err
    return code, said.out.splitlines()


# ── export, then verify ─────────────────────────────────────────────────────


def test_a_run_exports_to_a_folder_that_verifies(tmp_path, monkeypatch, capsys):
    run = _run(tmp_path, monkeypatch)
    out = tmp_path / "shared"
    said = _export(capsys, run, out)
    assert said.splitlines()[0] == f"wrote {out}"
    assert f"next: metabot receipts verify --chain {out}" in said
    names = {p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()}
    assert names == {"bundle.json", f"receipts/{run}.json", f"journals/{run}.json"}
    assert (out / "receipts" / f"{run}.json").read_bytes() == receipt_path(run).read_bytes()
    code, lines = _verify(capsys, out)
    assert code == 0 and lines[0] == f"VALID  {out}"
    assert any(line.startswith(f"  run {run}, signed by did:") for line in lines)
    assert any("1 journal" in line and "re-derives every hash" in line for line in lines)


def test_a_ladder_exports_to_a_tarball_that_verifies_on_another_machine(
    chat, tmp_path, monkeypatch, capsys
):
    ladder = _ladder(chat, tmp_path, capsys)
    out = tmp_path / "ladder.tar.gz"
    _export(capsys, ladder, out)
    with tarfile.open(out) as tar:
        names = sorted(tar.getnames())
    top = f"{ladder}.bundle"
    assert f"{top}/bundle.json" in names
    assert f"{top}/receipts/{ladder}.1.json" in names
    assert f"{top}/receipts/{ladder}.1.checks.json" in names
    elsewhere = tmp_path / "another-machine"
    monkeypatch.setenv("METABOT_HOME", str(elsewhere / "home"))  # none of this run is there
    monkeypatch.setenv("ORCHA_KEY_PATH", str(elsewhere / "no-key"))
    code, lines = _verify(capsys, out)
    assert code == 0 and lines[0] == f"VALID  {out}", lines
    assert any(line.startswith(f"  ladder {ladder}, signed by did:") for line in lines)
    assert any("2 members: each present, unchanged, and valid" in line for line in lines)
    assert not elsewhere.exists()  # verifying wrote nothing there, not even a home


def test_an_office_run_exports_and_verifies(tmp_path, capsys):
    office = _office(tmp_path)
    out = tmp_path / "office"
    _export(capsys, office, out)
    members = [f"{office}.{m}" for m in ("build", "build.checks", "build.r2", "build.r2.checks")]
    assert {p.stem for p in (out / "receipts").iterdir()} == {office, *members}
    code, lines = _verify(capsys, out)
    assert code == 0, lines
    assert any(line.startswith(f"  office {office}, signed by") for line in lines)
    assert any("4 members: each present, unchanged, and valid" in line for line in lines)


def test_the_default_bundle_is_a_tarball_named_for_the_run_here(tmp_path, monkeypatch, capsys):
    run = _run(tmp_path, monkeypatch)
    here = tmp_path / "here"
    here.mkdir()
    monkeypatch.chdir(here)
    assert main(["receipts", "export", run, "--journals"]) == 0
    capsys.readouterr()
    out = here / f"{run}.bundle.tar.gz"
    assert out.is_file() and (out.stat().st_mode & 0o777) == 0o600
    assert _verify(capsys, out)[0] == 0
    again = tmp_path / "again.tar.gz"
    _export(capsys, run, again)
    assert again.read_bytes() == out.read_bytes()  # one run, one set of bytes
    plain = tmp_path / "plain.tar"
    _export(capsys, run, plain)
    assert plain.read_bytes()[:2] != b"\x1f\x8b" and read_bundle(plain) == read_bundle(out)


def test_a_plain_export_holds_the_receipts_alone_and_still_verifies(chat, tmp_path, capsys):
    ladder = _ladder(chat, tmp_path, capsys)
    out = tmp_path / "bare"
    said = _export(capsys, ladder, out, journals=False)
    assert not (out / "journals").exists()
    assert "the receipts hold hashes, and no text" in said
    assert "--journals puts in the prompts, answers and tool calls as kept" in said
    code, lines = _verify(capsys, out)
    assert code == 0 and any(line == "  no journals" for line in lines), lines


def test_export_says_what_the_journals_hold(tmp_path, monkeypatch, capsys):
    said = _export(capsys, _run(tmp_path, monkeypatch), tmp_path / "out")
    assert "the prompts, answers and tool calls as kept" in said
    assert "leave out --journals to export the receipts alone" in said


# ── a byte changed, a file missing or added ─────────────────────────────────


def _ladder_files(chat, tmp_path, capsys) -> tuple[str, dict[str, bytes]]:
    ladder = _ladder(chat, tmp_path, capsys)
    out = tmp_path / "bundle"
    _export(capsys, ladder, out)
    files = read_bundle(out)
    assert verify_bundle(files).valid
    return ladder, files


def _with(files: dict[str, bytes], name: str, at: int, byte: int) -> dict[str, bytes]:
    data = bytearray(files[name])
    data[at] = byte
    return {**files, name: bytes(data)}


def test_flipping_any_one_byte_of_any_file_fails_the_bundle(chat, tmp_path, capsys):
    _ladder_id, files = _ladder_files(chat, tmp_path, capsys)
    assert {n.split("/")[0] for n in files} == {"bundle.json", "receipts", "journals"}
    held = [
        (name, at)
        for name, data in files.items()
        for at in range(len(data))
        if verify_bundle(_with(files, name, at, data[at] ^ 0x01)).valid
    ]
    assert held == []


def test_whitespace_swapped_for_other_whitespace_fails_too(chat, tmp_path, capsys):
    _ladder_id, files = _ladder_files(chat, tmp_path, capsys)
    space = b" \t\n\r"
    held = [
        (name, at, other)
        for name, data in files.items()
        for at, byte in enumerate(data)
        if byte in space
        for other in space
        if other != byte and verify_bundle(_with(files, name, at, other)).valid
    ]
    assert held == []


def test_a_flipped_byte_in_the_tarball_never_passes_a_changed_file(tmp_path, monkeypatch, capsys):
    """A byte of the container itself, such as the time in the gzip header, changes no
    file of the bundle; any flip that does change one fails, and none raises."""
    run = _run(tmp_path, monkeypatch)
    out = tmp_path / "b.tar.gz"
    _export(capsys, run, out)
    data, good = out.read_bytes(), read_bundle(out)
    bad = tmp_path / "bad.tar.gz"
    failed = 0
    for at in range(len(data)):
        bad.write_bytes(data[:at] + bytes([data[at] ^ 0x01]) + data[at + 1 :])
        try:
            files = read_bundle(bad)
        except BundleError:
            failed += 1
            continue
        if verify_bundle(files).valid:
            assert files == good, at  # VALID only when every file is as exported
        else:
            failed += 1
    assert failed > len(data) * 0.9
    assert _verify(capsys, bad)[0] in (0, 1)


def test_the_cli_says_which_file_was_changed(tmp_path, monkeypatch, capsys):
    run = _run(tmp_path, monkeypatch)
    out = tmp_path / "out"
    _export(capsys, run, out)
    journal = out / "journals" / f"{run}.json"
    kept = json.loads(journal.read_text(encoding="utf-8"))
    kept["meta"]["prompt"] = "a prompt nobody gave"
    journal.write_text(json.dumps(kept, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    code, lines = _verify(capsys, out)
    assert code == 1 and lines[0] == f"INVALID  {out}"
    assert f"  journals/{run}.json: its prompt does not re-derive the digest its receipt signs" in (
        lines
    )


def test_a_missing_member_fails_even_where_this_machine_holds_it(chat, tmp_path, capsys):
    ladder = _ladder(chat, tmp_path, capsys)
    out = tmp_path / "bundle"
    _export(capsys, ladder, out)
    (out / "receipts" / f"{ladder}.1.json").unlink()
    assert receipt_path(f"{ladder}.1").is_file()  # this machine still has it
    code, lines = _verify(capsys, out)
    assert code == 1 and lines[0].startswith("INVALID")
    assert f"  {ladder}.1: its receipt is missing" in lines


def test_a_missing_journal_or_manifest_fails(chat, tmp_path, capsys):
    ladder, files = _ladder_files(chat, tmp_path, capsys)
    dropped = {k: v for k, v in files.items() if k != f"journals/{ladder}.1.json"}
    assert not verify_bundle(dropped).valid
    report = verify_bundle({k: v for k, v in files.items() if k != "bundle.json"})
    assert report.problems == ["bundle.json: missing, so this is not a bundle"]


def test_a_file_the_bundle_does_not_list_fails(tmp_path, monkeypatch, capsys):
    run = _run(tmp_path, monkeypatch)
    other = _run(tmp_path, monkeypatch)
    out = tmp_path / "out"
    _export(capsys, run, out)
    files = read_bundle(out)
    with_other = {**files, f"receipts/{other}.json": receipt_path(other).read_bytes()}
    assert f"receipts/{other}.json: a receipt this bundle's run does not hold" in (
        verify_bundle(with_other).problems
    )
    for name in ("notes.txt", "workflows/0123.yaml", "receipts/x/y.json"):
        problems = verify_bundle({**files, name: b"hello"}).problems
        assert f"{name}: a file a metabot.bundle/1 bundle does not have" in problems


def test_a_member_sealed_with_no_receipt_must_have_none(tmp_path, monkeypatch, capsys):
    from metabot.seals import Member, seal_runs

    run = _run(tmp_path, monkeypatch)
    seal = "mb-20261104T000000Z-5ea1ed"
    members = [
        Member(run, "rung", receipt_path(run), True),
        Member(f"{seal}.2", "rung", None, False),
    ]
    seal_runs(seal, members, delivered=True, kind_detail="kind=ladder", verdicts=[],
              started_at="2026-11-04T00:00:00Z", meta={"task": "two rungs"})  # fmt: skip
    out = tmp_path / "out"
    _export(capsys, seal, out)
    code, lines = _verify(capsys, out)
    assert code == 0, lines
    assert "  2 members: 1 present, unchanged, and valid; 1 sealed with no receipt" in lines
    files = read_bundle(out)
    files[f"receipts/{seal}.2.json"] = files[f"receipts/{run}.json"]
    problems = verify_bundle(files).problems
    assert f"{seal}.2: the seal says it has no receipt, but one is here" in problems


def test_a_member_swapped_for_another_valid_receipt_fails(chat, tmp_path, capsys):
    ladder, files = _ladder_files(chat, tmp_path, capsys)
    member, checks = f"receipts/{ladder}.1.json", f"receipts/{ladder}.1.checks.json"
    swapped = {**files, member: files[checks]}
    problems = verify_bundle(swapped).problems
    assert f"{ladder}.1: its receipt is not the one sealed" in problems


def test_a_manifest_naming_a_member_as_the_run_fails(chat, tmp_path, capsys):
    ladder, files = _ladder_files(chat, tmp_path, capsys)
    manifest = json.loads(files["bundle.json"])
    manifest.update(run_id=f"{ladder}.1", kind="run")
    files["bundle.json"] = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    problems = verify_bundle(files).problems
    assert f"receipts/{ladder}.json: a receipt this bundle's run does not hold" in problems


def _relisted(files: dict[str, bytes], **changes) -> dict[str, bytes]:
    """``files`` with ``bundle.json`` written again over them, so it lists each as it is."""
    manifest = json.loads(files["bundle.json"])
    manifest.update(changes)
    manifest["files"] = {
        name: hashlib.sha256(data).hexdigest()
        for name, data in files.items()
        if name != "bundle.json"
    }
    return {
        **files,
        "bundle.json": (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode(),
    }


def test_a_manifest_that_names_another_kind_fails(chat, tmp_path, capsys):
    ladder, files = _ladder_files(chat, tmp_path, capsys)
    assert verify_bundle(_relisted(files)).valid
    problems = verify_bundle(_relisted(files, kind="office")).problems
    assert problems == ["bundle.json: it says office, and its run's receipt is a ladder's"]


def test_a_file_in_another_form_fails_though_it_is_listed(chat, tmp_path, capsys):
    """The same JSON in other bytes, and ``bundle.json`` listing them: each form is fixed."""
    ladder, files = _ladder_files(chat, tmp_path, capsys)
    for name, said in (
        (f"receipts/{ladder}.json", "not in the form metabot writes a receipt"),
        (f"journals/{ladder}.1.json", "not in the form a bundle writes a journal"),
    ):
        spaced = (json.dumps(json.loads(files[name]), indent=4) + "\n").encode()
        assert verify_bundle(_relisted({**files, name: spaced})).problems == [f"{name}: {said}"]
    manifest = _relisted(files)["bundle.json"]
    spaced = (json.dumps(json.loads(manifest), indent=4, sort_keys=True) + "\n").encode()
    problems = verify_bundle({**files, "bundle.json": spaced}).problems
    assert problems == ["bundle.json: not in the form metabot writes it"]


def test_a_signature_written_another_way_fails(tmp_path, monkeypatch, capsys):
    """Base64 leaves spare bits in its last character: a decoder passes over them, so the
    signature is the same, and the receipt's bytes are not."""
    run = _run(tmp_path, monkeypatch)
    out = tmp_path / "out"
    _export(capsys, run, out)
    files = read_bundle(out)
    name = f"receipts/{run}.json"
    envelope = json.loads(files[name])
    sig = envelope["signature"]
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
    last = sig.rstrip("=")[-1]
    other = alphabet[alphabet.index(last) ^ 0b1]  # a spare bit: 64 bytes leave 4 of them
    respelled = sig.rstrip("=")[:-1] + other + "=" * (len(sig) - len(sig.rstrip("=")))
    assert base64.b64decode(respelled) == base64.b64decode(sig)
    envelope["signature"] = respelled
    changed = _relisted({**files, name: (json.dumps(envelope, indent=2) + "\n").encode()})
    assert changed[name] != files[name]
    assert verify_bundle(changed).problems == [
        f"{name}: its signature is not written in canonical base64"
    ]


def test_a_journal_value_of_another_type_fails(tmp_path, monkeypatch, capsys):
    """``1`` equals ``true`` and ``7.0`` equals ``7`` in Python; a bundle journal holds each
    as its receipt signs it."""
    run = _run(tmp_path, monkeypatch)
    out = tmp_path / "out"
    _export(capsys, run, out)
    files = read_bundle(out)
    name = f"journals/{run}.json"
    for key, value in (("success", 1), ("latency_ms", None)):
        journal = json.loads(files[name])
        step = journal["steps"][0]
        step[key] = value if value is not None else float(step[key])
        changed = _relisted(
            {**files, name: (json.dumps(journal, indent=2, sort_keys=True) + "\n").encode()}
        )
        assert verify_bundle(changed).problems == [
            f"{name}: step 1's {key} is not the one its receipt signs"
        ], key


def test_a_bundle_journal_names_no_login_folder_or_path_from_this_machine(tmp_path, capsys):
    key, _did = ensure_key()
    run_id = "mb-20261104T000000Z-0a0b0c"
    seal_and_store(
        run_id=run_id,
        steps=[
            {
                "call_id": "c1",
                "tool": "fake/Read",
                "args": {"path": "."},
                "output": "ok",
                "success": True,
                "latency_ms": 7,
            }
        ],  # fmt: skip
        verdicts=[model_verdict("claude", "sonnet")],
        started_at="2026-11-04T00:00:00Z",
        finished_at="2026-11-04T00:00:09Z",
        key=key,
        meta={
            "engine": "claude",
            "prompt": "hello",
            "final": "hi",
            "cwd": str(tmp_path),
            "login": "login-name-q7",
            "login_folder": str(tmp_path / "folder-q7"),
        },  # fmt: skip
        commit=("prompt", "final", "login", "login_folder"),
    )
    out = tmp_path / "out"
    _export(capsys, run_id, out)
    files = read_bundle(out)
    every = b"".join(files.values())
    for private in (str(tmp_path).encode(), b"login-name-q7", b"folder-q7"):
        assert private not in every
    journal = json.loads(files[f"journals/{run_id}.json"])
    assert journal["meta"] == {"prompt": "hello", "final": "hi"}
    assert set(journal["salts"]) == {"prompt", "final"}
    assert journal["steps"][0]["latency_ms"] == 7
    report = verify_bundle(files)
    assert report.valid and report.withheld == ["login", "login_folder"]
    code, lines = _verify(capsys, out)
    assert code == 0 and any("withheld: login, login_folder" in line for line in lines)


# ── reading a bundle from someone else ──────────────────────────────────────


def _members(tmp_path, monkeypatch, capsys) -> list[tuple[tarfile.TarInfo, bytes | None]]:
    """A good bundle's tarball, as members and their bytes, to build a bad one from."""
    run = _run(tmp_path, monkeypatch)
    out = tmp_path / "good.tar.gz"
    _export(capsys, run, out)
    with tarfile.open(out) as tar:
        return [
            (m, tar.extractfile(m).read() if m.isfile() else None)  # type: ignore[union-attr]
            for m in tar.getmembers()
        ]


def _tarball(path: Path, members, compress: bool = True) -> Path:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for info, data in members:
            tar.addfile(info, io.BytesIO(data) if data is not None else None)
    path.write_bytes(gzip.compress(raw.getvalue()) if compress else raw.getvalue())
    return path


def _info(
    name: str, kind: bytes = tarfile.REGTYPE, size: int = 0, link: str = ""
) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type, info.size, info.linkname, info.mode = kind, size, link, 0o600
    return info


def _top(members) -> str:
    return members[0][0].name.split("/")[0]


def _evil(top: str, name: str):
    evils = {
        "absolute": [(_info("/etc/metabot-evil", size=1), b"x")],
        "dot-dot": [(_info(f"{top}/../../metabot-evil", size=1), b"x")],
        "symlink": [(_info(f"{top}/receipts/s.json", tarfile.SYMTYPE, link="/etc/passwd"), None)],
        "hardlink": [(_info(f"{top}/receipts/h.json", tarfile.LNKTYPE, link=f"{top}/x"), None)],
        "fifo": [(_info(f"{top}/receipts/fifo.json", tarfile.FIFOTYPE), None)],
        "device": [(_info(f"{top}/receipts/dev.json", tarfile.CHRTYPE), None)],
        "two tops": [(_info("elsewhere/bundle.json", size=2), b"{}")],
        "twice": [(_info(f"{top}/bundle.json", size=2), b"{}")],
        "empty part": [(_info(f"{top}//receipts/x.json", size=2), b"{}")],
    }  # fmt: skip
    return evils[name]


@pytest.mark.parametrize(
    "name",
    ["absolute", "dot-dot", "symlink", "hardlink", "fifo", "device", "two tops", "twice",
     "empty part"],
)  # fmt: skip
def test_a_tarball_entry_that_could_land_outside_or_is_no_file_is_refused(
    tmp_path, monkeypatch, capsys, name
):
    members = _members(tmp_path, monkeypatch, capsys)
    bad = _tarball(tmp_path / "bad.tar.gz", [*members, *_evil(_top(members), name)])
    before = sorted(p for p in Path("/").glob("metabot-evil*"))
    code, lines = _verify(capsys, bad)
    assert code == 1 and lines[0] == f"INVALID  {bad}", lines
    assert len(lines) == 2 and lines[1].startswith("  the tarball: "), lines
    assert sorted(p for p in Path("/").glob("metabot-evil*")) == before
    assert not (tmp_path / "metabot-evil").exists()


def test_a_tarball_past_the_limits_is_refused_before_it_is_read_whole(
    tmp_path, monkeypatch, capsys
):
    members = _members(tmp_path, monkeypatch, capsys)
    top = _top(members)
    largest, kept = max(len(data or b"") for _info_, data in members), bundles.MAX_FILE_BYTES
    monkeypatch.setattr(bundles, "MAX_FILE_BYTES", largest + 10)  # every good file fits
    over = largest + 11
    entry = (_info(f"{top}/journals/big.json", size=over), b"0" * over)
    big = _tarball(tmp_path / "big.tar.gz", [*members, entry])
    assert _verify(capsys, big)[1][1] == (
        f"  the tarball: journals/big.json is larger than {largest + 10} bytes"
    )
    monkeypatch.setattr(bundles, "MAX_FILES", 4)
    small = [(_info(f"{top}/journals/j{n}.json", size=1), b"x") for n in range(6)]
    many = _tarball(tmp_path / "many.tar.gz", [*members, *small])
    assert _verify(capsys, many)[1][1] == "  the tarball: more than 4 files"
    monkeypatch.setattr(bundles, "MAX_FILES", 4096)
    monkeypatch.setattr(bundles, "MAX_FILE_BYTES", kept)
    monkeypatch.setattr(bundles, "MAX_TOTAL_BYTES", 20_000)
    zeros = [(_info(f"{top}/journals/z{n}.json", size=4000), b"\0" * 4000) for n in range(10)]
    bomb = _tarball(tmp_path / "bomb.tar.gz", [*members, *zeros])
    assert _verify(capsys, bomb)[1][1] == "  the tarball: more than 20000 bytes in all"


def test_an_uncompressed_tarball_reads_too(tmp_path, monkeypatch, capsys):
    members = _members(tmp_path, monkeypatch, capsys)
    plain = _tarball(tmp_path / "plain.tar", members, compress=False)
    assert _verify(capsys, plain)[0] == 0


def test_a_folder_bundle_holding_a_link_is_refused(chat, tmp_path, capsys):
    ladder = _ladder(chat, tmp_path, capsys)
    out = tmp_path / "bundle"
    _export(capsys, ladder, out)
    member = out / "receipts" / f"{ladder}.1.json"
    member.unlink()
    member.symlink_to(receipt_path(f"{ladder}.1"))  # the same bytes, from this machine
    code, lines = _verify(capsys, out)
    assert code == 1 and lines[1] == f"  receipts/{ladder}.1.json: a link; a bundle holds files"


@pytest.mark.parametrize(
    "garbage",
    [b"", b"not json", b"[1,2]", b"{\"format\": \"metabot.bundle/1\"}", b"[" * 50_000,
     b"\xff\xfe\x00", json.dumps({"format": "metabot.bundle/2", "run_id": "x"}).encode()],
)  # fmt: skip
def test_a_bundle_json_that_does_not_read_says_so(tmp_path, monkeypatch, capsys, garbage):
    run = _run(tmp_path, monkeypatch)
    out = tmp_path / "out"
    _export(capsys, run, out)
    (out / "bundle.json").write_bytes(garbage)
    code, lines = _verify(capsys, out)
    assert code == 1 and lines[0].startswith("INVALID") and lines[1].startswith("  bundle.json: ")


@pytest.mark.parametrize("garbage", [b"", b"{", b"[]", b"{}", b"[" * 50_000, b"\xff"])
def test_a_receipt_or_journal_that_does_not_read_is_invalid_without_a_traceback(
    chat, tmp_path, capsys, garbage
):
    ladder, files = _ladder_files(chat, tmp_path, capsys)
    for name in files:
        if name != "bundle.json":
            assert not verify_bundle({**files, name: garbage}).valid, name


def test_what_is_no_bundle_is_said_plainly(tmp_path, capsys):
    (tmp_path / "noise").write_bytes(os.urandom(512))
    empty = tmp_path / "empty"
    empty.mkdir()
    for path in (tmp_path / "noise", empty):
        code, lines = _verify(capsys, path)
        assert code == 1 and lines[0] == f"INVALID  {path}" and len(lines) == 2, lines
    assert main(["receipts", "verify", "--chain", str(tmp_path / "not-there")]) == 2
    assert "no bundle at" in capsys.readouterr().err


def test_reading_a_bundle_raises_its_own_error_for_bad_input(tmp_path):
    (tmp_path / "noise.tar.gz").write_bytes(b"\x1f\x8b" + os.urandom(200))
    with pytest.raises(BundleError, match="^the tarball: "):
        read_bundle(tmp_path / "noise.tar.gz")


def test_verify_never_opens_a_socket(tmp_path, monkeypatch, capsys):
    run = _run(tmp_path, monkeypatch)
    out = tmp_path / "out.tar.gz"
    _export(capsys, run, out)

    def refused(*_a, **_k):
        raise AssertionError("verify --chain opened a socket")

    monkeypatch.setattr(socket, "socket", refused)
    monkeypatch.setattr(socket, "create_connection", refused)
    monkeypatch.setattr(socket, "getaddrinfo", refused)
    assert _verify(capsys, out)[0] == 0


# ── what export refuses ─────────────────────────────────────────────────────


def test_export_refuses_a_run_this_machine_does_not_have(tmp_path, capsys):
    assert main(["receipts", "export", "mb-nothing", "--out", str(tmp_path / "x")]) == 2
    assert "no receipt mb-nothing" in capsys.readouterr().err
    assert main(["receipts", "export", "../escape", "--out", str(tmp_path / "x")]) == 2
    assert not (tmp_path / "x").exists()


def test_export_never_writes_over_anything(tmp_path, monkeypatch, capsys):
    run = _run(tmp_path, monkeypatch)
    for there in (tmp_path / "taken.tar.gz", tmp_path / "taken"):
        if there.suffix:
            there.write_text("mine", encoding="utf-8")
        else:
            there.mkdir()
        assert main(["receipts", "export", run, "--out", str(there)]) == 2
        assert "is there already" in capsys.readouterr().err
    assert (tmp_path / "taken.tar.gz").read_text(encoding="utf-8") == "mine"
    assert list((tmp_path / "taken").iterdir()) == []


def test_export_refuses_a_seal_whose_member_is_not_here(chat, tmp_path, capsys):
    ladder = _ladder(chat, tmp_path, capsys)
    receipt_path(f"{ladder}.1.checks").unlink()
    assert main(["receipts", "export", ladder, "--out", str(tmp_path / "x")]) == 1
    assert f"{ladder}.1.checks: its receipt is missing" in capsys.readouterr().err
    assert not (tmp_path / "x").exists()


def test_export_refuses_a_journal_that_does_not_match_its_receipt(tmp_path, monkeypatch, capsys):
    run = _run(tmp_path, monkeypatch)
    journal = run_path(run) / "journal.json"
    kept = json.loads(journal.read_text(encoding="utf-8"))
    kept["steps"][0]["output"] = "an output it never had"
    journal.write_text(json.dumps(kept), encoding="utf-8")
    assert main(["receipts", "export", run, "--out", str(tmp_path / "x"), "--journals"]) == 1
    err = capsys.readouterr().err
    assert f"journals/{run}.json" in err and "leave out --journals" in err
    assert not (tmp_path / "x").exists()
    assert main(["receipts", "export", run, "--out", str(tmp_path / "y")]) == 0


# ── a run's, a ladder's and an office run's bundle bytes ────────────────────

# The tarball of each, from one key and fixed times, as S12 wrote it before workflows: a
# change to bundles that moves one of these changes what an existing bundle holds. A
# change to the receipt format moves them too, and is no change to bundles.
PINNED = {
    "run": "88cd4fd478c54dfb92f673909cc54e6e1f123ac20724ac146fa68e6cbba1733f",
    "ladder": "35a3d9252ae3343bb89c96f713c967ef7ec52145f2d68af3f6acf952b06de36d",
    "office": "89b4fc1044492eca0e71f4e485828b81ec1481c07dfb264daccb43e9fc7b7ba2",
}


def _fixed_bundle(kind: str) -> bytes:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    at = "2026-11-04T00:00:00Z"
    top = "mb-20261104T000000Z-0f1e2d"
    run = f"{top}.1" if kind != "run" else top
    step = {"call_id": "c1", "tool": "fake/Read", "args": {"path": "."}, "output": "ok",
            "success": True, "latency_ms": 7}  # fmt: skip
    verdicts = [model_verdict("claude", "sonnet")]
    path = seal_and_store(
        run_id=run, steps=[step], verdicts=verdicts, started_at=at, finished_at=at, key=key,
        meta={"engine": "claude"},
    )  # fmt: skip
    if kind != "run":
        member = {"call_id": run, "tool": "metabot.member.rung", "args": {"run_id": run},
                  "output": hashlib.sha256(path.read_bytes()).hexdigest(), "success": True,
                  "latency_ms": 0}  # fmt: skip
        seal = {"check": "seal", "result": "pass", "detail": f"kind={kind};members=1"}
        seal_and_store(run_id=top, steps=[member], verdicts=[seal], started_at=at,
                       finished_at=at, key=key, meta={"engine": "seal"}, index=False)  # fmt: skip
    files = bundles.build_bundle(top, journals=True)
    assert verify_bundle(files).valid
    return bundles._tarball_bytes(top + bundles.TOP_SUFFIX, files, 0, True)


@pytest.mark.parametrize("kind", sorted(PINNED))
def test_a_run_s_a_ladder_s_and_an_office_run_s_bundle_bytes_are_as_s12_wrote_them(kind):
    assert hashlib.sha256(_fixed_bundle(kind)).hexdigest() == PINNED[kind]


# ── a workflow run and a firing ─────────────────────────────────────────────


def _workflow(chat, tmp_path: Path, steps: list | None = None, **top):
    """A workflow kept, approved and run once: the workflow, its approval, its seal's id."""
    from metabot.console import Trace
    from metabot.workflow_approval import approve
    from metabot.workflow_run import run_workflow
    from metabot.workflows import FORMAT, store_workflow

    fake = chat(repeat=text_reply("hi"))
    save_config(Config(openai_base_url=fake.url, ladder=[{"engine": "openai", "model": "tiny"}]))
    data = {
        "format": FORMAT,
        "name": "date-fix",
        "cwd": str(tmp_path),
        "steps": steps or [{"name": "say", "kind": "ask", "prompt": "Say hi.", "engine": "openai"}],
        **top,
    }
    wf = store_workflow(json.dumps(data).encode())
    approval = approve(wf)
    run = asyncio.run(run_workflow(wf, approval, approver=DenyAll(), trace=Trace(quiet=True)))
    assert run.ok and run.seal is not None, [s.error for s in run.steps]
    return wf, approval, run.run_id


def _workflow_files(chat, tmp_path: Path, capsys, **kw) -> tuple[str, dict[str, bytes]]:
    wf, approval, run_id = _workflow(chat, tmp_path, **kw)
    out = tmp_path / "wf-bundle"
    _export(capsys, run_id, out)
    files = read_bundle(out)
    assert verify_bundle(files).valid, verify_bundle(files).problems
    return run_id, files


def test_a_workflow_run_exports_with_its_file_and_its_approval_and_verifies_elsewhere(
    chat, tmp_path, monkeypatch, capsys
):
    from metabot.workflows import kept_path

    steps = [
        {"name": "one", "kind": "ask", "prompt": "Say hi.", "engine": "openai"},
        {"name": "two", "kind": "ask", "prompt": "Say bye.", "engine": "openai"},
    ]
    wf, approval, run_id = _workflow(chat, tmp_path, steps=steps)
    out = tmp_path / "wf.tar.gz"
    said = _export(capsys, run_id, out, journals=False)
    assert f"  workflow {run_id}: its seal and 2 member receipts, no journals" in said
    assert f"  the workflow file {wf.short} and its approval {approval.run_id}" in said
    files = read_bundle(out)
    workflow, signed = f"workflows/{wf.sha256}.json", f"approvals/{approval.run_id}.json"
    assert files[workflow] == kept_path(wf.sha256).read_bytes() == wf.raw
    assert files[signed] == approval.receipt.read_bytes()
    assert json.loads(files["bundle.json"])["kind"] == "workflow"
    elsewhere = tmp_path / "another-machine"
    monkeypatch.setenv("METABOT_HOME", str(elsewhere / "home"))
    monkeypatch.setenv("ORCHA_KEY_PATH", str(elsewhere / "no-key"))
    code, lines = _verify(capsys, out)
    assert code == 0 and lines[0] == f"VALID  {out}", lines
    assert any(line.startswith(f"  workflow {run_id}, signed by did:") for line in lines)
    assert "  2 members: each present, unchanged, and valid" in lines
    assert f"  workflow date-fix {wf.short}: its file hashes to the sha256 the seal signs" in lines
    assert (
        f"  approval {approval.run_id}: signed by the same key, over that sha256, and its "
        "receipt hashes to the digest the seal signs"
    ) in lines
    assert (
        "  not shown: whether that approval was revoked; a revoke stays on the machine that "
        "signed it, and no bundle holds one"
    ) in lines
    assert not elsewhere.exists()


def test_a_workflow_s_ladder_step_brings_the_ladder_s_own_members(chat, tmp_path, capsys):
    steps = [{"name": "climb", "kind": "ladder", "prompt": "2+2?", "checks": ["true"]}]
    run_id, files = _workflow_files(chat, tmp_path, capsys, steps=steps)
    [ladder] = [json.loads(files[f"receipts/{run_id}.json"])["steps"][0]["call_id"]]
    held = [f"receipts/{ladder}.json", f"receipts/{ladder}.1.json"]
    held += [f"receipts/{ladder}.1.checks.json", f"journals/{ladder}.1.json"]
    assert set(held) <= set(files)
    assert verify_bundle(files).members == 3
    dropped = {k: v for k, v in files.items() if k != f"receipts/{ladder}.1.json"}
    assert f"{ladder}.1: its receipt is missing" in verify_bundle(dropped).problems
    stray = {**files, f"receipts/{ladder}.9.json": files[f"receipts/{ladder}.1.json"]}
    assert f"receipts/{ladder}.9.json: a receipt this bundle's run does not hold" in (
        verify_bundle(stray).problems
    )


def test_a_firing_exports_and_verify_checks_its_signed_lineage(chat, tmp_path, capsys):
    from metabot.routines import fire
    from metabot.workflow_approval import approve
    from metabot.workflow_firing import save_workflow_routine
    from metabot.workflows import FORMAT, store_workflow

    fake = chat(repeat=text_reply("hi"))
    save_config(Config(openai_base_url=fake.url))
    data = {
        "format": FORMAT,
        "name": "nightly",
        "cwd": str(tmp_path),
        "steps": [{"name": "say", "kind": "ask", "prompt": "Say hi.", "engine": "openai"}],
        "schedule": {"cron": "0 2 * * *", "tz": "UTC"},
        "ceiling": {"firings": 40},
    }
    wf = store_workflow(json.dumps(data).encode())
    approval = approve(wf)
    rid = save_workflow_routine("nightly", "nightly")
    firing = asyncio.run(fire(rid))
    assert firing.state == "sealed" and firing.run_id.startswith("wff-"), firing
    out = tmp_path / "firing"
    _export(capsys, firing.run_id, out)
    assert {f"workflows/{wf.sha256}.json", f"approvals/{approval.run_id}.json"} <= {
        p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()
    }
    code, lines = _verify(capsys, out)
    assert code == 0, lines
    assert f"  firing 1 of routine {rid}, under the same workflow and approval" in lines


def test_flipping_any_one_byte_of_any_file_fails_a_workflow_bundle(chat, tmp_path, capsys):
    _run_id, files = _workflow_files(chat, tmp_path, capsys)
    assert {n.split("/")[0] for n in files} == {
        "bundle.json", "receipts", "journals", "workflows", "approvals",
    }  # fmt: skip
    held = [
        (name, at)
        for name, data in files.items()
        for at in range(len(data))
        if verify_bundle(_with(files, name, at, data[at] ^ 0x01)).valid
    ]
    assert held == []


def test_a_workflow_bundle_missing_its_file_or_approval_fails(chat, tmp_path, capsys):
    run_id, files = _workflow_files(chat, tmp_path, capsys)
    [workflow] = [n for n in files if n.startswith("workflows/")]
    [signed] = [n for n in files if n.startswith("approvals/")]
    for name, said in (
        (workflow, "missing, and the seal signs its sha256"),
        (signed, "missing, and the seal names it the approval its run went under"),
    ):
        problems = verify_bundle({k: v for k, v in files.items() if k != name}).problems
        assert f"{name}: {said}" in problems, problems
        assert not verify_bundle(_relisted({k: v for k, v in files.items() if k != name})).valid


def test_a_workflow_bundle_with_a_file_it_does_not_list_fails(chat, tmp_path, capsys):
    run_id, files = _workflow_files(chat, tmp_path, capsys)
    [signed] = [n for n in files if n.startswith("approvals/")]
    for name in (f"workflows/{'0' * 64}.json", "approvals/wfa-0-x.json", "approvals/a/b.json"):
        problems = verify_bundle(_relisted({**files, name: files[signed]})).problems
        assert f"{name}: a file a metabot.bundle/1 bundle does not have" in problems, problems


def test_a_run_or_a_ladder_bundle_holds_no_workflow_file_or_approval(chat, tmp_path, capsys):
    ladder, files = _ladder_files(chat, tmp_path, capsys)
    for name in (f"workflows/{'0' * 64}.json", "approvals/wfa-0-x.json"):
        problems = verify_bundle(_relisted({**files, name: b"{}\n"})).problems
        assert problems == [f"{name}: a file a metabot.bundle/1 bundle does not have"]


def test_an_approval_swapped_for_another_valid_one_of_the_workflow_fails(chat, tmp_path, capsys):
    from metabot.workflow_approval import approve
    from metabot.workflows import load_workflow

    run_id, files = _workflow_files(chat, tmp_path, capsys)
    [signed] = [n for n in files if n.startswith("approvals/")]
    other = approve(load_workflow("date-fix")).receipt.read_bytes()  # a second, also valid
    problems = verify_bundle(_relisted({**files, signed: other})).problems
    assert problems == [f"{signed}: not the approval receipt the seal signs by its sha256"]


def _forged(tmp_path: Path, monkeypatch, *, approves: str | None = None,
            run_detail: str | None = None, firing: str | None = None,
            other_key: bool = False, our_did: bool = False, tamper: bool = False,
            members=(), twice: bool = False):  # fmt: skip
    """A workflow seal signed by this home over the verdicts given, its approval kept here."""
    from metabot.seals import seal_runs
    from metabot.workflow_approval import approve
    from metabot.workflows import FORMAT, store_workflow

    step = {"name": "say", "kind": "ask", "prompt": "Say hi.", "engine": "openai"}
    data = {"format": FORMAT, "name": "date-fix", "cwd": str(tmp_path), "steps": [step]}
    wf = store_workflow(json.dumps(data).encode())
    signed_for = wf
    if approves is not None:
        signed_for = store_workflow(json.dumps({**data, "about": approves}).encode())
    ours = os.environ["ORCHA_KEY_PATH"]
    if other_key:
        monkeypatch.setenv("ORCHA_KEY_PATH", str(tmp_path / "another-key"))
    approval = approve(signed_for)
    other, _other_did = ensure_key()
    monkeypatch.setenv("ORCHA_KEY_PATH", ours)
    if our_did:  # the other key's receipt, under this home's DID and signed again: it verifies
        from emerge.run_attestation import compute_envelope_digest, verify_run_attestation

        _key, did = ensure_key()
        envelope = json.loads(approval.receipt.read_text())
        envelope["signer"]["did"], envelope["agent_dids"] = did, [did]
        digest = compute_envelope_digest(envelope).encode()
        envelope["signature"] = base64.b64encode(other.sign(digest)).decode()
        assert verify_run_attestation(envelope).valid
        approval.receipt.write_text(json.dumps(envelope, indent=2) + "\n")
    if tamper:  # its bytes changed by hand: the seal signs their digest, the signature fails
        text = approval.receipt.read_text().replace("surface=cli", "surface=web")
        approval.receipt.write_text(text)
    detail = (
        run_detail
        if run_detail is not None
        else (
            f"sha256={wf.sha256};approval={approval.run_id};approval_receipt={approval.digest};"
            "steps=1;ran=0;spent_musd=0"
        )
    )
    verdicts = [{"check": "workflow_run", "result": "pass", "detail": detail}] * (2 if twice else 1)
    if firing is not None:
        verdicts.append({"check": "workflow_firing", "result": "pass", "detail": firing})
    seal = "mb-20261104T000000Z-f0f0f0"
    seal_runs(seal, list(members), delivered=True, kind_detail="kind=workflow;name=date-fix",
              verdicts=verdicts, started_at="2026-11-04T00:00:00Z", meta={})  # fmt: skip
    return wf, approval, bundles.build_bundle(seal)


def test_an_approval_of_another_workflow_fails(tmp_path, monkeypatch):
    wf, approval, files = _forged(tmp_path, monkeypatch, approves="other bytes")
    assert verify_bundle(files).problems == [
        f"approvals/{approval.run_id}.json: it does not approve the workflow sha256 the seal signs"
    ]


def test_an_approval_signed_with_another_key_fails(tmp_path, monkeypatch):
    wf, approval, files = _forged(tmp_path, monkeypatch, other_key=True)
    assert verify_bundle(files).problems == [
        f"approvals/{approval.run_id}.json: signed by another key than the seal's"
    ]


def test_an_approval_another_key_signed_under_the_seal_s_did_fails(tmp_path, monkeypatch):
    _wf, approval, files = _forged(tmp_path, monkeypatch, other_key=True, our_did=True)
    signed = f"approvals/{approval.run_id}.json"
    seal = json.loads(files["receipts/mb-20261104T000000Z-f0f0f0.json"])
    assert json.loads(files[signed])["signer"]["did"] == seal["signer"]["did"]
    assert verify_bundle(files).problems == [f"{signed}: signed by another key than the seal's"]


def test_a_firing_whose_lineage_names_another_approval_fails(tmp_path, monkeypatch):
    lineage = "sha256=x;approval=wfa-y;approval_receipt=z"
    _wf, _approval, files = _forged(tmp_path, monkeypatch, firing=lineage)
    seal = "mb-20261104T000000Z-f0f0f0"
    assert verify_bundle(files).problems == [
        f"receipts/{seal}.json: its workflow_firing verdict names another workflow or "
        "approval than its workflow_run verdict"
    ]


@pytest.mark.parametrize(
    "detail",
    ["", "sha256=zz;approval=wfa-1;approval_receipt=00", "approval=wfa-1",
     f"sha256={'a' * 64};approval=../x;approval_receipt={'b' * 64}",
     f"sha256={'a' * 64};approval=mb-1;approval_receipt={'b' * 64}",
     f"sha256=zz;approval=wfa-1;approval_receipt={'b' * 64}",
     f"sha256={'a' * 64};approval=wfa-1;approval_receipt=00",
     f"sha256={'a' * 64};sha256={'a' * 64};approval=wfa-1;approval_receipt={'b' * 64}"],
)  # fmt: skip
def test_a_workflow_seal_whose_workflow_run_verdict_does_not_read_fails(
    tmp_path, monkeypatch, detail
):
    _wf, _approval, files = _forged(tmp_path, monkeypatch, run_detail=detail)
    seal = "mb-20261104T000000Z-f0f0f0"
    problems = verify_bundle(files).problems
    assert (
        f"receipts/{seal}.json: its workflow_run verdict does not name a workflow sha256, "
        "an approval and its receipt's sha256"
    ) in problems, problems


def test_export_says_when_this_machine_takes_the_approval_as_revoked(chat, tmp_path, capsys):
    from metabot.workflow_approval import approval_state, revoke

    wf, approval, run_id = _workflow(chat, tmp_path)
    revoke(wf.sha256, approval_state(wf.sha256).in_force)
    said = _export(capsys, run_id, tmp_path / "out", journals=False)
    assert (
        f"  this machine takes approval {approval.run_id} as revoked now; "
        "the bundle holds no revoke"
    ) in said
    assert _verify(capsys, tmp_path / "out")[0] == 0  # what the bundle proves still holds


def test_export_refuses_a_workflow_whose_kept_file_changed(chat, tmp_path, capsys):
    from metabot.workflows import kept_path

    wf, _approval, run_id = _workflow(chat, tmp_path)
    kept_path(wf.sha256).write_bytes(wf.raw.replace(b"Say hi.", b"Say rm."))
    assert main(["receipts", "export", run_id, "--out", str(tmp_path / "x")]) == 1
    err = capsys.readouterr().err
    assert f"workflows/{wf.sha256}.json: not the bytes of the sha256 the seal signs" in err
    assert not (tmp_path / "x").exists()


def test_an_approval_whose_signature_fails_fails_though_the_seal_signs_its_digest(
    tmp_path, monkeypatch
):
    _wf, approval, files = _forged(tmp_path, monkeypatch, tamper=True)
    assert verify_bundle(files).problems == [
        f"approvals/{approval.run_id}.json: does not verify: its hashes or its signature do not "
        "hold"
    ]


def test_an_approval_receipt_of_another_id_fails(tmp_path, monkeypatch):
    from metabot.workflow_approval import approve

    wf, approval, files = _forged(tmp_path, monkeypatch)
    other = approve(wf)
    seal_id = "mb-20261104T000000Z-f0f0f1"
    from metabot.seals import seal_runs

    detail = (
        f"sha256={wf.sha256};approval={approval.run_id};approval_receipt={other.digest};"
        "steps=1;ran=0;spent_musd=0"
    )
    seal_runs(seal_id, [], delivered=True, kind_detail="kind=workflow;name=date-fix",
              verdicts=[{"check": "workflow_run", "result": "pass", "detail": detail}],
              started_at="2026-11-04T00:00:00Z", meta={})  # fmt: skip
    files = bundles.build_bundle(seal_id)
    name = f"approvals/{approval.run_id}.json"
    swapped = _relisted({**files, name: other.receipt.read_bytes()})
    assert verify_bundle(swapped).problems == [f"{name}: the receipt of run {other.run_id}"]


def test_a_workflow_step_that_is_a_seal_of_another_kind_fails(tmp_path, monkeypatch):
    from metabot.seals import Member, seal_runs

    run = _run(tmp_path, monkeypatch)
    inner = "mb-20261104T000000Z-1a1a1a"
    seal_runs(inner, [Member(run, "rung", receipt_path(run), True)], delivered=True,
              kind_detail="kind=workflow;name=inner", verdicts=[],
              started_at="2026-11-04T00:00:00Z", meta={})  # fmt: skip
    step = Member(inner, "step", receipt_path(inner), True)
    _wf, _approval, files = _forged(tmp_path, monkeypatch, members=[step])
    assert verify_bundle(files).problems == [
        f"receipts/{inner}.json: a seal of a kind no workflow step makes"
    ]


def test_a_workflow_seal_that_signs_two_workflow_run_verdicts_fails(tmp_path, monkeypatch):
    _wf, _approval, files = _forged(tmp_path, monkeypatch, twice=True)
    seal = "mb-20261104T000000Z-f0f0f0"
    assert verify_bundle(files).problems == [
        f"receipts/{seal}.json: a workflow's seal signs one workflow_run verdict, and this signs 2"
    ]
