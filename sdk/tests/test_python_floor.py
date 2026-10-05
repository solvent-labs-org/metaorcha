"""The SDK runs on the Python it declares (``requires-python``), not only on CI's.

CI runs the SDK suite on 3.12 alone, and the repo lints as py312, whose
pyupgrade rules rewrite code into 3.11+ forms. That is how ``datetime.UTC``
(3.11) reached ``emerge.cli`` and shipped in 0.1.1–0.1.3 while the package
declared ``>=3.10``: on a 3.10 interpreter every console script (``orcha``,
``orcha-sdk``, ``emerge``) died importing the CLI.

Two checks, both static because no 3.10 interpreter runs here: the lint target
for ``sdk/`` stays at the declared floor, and no SDK module uses a standard
library name newer than it. The second is a denylist of the names most likely
to arrive (by hand or by a pyupgrade fix); it is not a proof of 3.10
compatibility — a 3.10 leg in CI would be.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SDK_SRC = REPO / "sdk" / "src" / "emerge"

# (module, name) -> the Python version that introduced it
_NEWER_THAN_310 = {
    ("datetime", "UTC"): "3.11",
    ("enum", "StrEnum"): "3.11",
    ("typing", "Self"): "3.11",
    ("typing", "LiteralString"): "3.11",
    ("typing", "Never"): "3.11",
    ("typing", "assert_never"): "3.11",
    ("typing", "reveal_type"): "3.11",
    ("typing", "override"): "3.12",
    ("asyncio", "TaskGroup"): "3.11",
    ("asyncio", "timeout"): "3.11",
    ("hashlib", "file_digest"): "3.11",
    ("itertools", "batched"): "3.12",
}
_NEWER_MODULES = {"tomllib": "3.11"}


def _declared_floor() -> str:
    text = (REPO / "sdk" / "pyproject.toml").read_text()
    match = re.search(r'^requires-python\s*=\s*">=\s*(\d+\.\d+)"', text, re.M)
    assert match, "sdk/pyproject.toml declares no requires-python floor"
    return match.group(1)


def test_the_sdk_is_linted_against_its_declared_floor():
    floor = _declared_floor()
    assert floor == "3.10", "the floor moved: update this test and the lint target"
    root = (REPO / "pyproject.toml").read_text()
    # without this, pyupgrade (UP017 and friends) rewrites the SDK into 3.11+ forms
    assert re.search(
        r'per-file-target-version\s*=\s*\{[^}]*"sdk/\*\*/\*\.py"\s*=\s*"py310"', root
    ), "root ruff config must lint sdk/ as py310"


def _uses(tree: ast.AST) -> list[tuple[str, str, int]]:
    found = []
    modules: dict[str, str] = {}  # local alias -> module
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in _NEWER_MODULES:
                    found.append((alias.name, "", node.lineno))
                modules[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            if node.module in _NEWER_MODULES:
                found.append((node.module, "", node.lineno))
            for alias in node.names:
                if (node.module, alias.name) in _NEWER_THAN_310:
                    found.append((node.module, alias.name, node.lineno))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and (modules.get(node.value.id), node.attr) in _NEWER_THAN_310
        ):
            found.append((modules[node.value.id], node.attr, node.lineno))
        if isinstance(node, getattr(ast, "TryStar", ())):  # except* is 3.11
            found.append(("<syntax>", "except*", node.lineno))
    return found


def test_no_sdk_module_uses_a_stdlib_name_newer_than_the_floor():
    offenders = [
        f"{path.relative_to(REPO)}:{line} {module}.{name}".rstrip(".")
        for path in sorted(SDK_SRC.rglob("*.py"))
        for module, name, line in _uses(ast.parse(path.read_text()))
    ]
    assert offenders == [], offenders


def test_the_denylist_sees_both_spellings_of_the_name_that_shipped():
    # the check is only worth something if it catches what actually happened
    assert _uses(ast.parse("from datetime import UTC, datetime"))
    assert _uses(ast.parse("import datetime\nx = datetime.UTC"))
    assert _uses(ast.parse("import tomllib"))
    assert not _uses(ast.parse("from datetime import datetime, timezone"))
