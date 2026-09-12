"""Import shim for the monorepo's common-kafka workspace package.

``common/kafka`` is packaged as ``packages = ["src"]`` (dist name
``common-kafka``), so its editable install only puts ``common/kafka`` on
``sys.path`` and the modules are not importable under the canonical
``common_kafka`` name that superagent's step_events.py uses. The
playground consumes the same public API, so this shim loads the real
package by file location and replaces this module's ``sys.modules``
entry with it.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_REAL_SRC = Path(__file__).resolve().parents[4] / "common" / "kafka" / "src"

_spec = importlib.util.spec_from_file_location(
    __name__,
    _REAL_SRC / "__init__.py",
    submodule_search_locations=[str(_REAL_SRC)],
)
if _spec is None or _spec.loader is None:  # pragma: no cover
    raise ImportError(f"cannot locate common/kafka package at {_REAL_SRC}")
_module = importlib.util.module_from_spec(_spec)
sys.modules[__name__] = _module
_spec.loader.exec_module(_module)
