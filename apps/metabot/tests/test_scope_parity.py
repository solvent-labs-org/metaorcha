"""MetaBot and the platform class every capability the same way, so one classifier."""

from __future__ import annotations

from pathlib import Path

import pytest

from metabot import scope_classes

REPO = Path(__file__).resolve().parents[3]
PLATFORM = REPO / "services/superagent/src/superagent/middleware/scope_classes.py"
OURS = Path(scope_classes.__file__)
HEADER_LINES = 3


def test_vendored_matches_superagent():
    if not PLATFORM.exists():
        pytest.skip(f"platform classifier not present at {PLATFORM} (installed outside the repo)")
    ours = OURS.read_bytes().split(b"\n", HEADER_LINES)[HEADER_LINES]
    assert ours == PLATFORM.read_bytes(), (
        "metabot/scope_classes.py has drifted from the platform's classifier; "
        "re-vendor it: one classifier"
    )


def test_unrecognised_is_destructive():
    assert scope_classes.platform_rule("frobnicate") is scope_classes.ScopeClass.DESTRUCTIVE
    assert scope_classes.platform_rule("list_issues") is scope_classes.ScopeClass.READ
    assert scope_classes.platform_rule("create_issue") is scope_classes.ScopeClass.WRITE
    assert scope_classes.platform_rule("delete_repo") is scope_classes.ScopeClass.DESTRUCTIVE
