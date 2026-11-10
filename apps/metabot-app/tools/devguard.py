#!/usr/bin/env python3
"""devguard: keep a development machine clean while building and testing the app.

    python3 tools/devguard.py            # all checks
    python3 tools/devguard.py --disk     # free space on the root volume is above the floor
    python3 tools/devguard.py --scratch  # the scratch folder is under its cap
    python3 tools/devguard.py --procs    # nothing is left listening on the test ports,
                                         # and no tmux server runs on the test socket

Exit 0 when every requested check passes, 1 otherwise. Prints one line per check.
Settings come from the environment: MBAPP_DISK_FLOOR_GB (default 4),
MBAPP_SCRATCH (default /var/tmp/metabot-app), MBAPP_SCRATCH_CAP_MB (default 1536),
MBAPP_TEST_PORTS (default 8471-8479), MBAPP_TMUX_SOCKET (default mb-test).
"""
from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path


def disk_ok(path: str = "/", floor_gb: float | None = None) -> tuple[bool, str]:
    floor = float(os.environ.get("MBAPP_DISK_FLOOR_GB", "4")) if floor_gb is None else floor_gb
    free = shutil.disk_usage(path).free / 1e9
    return free >= floor, f"disk: {free:.1f} GB free on {path} (floor {floor:g} GB)"


def folder_mb(root: Path) -> float:
    total = 0
    for dirpath, _dirs, files in os.walk(root, onerror=lambda e: None):
        for name in files:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                pass
    return total / 1e6


def scratch_ok(root: Path | None = None, cap_mb: float | None = None) -> tuple[bool, str]:
    root = Path(os.environ.get("MBAPP_SCRATCH", "/var/tmp/metabot-app")) if root is None else root
    cap = float(os.environ.get("MBAPP_SCRATCH_CAP_MB", "1536")) if cap_mb is None else cap_mb
    if not root.exists():
        return True, f"scratch: {root} absent"
    used = folder_mb(root)
    return used <= cap, f"scratch: {used:.0f} MB in {root} (cap {cap:g} MB)"


def parse_ports(spec: str) -> list[int]:
    out: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            lo, hi = part.split("-", 1)
            out.extend(range(int(lo), int(hi) + 1))
        elif part:
            out.append(int(part))
    return out


def port_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.2)
        return s.connect_ex((host, port)) == 0


def procs_ok(ports: list[int] | None = None, tmux_socket: str | None = None) -> tuple[bool, str]:
    ports = parse_ports(os.environ.get("MBAPP_TEST_PORTS", "8471-8479")) if ports is None else ports
    sock = os.environ.get("MBAPP_TMUX_SOCKET", "mb-test") if tmux_socket is None else tmux_socket
    busy = [p for p in ports if port_open(p)]
    tmux_alive = False
    if shutil.which("tmux"):
        r = subprocess.run(["tmux", "-L", sock, "list-sessions"], capture_output=True, text=True)
        tmux_alive = r.returncode == 0 and bool(r.stdout.strip())
    problems = []
    if busy:
        problems.append("listening on test ports " + ",".join(map(str, busy)))
    if tmux_alive:
        problems.append(f"tmux sessions alive on socket {sock}")
    return not problems, "procs: " + ("; ".join(problems) if problems else "nothing left running")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--disk", action="store_true")
    ap.add_argument("--scratch", action="store_true")
    ap.add_argument("--procs", action="store_true")
    a = ap.parse_args(argv)
    checks = [c for c, on in (("disk", a.disk), ("scratch", a.scratch), ("procs", a.procs)) if on] or ["disk", "scratch", "procs"]
    fns = {"disk": disk_ok, "scratch": scratch_ok, "procs": procs_ok}
    ok = True
    for name in checks:
        good, line = fns[name]()
        print(("ok    " if good else "FAIL  ") + line)
        ok = ok and good
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
