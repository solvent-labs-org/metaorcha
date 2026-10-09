"""``counts_match`` — a run-level declared criterion (story 2.4, FR-25, AD-19).

Two sources a routine declared (``"<connection DID>#<capability>"``) each
report a count; the check passes only when both counts are readable and
equal. It is evaluated at seal, over the run's accumulated steps, and signed
as one verdict whose ``detail`` names both counts and both sources::

    left=11 right=10 key=open_prs left_source=did:...#list_issues left_path=/total_count ...

Reading a source:

- the **last** step whose ``tool`` is the source and that succeeded is read —
  a re-read replaces an earlier one; a failed call never counts;
- only the AD-16 pre-image is read (a 280-char display copy is not a count);
- a JSON string node is decoded (an MCP text block ``"11"`` is the number 11);
- the operand path is an RFC 6901 JSON Pointer and is required: counted
  whole, a multi-block MCP output would count its blocks, not its data;
- the node it reaches is a count when it is an integer >= 0 (never a bool) or
  a list (its length). Anything else is unreadable, and unreadable fails.

The module is total: it never raises, and the ``detail`` is printable ASCII,
so the check can never stop a run from sealing.
"""

from __future__ import annotations

import json
from typing import Any

CHECK = "counts_match"

NEVER_CALLED = "never called"
NO_SUCCESS = "no successful call"
NO_PREIMAGE = "no pre-image"
NOT_JSON = "not JSON"
PATH_NOT_FOUND = "path not found"
NOT_A_COUNT = "not a count"
NO_PATH = "no path declared"

_NO_OPERANDS = "no operands declared"
_NOT_EVALUATED = "counts_match could not be evaluated"


class _Unreadable(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _decoded(node: Any) -> Any:
    if not isinstance(node, str):
        return node
    try:
        return json.loads(node)
    except Exception:
        raise _Unreadable(NOT_JSON) from None


def _tokens(path: str) -> list[str]:
    if path == "":
        return []
    if not path.startswith("/"):
        raise _Unreadable(PATH_NOT_FOUND)
    return [t.replace("~1", "/").replace("~0", "~") for t in path[1:].split("/")]


def _child(node: Any, token: str) -> Any:
    if isinstance(node, dict):
        if token not in node:
            raise _Unreadable(PATH_NOT_FOUND)
        return node[token]
    if isinstance(node, list):
        # RFC 6901 array index: "0" or digits without a leading zero.
        if not token.isdigit() or (len(token) > 1 and token[0] == "0"):
            raise _Unreadable(PATH_NOT_FOUND)
        index = int(token)
        if index >= len(node):
            raise _Unreadable(PATH_NOT_FOUND)
        return node[index]
    raise _Unreadable(PATH_NOT_FOUND)


def read_count(output: Any, path: str, *, has_preimage: bool) -> tuple[int | None, str]:
    """``(count, "")`` or ``(None, reason)`` for one source's output."""
    if not has_preimage:
        return None, NO_PREIMAGE
    try:
        node = _decoded(output)
        for token in _tokens(path):
            node = _decoded(_child(node, token))
    except _Unreadable as unreadable:
        return None, unreadable.reason
    if isinstance(node, bool):
        return None, NOT_A_COUNT
    if isinstance(node, int) and node >= 0:
        return node, ""
    if isinstance(node, list):
        return len(node), ""
    return None, NOT_A_COUNT


def pick_source(steps: list[Any], source: str) -> tuple[Any | None, str]:
    """The last successful step calling ``source``, or ``(None, reason)``."""
    called = False
    for step in reversed(steps):
        if getattr(step, "tool", None) != source:
            continue
        called = True
        if getattr(step, "success", False) is True:
            return step, ""
    return None, NO_SUCCESS if called else NEVER_CALLED


def _side(steps: list[Any], source: str, path: Any) -> tuple[int | None, str]:
    if not isinstance(path, str) or not path:
        return None, NO_PATH
    step, reason = pick_source(steps, source)
    if step is None:
        return None, reason
    return read_count(
        step.output, path, has_preimage=bool(getattr(step, "has_preimage", True))
    )


def _ascii(value: Any) -> str:
    return "".join(ch if " " <= ch <= "~" else "?" for ch in str(value))


def _shown(count: int | None, reason: str) -> str:
    return str(count) if count is not None else f"unreadable({reason})"


def evaluate_counts_match(steps: list[Any], operands: Any) -> dict[str, Any]:
    """The signed ``counts_match`` verdict for a run. Never raises."""
    try:
        if not isinstance(operands, dict) or not (
            isinstance(operands.get("left"), str)
            and isinstance(operands.get("right"), str)
        ):
            return {"check": CHECK, "result": "fail", "detail": _NO_OPERANDS}
        left, right = operands["left"], operands["right"]
        left_path = operands.get("left_path")
        right_path = operands.get("right_path")
        key = operands.get("key")
        lc, lr = _side(steps, left, left_path)
        rc, rr = _side(steps, right, right_path)
        parts = [f"left={_shown(lc, lr)}", f"right={_shown(rc, rr)}"]
        if isinstance(key, str) and key:
            parts.append(f"key={key}")
        parts.append(f"left_source={left}")
        if isinstance(left_path, str) and left_path:
            parts.append(f"left_path={left_path}")
        parts.append(f"right_source={right}")
        if isinstance(right_path, str) and right_path:
            parts.append(f"right_path={right_path}")
        passed = lc is not None and rc is not None and lc == rc
        return {
            "check": CHECK,
            "result": "pass" if passed else "fail",
            "detail": _ascii(" ".join(parts)),
        }
    except Exception:
        return {"check": CHECK, "result": "fail", "detail": _NOT_EVALUATED}
