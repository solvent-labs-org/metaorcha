"""Minimal registry client — register an agent's emerge.yaml.

Uses only the standard library (urllib) so the SDK stays dependency-light.
Registration mirrors ``POST /api/v1/agents/register`` (multipart upload of the
``emerge.yaml`` file, optional ``Authorization: Bearer <PAT>``).
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
import uuid

logger = logging.getLogger("emerge.client")

DEFAULT_REGISTRY_URL = "http://localhost:8000"
_REGISTER_PATH = "/api/v1/agents/register"


class RegistryError(RuntimeError):
    """Raised when registration fails."""


def _multipart_body(yaml_text: str, *, field: str = "emerge_yaml") -> tuple[bytes, str]:
    boundary = f"----emerge{uuid.uuid4().hex}"
    parts = [
        f"--{boundary}",
        f'Content-Disposition: form-data; name="{field}"; filename="emerge.yaml"',
        "Content-Type: application/x-yaml",
        "",
        yaml_text,
        f"--{boundary}--",
        "",
    ]
    body = "\r\n".join(parts).encode("utf-8")
    return body, boundary


def register(
    yaml_text: str,
    *,
    registry_url: str = DEFAULT_REGISTRY_URL,
    token: str | None = None,
    timeout: float = 30.0,
) -> dict:
    """Register an agent manifest against a registry. Returns the JSON response.

    Raises :class:`RegistryError` on non-2xx responses or transport errors.
    """
    url = registry_url.rstrip("/") + _REGISTER_PATH
    if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
        raise RegistryError(f"The registry URL must be http or https: {registry_url}")
    body, boundary = _multipart_body(yaml_text)
    req = urllib.request.Request(url, data=body, method="POST")  # noqa: S310 - http(s) only
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - http(s) only
            payload = resp.read().decode("utf-8")
            return json.loads(payload) if payload else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RegistryError(
            f"Registry returned {exc.code} for {url}: {detail}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RegistryError(
            f"Could not reach registry at {registry_url} — is the local stack up? "
            f"({exc.reason})"
        ) from exc
