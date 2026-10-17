"""The version the SDK reports is the one it is published as."""

from __future__ import annotations

import re
from pathlib import Path

import emerge


def test_the_module_reports_the_published_version():
    text = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
    assert match, "sdk/pyproject.toml declares no version"
    assert emerge.__version__ == match.group(1)
