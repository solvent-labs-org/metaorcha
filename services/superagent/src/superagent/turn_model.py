"""Which model ran a turn, as one signed-verdict detail (FR-13, AD-21, story 3.3).

The orchestrator calls :func:`turn_model` after each LLM call with the client
it built and the message the provider streamed back. The result,
``"<route>/<model id>"``, is stamped on every step of the turn and signed once
into the receipt as ``{check: "model", result: "pass", detail}``.

- The **model id** is the one the provider says it served
  (``response_metadata["model_name"]``) — an OpenRouter alias resolves to the
  model that ran — falling back to the id the client was built with.
- The **route** is where the request went, read from the client and never
  from the response: the endpoint's host name (``openrouter.ai``, the native
  Gemini client's API host, a BYOK host), or ``local`` for a loopback,
  private-network or link-local address or one of the conventional local
  host names. ``local`` describes the first hop only — a proxy there may
  forward anywhere — and is never inferred from a port.

Pure and total: a value that cannot be read gives None, and the detail is
printable ASCII (the envelope's verdict rule) so it can never stop a receipt
from sealing.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Any
from urllib.parse import urlparse

MODEL_CHECK = "model"
# Where the model is kept on the turn's AIMessage (``response_metadata``),
# which the transcript writes to the assistant row; never sent to a provider.
TURN_MODEL_KEY = "orcha_turn_model"
_MAX_LEN = 200
_NOT_PRINTABLE = re.compile(r"[^\x20-\x7e]")
_LOCAL_HOSTS = frozenset({"localhost", "ollama", "host.docker.internal"})
# The native Gemini client carries no base URL; this is the API host it
# sends to (a host name is data, like the OpenRouter URL in config).
_GEMINI_HOST = "generativelanguage.googleapis.com"


def route(chat: Any) -> str:
    """Where *chat* sends its requests: ``local`` | ``<host>`` | ``unknown``."""
    if type(chat).__name__ == "ChatGoogleGenerativeAI":
        return _GEMINI_HOST
    base = getattr(chat, "openai_api_base", None)
    try:
        host = (urlparse(base if isinstance(base, str) else "").hostname or "").lower()
    except ValueError:
        return "unknown"
    if not host:
        return "unknown"
    if host in _LOCAL_HOSTS or _private_ip(host):
        return "local"
    return host


def _private_ip(host: str) -> bool:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_private or address.is_link_local


def _served(accumulated: Any) -> str | None:
    metadata = getattr(accumulated, "response_metadata", None)
    if not isinstance(metadata, dict):
        return None
    for key in ("model_name", "model"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _configured(chat: Any) -> str | None:
    for attr in ("model_name", "model"):
        value = getattr(chat, attr, None)
        if isinstance(value, str) and value.strip():
            return value
    return None


def clean(detail: Any) -> str | None:
    """A verdict-safe detail: printable ASCII, trimmed, capped; None if empty."""
    if not isinstance(detail, str):
        return None
    text = _NOT_PRINTABLE.sub("", detail).strip()[:_MAX_LEN].strip()
    return text or None


def turn_model(chat: Any, accumulated: Any = None) -> str | None:
    """``"<route>/<model id>"`` for one LLM call, or None. Never raises."""
    try:
        model = clean(_served(accumulated) or _configured(chat))
        if not model:
            return None
        return clean(f"{route(chat)}/{model}")
    except Exception:
        return None
