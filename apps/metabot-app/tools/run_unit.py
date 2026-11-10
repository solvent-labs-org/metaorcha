#!/usr/bin/env python3
"""Run the unit suite under the clean-testing rule (2026-11-09).

The run fails when:
  * any test fails;
  * any test leaves a process, socket or file open (a ResourceWarning anywhere in the output);
  * any test leaves something behind in its own temporary folder.

The suite gets a private TMPDIR. It is removed afterwards only when it is empty, so a leak is
never hidden by the cleanup. Output streams through unchanged.

    python3 tools/run_unit.py [extra unittest args]
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

APP = Path(__file__).resolve().parents[1]


def main(argv: list[str]) -> int:
    tmp = Path(tempfile.mkdtemp(prefix="metabot-app-unit-"))
    env = dict(os.environ, TMPDIR=str(tmp))
    cmd = [sys.executable, "-B", "-X", "tracemalloc=5", "-W", "always::ResourceWarning",
           "-m", "unittest", "discover", "-s", "tests", *argv]
    p = subprocess.Popen(cmd, cwd=APP, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    leaks = 0
    assert p.stdout is not None
    for line in p.stdout:
        sys.stdout.write(line)
        if "ResourceWarning" in line:
            leaks += 1
    status = p.wait()
    left = sorted(x.name for x in tmp.iterdir())
    if leaks:
        print(f"FAIL: {leaks} resource warning(s): a test left a process, socket or file open")
        status = status or 1
    if left:
        print(f"FAIL: tests left {len(left)} item(s) in their temp folder {tmp}: {', '.join(left[:10])}")
        status = status or 1
    else:
        tmp.rmdir()
    if status == 0:
        print("clean: no failures, no leaked processes, sockets or files, nothing left in the temp folder")
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
