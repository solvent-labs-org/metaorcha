"""FR-8 (story 3.2): no string describes the record as a complete transcript.

A receipt commits to the tool calls in its steps and nothing else (the SDK's
``COVERAGE_STATEMENT``); model calls, planning and refused calls are not in
it. So no UI string, doc or code comment may call it a complete transcript,
a full log or a complete record. Text is matched with whitespace and
line-leading comment markers collapsed, so a phrase split across two lines of
a comment or a Markdown paragraph is still found.

``README.md`` is frozen under ADR 0008 and is not swept here; it is checked
when it next changes.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SWEPT = (
    "frontend/src",
    "frontend/TRUST-INDICATORS.md",
    "docs",
    "sdk/src",
    "sdk/README.md",
    "services",
    "common",
    "scripts",
    "apps",
)
SUFFIXES = {".py", ".ts", ".tsx", ".md", ".mdx", ".html", ".txt", ".yaml", ".yml"}
SKIP_PARTS = {
    "node_modules",
    ".venv",
    "__pycache__",
    "generated_client",
    "dist",
    "tests",  # tests quote the phrases they forbid
}
BANNED = re.compile(
    r"\b(complete|full|entire)\s+(transcript|log|record|audit\s+trail)s?\b"
    r"|\bevery\s+action\s+the\s+agent\b"
    r"|\beverything\s+the\s+agent\s+did\b",
    re.IGNORECASE,
)
# Reviewed and kept, each with its reason. Keyed by (path, normalised phrase).
ALLOWED = {
    # RFC 0003 §Alternatives: "complete" qualifies the list of steps a
    # verifier walks (the linear chain against a membership proof), not the
    # record's coverage. Changing an Accepted RFC's text is an amendment.
    ("docs/spec/rfcs/0003-run-attestation-envelope.md", "complete transcript"),
}
_COMMENT_LEAD = re.compile(r"^\s*(?:#+|//+|/?\*+|-|>|\|)?\s*")


def _normalised(text: str) -> str:
    return " ".join(_COMMENT_LEAD.sub("", line) for line in text.splitlines())


def _files() -> list[Path]:
    out: list[Path] = []
    for rel in SWEPT:
        base = ROOT / rel
        candidates = [base] if base.is_file() else base.rglob("*")
        for path in candidates:
            if (
                path.is_file()
                and path.suffix in SUFFIXES
                and not SKIP_PARTS & set(path.relative_to(ROOT).parts)
            ):
                out.append(path)
    return out


def _hits() -> list[tuple[str, str]]:
    hits = []
    for path in _files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        rel = path.relative_to(ROOT).as_posix()
        for match in BANNED.finditer(_normalised(text)):
            phrase = " ".join(match.group(0).lower().split())
            hits.append((rel, phrase))
    return hits


def test_no_string_calls_the_record_complete() -> None:
    assert len(_files()) > 100  # the sweep actually reached the tree
    unexpected = [hit for hit in _hits() if hit not in ALLOWED]
    assert unexpected == []


def test_every_allowed_hit_still_exists() -> None:
    # an exception whose text is gone is removed, not left to excuse a new hit
    assert set(_hits()) >= ALLOWED


def test_the_sweep_finds_a_phrase_split_across_comment_lines() -> None:
    sample = "# the receipt is a complete\n# transcript of the run\n"
    assert BANNED.search(_normalised(sample))
    sample = "/**\n * keeps the full\n * log of every call\n */"
    assert BANNED.search(_normalised(sample))
    assert not BANNED.search(_normalised("a completed transcript row"))
