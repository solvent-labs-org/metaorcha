"""Durable Google OAuth token store keyed by opaque session/user key.

SQLite-backed (stdlib) so tokens survive agent restarts — the previous
in-memory dict silently logged users out whenever the process restarted
mid-session. DB path comes from ``GWS_TOKEN_DB`` (default ``data/tokens.db``;
the sandbox compose mounts a volume at ``/app/data``).

Expiry uses wall-clock ``time.time()`` — the old ``time.monotonic()`` stamps
were meaningless across restarts, which is exactly when durability matters.
"""

from __future__ import annotations

import logging
import os
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_DB_PATH = os.getenv("GWS_TOKEN_DB", "data/tokens.db")

_lock = threading.Lock()


# Aliases handed out by log_ref(), one per credential, for this process only.
_log_refs: dict[str, str] = {}
_log_refs_lock = threading.Lock()
_LOG_REFS_MAX = 4096


def log_ref(secret: str) -> str:
    """What logs show in place of a credential: a random 12-hex-digit alias.

    The alias is the same for the same value for the life of the process, so
    log lines still correlate, but nothing in it is derived from the value: a
    log line cannot be checked against a guess. A token key counts: it
    addresses the OAuth tokens stored for a session.
    """
    with _log_refs_lock:
        ref = _log_refs.get(secret)
        if ref is None:
            if len(_log_refs) >= _LOG_REFS_MAX:
                _log_refs.clear()  # bounded; earlier aliases simply stop matching
            ref = _log_refs[secret] = secrets.token_hex(6)
        return ref


@dataclass
class GoogleTokens:
    access_token: str
    refresh_token: str | None = None
    expires_at: float | None = None  # wall-clock epoch seconds

    def is_expired(self, buffer_seconds: float = 120) -> bool:
        if self.expires_at is None:
            return False
        return time.time() >= self.expires_at - buffer_seconds


def _connect() -> sqlite3.Connection:
    Path(_DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH, timeout=10)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS google_tokens ("
        "  token_key TEXT PRIMARY KEY,"
        "  access_token TEXT NOT NULL,"
        "  refresh_token TEXT,"
        "  expires_at REAL"
        ")"
    )
    return conn


def put_tokens(
    key: str, access_token: str, refresh_token: str | None, expires_in: int | None
) -> None:
    exp: float | None = None
    if expires_in is not None:
        exp = time.time() + float(expires_in)
    with _lock, _connect() as conn:
        # Keep an existing refresh_token when Google omits it on re-consent
        # (it only issues one on the first authorization by default).
        if refresh_token is None:
            row = conn.execute(
                "SELECT refresh_token FROM google_tokens WHERE token_key = ?", (key,)
            ).fetchone()
            if row and row[0]:
                refresh_token = row[0]
        conn.execute(
            "INSERT INTO google_tokens (token_key, access_token, refresh_token, expires_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(token_key) DO UPDATE SET "
            "  access_token = excluded.access_token,"
            "  refresh_token = excluded.refresh_token,"
            "  expires_at = excluded.expires_at",
            (key, access_token, refresh_token, exp),
        )
    logger.info(
        "oauth_tokens_put key=%s has_refresh=%s expires_in=%s db=%s",
        log_ref(key),
        bool(refresh_token),
        expires_in,
        _DB_PATH,
    )


def get_tokens(key: str) -> GoogleTokens | None:
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT access_token, refresh_token, expires_at FROM google_tokens WHERE token_key = ?",
            (key,),
        ).fetchone()
    tok = (
        GoogleTokens(access_token=row[0], refresh_token=row[1], expires_at=row[2])
        if row
        else None
    )
    logger.info("oauth_tokens_get key=%s hit=%s", log_ref(key), tok is not None)
    return tok


def clear_tokens(key: str) -> None:
    with _lock, _connect() as conn:
        conn.execute("DELETE FROM google_tokens WHERE token_key = ?", (key,))


def snapshot_keys() -> list[str]:
    with _lock, _connect() as conn:
        rows = conn.execute("SELECT token_key FROM google_tokens").fetchall()
    return [r[0] for r in rows]
