"""C3: one token per paired device.

The desktop token (``STATE/token``) stays what it is and opens the loopback
listener only. Every paired phone gets a token of its own, kept here as a
sha256 hash in ``STATE/devices.json`` (0600) with a name, a scope and times.
``match`` is scope-bound: a phone token never opens the desktop scope, and the
other way round. Revoking removes the row; the server closes that device's
live sockets (C5).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path

SCOPES = ("desktop", "phone")
NAME_MAX = 40
TOKEN_BYTES = 24     # 192 bits, url-safe
SEEN_EVERY = 60.0    # "seen" is rewritten at most this often per device


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def clean_name(raw) -> str:
    text = re.sub(r"\s+", " ", str(raw or "")).strip()[:NAME_MAX].strip()
    return text or "device"


class Devices:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._rows: list[dict] = []
        self._load()

    # ---- storage
    def _load(self) -> None:
        try:
            raw = self.path.read_text()
        except FileNotFoundError:
            return
        except OSError:
            self._rows = []
            return
        try:
            data = json.loads(raw)
            rows = data["devices"] if isinstance(data, dict) else None
            assert isinstance(rows, list)
            self._rows = [r for r in rows if isinstance(r, dict) and isinstance(r.get("hash"), str)
                          and r.get("scope") in SCOPES]
        except (ValueError, KeyError, AssertionError):
            try:
                os.replace(self.path, self.path.with_name(self.path.name + ".bad"))
            except OSError:
                pass
            self._rows = []

    def _save(self) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump({"v": 1, "devices": self._rows}, f, indent=1, sort_keys=True)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    @staticmethod
    def _public(row: dict) -> dict:
        return {"id": row["id"], "name": row["name"], "scope": row["scope"],
                "created": row["created"], "seen": row.get("seen")}

    # ---- api
    def add(self, name, scope: str) -> tuple[str, dict]:
        """A new device: (plain token, public row). The token is shown once and never stored."""
        if scope not in SCOPES:
            raise ValueError(f"scope {scope!r}")
        token = secrets.token_urlsafe(TOKEN_BYTES)
        with self._lock:
            ids = {r["id"] for r in self._rows}
            dev_id = secrets.token_hex(4)
            while dev_id in ids:
                dev_id = secrets.token_hex(4)
            row = {"id": dev_id, "name": clean_name(name), "scope": scope, "hash": _hash(token),
                   "created": int(time.time()), "seen": None}
            self._rows.append(row)
            try:
                self._save()
            except OSError:
                self._rows.remove(row)
                raise
        return token, self._public(row)

    def match(self, token, scope: str) -> dict | None:
        """The public row a token opens in ``scope``, else None. Every row is compared."""
        if not isinstance(token, str) or not token or scope not in SCOPES:
            return None
        want = _hash(token)
        found = None
        with self._lock:
            for row in self._rows:
                ok = hmac.compare_digest(row["hash"], want)
                if ok and row["scope"] == scope and found is None:
                    found = row
            if found is None:
                return None
            now = time.time()
            if found.get("seen") is None or now - found["seen"] >= SEEN_EVERY:
                found["seen"] = int(now)
                try:
                    self._save()
                except OSError:
                    pass
            return self._public(found)

    def revoke(self, dev_id) -> bool:
        with self._lock:
            keep = [r for r in self._rows if r["id"] != dev_id]
            if len(keep) == len(self._rows):
                return False
            old, self._rows = self._rows, keep
            try:
                self._save()
            except OSError:
                self._rows = old
                raise
        return True

    def list(self) -> list[dict]:
        with self._lock:
            return [self._public(r) for r in self._rows]

    def count(self, scope: str | None = None) -> int:
        with self._lock:
            return sum(1 for r in self._rows if scope is None or r["scope"] == scope)
