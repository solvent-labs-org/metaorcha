"""Fake web_data: counts receipt_state calls in $METABOT_HOME/calls.log."""
import json
import os
from pathlib import Path


def _log(what):
    with open(Path(os.environ["METABOT_HOME"]) / "calls.log", "a") as f:
        f.write(what + "\n")


def is_safe_id(x):
    return isinstance(x, str) and x.isalnum()


def receipt_state(run_id):
    _log(f"state {run_id}")
    return "valid" if (Path(os.environ["METABOT_HOME"]) / "receipts" / f"{run_id}.json").is_file() else "no receipt"


def run_rows():
    rows = [json.loads(l) for l in (Path(os.environ["METABOT_HOME"]) / "runs.jsonl").read_text().splitlines() if l.strip()]
    newest = list(reversed(rows))
    for r in newest:
        r["state"] = receipt_state(r.get("run_id"))
    return newest, len(rows), None
