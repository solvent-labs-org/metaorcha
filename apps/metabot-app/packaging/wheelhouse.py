#!/usr/bin/env python3
"""D3: the wheelhouse. Builds the sdk, the core and the app as wheels, downloads their
pinned dependencies as wheels only (never an sdist) for one platform, and writes a
``requirements.lock`` with a sha256 per file, so a desktop bundle installs offline with
``pip install --no-index --find-links wheels/ --require-hashes -r wheels/requirements.lock``.

    python3 packaging/wheelhouse.py --platform linux-x86_64|macos-arm64 --out wheels/ [--python 3.12]
                                    [--root REPO] [--dry-run]

Standard library only. The network is used by ``pip download`` alone; CI runs it, a
developer machine runs ``--dry-run``. pip evaluates the lock's environment markers
(``sys_platform``, ...) against the interpreter running it, not the target, so each
platform's wheelhouse is built on a runner of that OS. Exit 0 when the lock is written, 1 when a step fails,
2 for a usage error.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
from pathlib import Path

PACKAGES = ("sdk", "apps/metabot", "apps/metabot-app")   # relative to the repo root, build order
# Every wheel tag a bundle accepts, preferred first. pip takes a ``manylinux_2_N`` tag
# literally (only ``manylinux2014``/``2010`` expand to the older names), and most native
# wheels on PyPI are tagged ``manylinux_2_17.manylinux2014``, so the list names them all.
# The Linux floor is glibc 2.28 (RHEL 8, Debian 10, Ubuntu 18.10 and newer); no 2_34 wheel.
# A macOS tag expands downwards by itself (and to universal2).
PLATFORMS = {
    "linux-x86_64": ["manylinux_2_28_x86_64", "manylinux_2_17_x86_64", "manylinux2014_x86_64"],
    "macos-arm64": ["macosx_11_0_arm64"],
}
LOCK = "requirements.lock"
WHEEL = re.compile(r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._]*)-(?P<version>[0-9][^-]*)-[^-]+-[^-]+-[^-]+\.whl$")


class WheelhouseError(Exception):
    pass


def repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def name_version(filename: str) -> tuple[str, str]:
    m = WHEEL.match(filename)
    if not m:
        raise WheelhouseError(f"not a wheel file name: {filename}")
    return re.sub(r"[-_.]+", "-", m.group("name")).lower(), m.group("version")


def refuse_sdists(out: Path) -> list[Path]:
    """Every file in ``out`` that is not a wheel is an error (an sdist would compile on
    the user's machine, or fail to). Returns the wheels."""
    wheels, others = [], []
    for p in sorted(out.iterdir()):
        if p.name == LOCK or p.name.startswith("."):
            continue
        (wheels if p.suffix == ".whl" and p.is_file() else others).append(p)
    if others:
        raise WheelhouseError("not wheels: " + ", ".join(p.name for p in others))
    return wheels


def lock_text(out: Path) -> str:
    rows = []
    for p in refuse_sdists(out):
        name, version = name_version(p.name)
        rows.append((name, version, hashlib.sha256(p.read_bytes()).hexdigest()))
    rows.sort()
    return "".join(f"{name}=={version} \\\n    --hash=sha256:{digest}\n" for name, version, digest in rows)


def write_lock(out: Path) -> Path:
    path = out / LOCK
    path.write_text(lock_text(out), encoding="utf-8")
    return path


def build_argvs(root: Path, out: Path) -> list[list[str]]:
    return [["uv", "build", "--wheel", "--out-dir", str(out), str(root / p)] for p in PACKAGES]


def export_argv(root: Path, requirements: Path) -> list[str]:
    return ["uv", "export", "--frozen", "--package", "orcha-metabot", "--no-emit-project",
            "--no-emit-workspace", "--no-hashes", "--no-dev", "-o", str(requirements), "--directory", str(root)]


def download_argv(platform: str, python: str, out: Path, requirements: Path) -> list[str]:
    tags = PLATFORMS.get(platform)
    if tags is None:
        raise WheelhouseError(f"unknown platform {platform!r}; one of {', '.join(PLATFORMS)}")
    abi = "cp" + python.replace(".", "")
    argv = [sys.executable, "-m", "pip", "download", "--only-binary=:all:"]
    for tag in tags:
        argv += ["--platform", tag]
    argv += ["--python-version", python, "--implementation", "cp", "--abi", abi, "--abi", "abi3",
             "--abi", "none", "--dest", str(out), "-r", str(requirements)]
    return argv


def _run(argv: list[str], dry: bool) -> None:
    print("$ " + " ".join(argv), flush=True)
    if dry:
        return
    r = subprocess.run(argv, check=False)
    if r.returncode != 0:
        raise WheelhouseError(f"{argv[0]} exited {r.returncode}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="build the offline wheelhouse for one platform")
    ap.add_argument("--platform", required=True, help=", ".join(PLATFORMS))
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--python", default="3.12", help="the interpreter version the bundle ships")
    ap.add_argument("--root", type=Path, default=None, help="the repo root (default: found from this file)")
    ap.add_argument("--dry-run", action="store_true", help="print the commands; download and build nothing")
    args = ap.parse_args(argv)
    if args.platform not in PLATFORMS:
        print(f"wheelhouse: unknown platform {args.platform!r}; one of {', '.join(PLATFORMS)}", file=sys.stderr)
        return 2
    root = (args.root or repo_root()).resolve()
    out = args.out
    requirements = out / "requirements.in"
    try:
        if not args.dry_run:
            out.mkdir(parents=True, exist_ok=True)
        for build in build_argvs(root, out):
            _run(build, args.dry_run)
        _run(export_argv(root, requirements), args.dry_run)
        _run(download_argv(args.platform, args.python, out, requirements), args.dry_run)
        if args.dry_run:
            print(f"(dry run) would write {out / LOCK}")
            return 0
        requirements.unlink(missing_ok=True)
        wheels = refuse_sdists(out)
        path = write_lock(out)
        print(f"wrote {path}: {len(wheels)} wheels for {args.platform}, python {args.python}")
        return 0
    except WheelhouseError as exc:
        print(f"wheelhouse: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
