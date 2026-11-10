#!/usr/bin/env python3
"""report.md and an exit code from a self-test report.jsonl.

    python3 tools/selftest_report.py REPORT.jsonl [--enforce-budgets] [--md OUT.md]

Exit: 0 clean, 1 a failed step, 2 a budget miss (with --enforce-budgets), 3 incomplete.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from metabot_app.selftest import report  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("report")
    ap.add_argument("--enforce-budgets", action="store_true")
    ap.add_argument("--md", default="")
    a = ap.parse_args(argv)
    lines = report.read(Path(a.report))
    md = report.render_md(lines)
    if a.md:
        Path(a.md).write_text(md, encoding="utf-8")
    else:
        sys.stdout.write(md)
    return report.verdict(lines, a.enforce_budgets)


if __name__ == "__main__":
    sys.exit(main())
