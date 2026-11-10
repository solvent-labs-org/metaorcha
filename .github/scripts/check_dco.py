#!/usr/bin/env python3
"""DCO check: every commit in a range carries a ``Signed-off-by:`` trailer whose email is
the commit's author email (compared without case).

    python3 .github/scripts/check_dco.py <base>..<head>

Merge commits are skipped: they carry no new authored change, and the branch-update merge
the hosting site writes has no sign-off. Each commit they bring in is checked on its own.

Exit 0 when every commit passes (or the range is empty), 1 when any fails, 2 on a usage
or git error. Standard library only; no third-party app sees the repository.
"""
from __future__ import annotations

import re
import subprocess
import sys

REC = "\x1e"
SEP = "\x1f"
FORMAT = SEP.join(["%H", "%P", "%ae", "%(trailers:key=Signed-off-by,valueonly,unfold)"]) + REC
EMAIL = re.compile(r"<([^<>\s]+@[^<>\s]+)>\s*$")


def commits(rng: str) -> list[tuple[str, list[str], str, list[str]]]:
    out = subprocess.run(["git", "log", f"--format={FORMAT}", rng, "--"],
                         capture_output=True, text=True, check=True).stdout
    rows = []
    for rec in out.split(REC):
        rec = rec.strip("\n")
        if not rec:
            continue
        sha, parents, email, trailers = rec.split(SEP, 3)
        rows.append((sha, parents.split(), email, [t.strip() for t in trailers.splitlines() if t.strip()]))
    return rows


def main(argv: list[str]) -> int:
    if len(argv) != 1 or ".." not in argv[0]:
        print(__doc__.strip().splitlines()[2].strip(), file=sys.stderr)
        return 2
    try:
        rows = commits(argv[0])
    except subprocess.CalledProcessError as e:
        print(f"error: git log {argv[0]} failed: {e.stderr.strip()}", file=sys.stderr)
        return 2
    if not rows:
        print(f"no commits in {argv[0]}")
        return 0
    failed = 0
    for sha, parents, email, signoffs in reversed(rows):
        short = sha[:12]
        if len(parents) > 1:
            print(f"skip {short} merge commit")
            continue
        emails = {m.group(1).lower() for s in signoffs if (m := EMAIL.search(s))}
        if email.lower() in emails:
            print(f"ok   {short} signed off by its author <{email}>")
            continue
        failed += 1
        why = "no Signed-off-by trailer" if not signoffs else "no Signed-off-by matches the author"
        print(f"FAIL {short} {why} <{email}>")
    if failed:
        print(f"\n{failed} commit(s) fail the DCO. Sign off each with `git commit -s` "
              "(for existing commits: `git rebase --signoff <base>`), using your author email.")
        return 1
    print(f"\nall {len(rows)} commit(s) pass the DCO")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
