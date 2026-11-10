import os
from pathlib import Path


def receipts_dir():
    return Path(os.environ["METABOT_HOME"]) / "receipts"


def run_path(run_id):
    return Path(os.environ["METABOT_HOME"]) / "runs" / f"{run_id}.jsonl"
