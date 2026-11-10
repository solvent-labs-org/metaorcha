"""history.py — MetaChat's append-only event log (contract section 9).

One SQLite file, ``history.sqlite`` in the state dir. The only statements in
this module are CREATE TABLE IF NOT EXISTS, INSERT and SELECT: nothing here
can change or remove a row once it is written. The log is the hook a later
sync plugin reads from.

Kinds: spawn, run, kill, verify, account, cli_read, cli_desk, modules_reload.
``data`` is a JSON object of at most 8 KB; stdout/stderr inside it are cut to
2 KB each before it is stored.
"""
import json
import sqlite3
import threading
import time

KINDS = ("spawn", "run", "kill", "verify", "account", "cli_read", "cli_desk",
         "modules_reload", "device_paired", "device_revoked", "folder_added", "folder_removed")
DATA_LIMIT = 8 * 1024
TEXT_LIMIT = 2 * 1024
SCHEMA = ("CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, ts REAL NOT NULL, "
          "kind TEXT NOT NULL, desk TEXT, run_id TEXT, data TEXT NOT NULL)")


def _trim(data):
    """Cut long captured text, then the whole object, to the stored limits."""
    out = {}
    for k, v in (data or {}).items():
        if isinstance(v, str) and k in ("stdout", "stderr") and len(v) > TEXT_LIMIT:
            v = v[:TEXT_LIMIT]
        out[k] = v
    text = json.dumps(out, default=str)
    if len(text.encode("utf-8")) > DATA_LIMIT:
        text = json.dumps({"truncated": True, "keys": sorted(out)})
    return text


class Store:
    """Append events and read the newest ones. Safe to share between threads."""

    def __init__(self, path):
        self.path = str(path)
        self._lock = threading.Lock()
        with self._connect() as c:
            c.execute(SCHEMA)

    def _connect(self):
        c = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        return c

    def append(self, kind, desk=None, run_id=None, data=None):
        if kind not in KINDS:
            raise ValueError(f"unknown history kind: {kind!r}")
        row = (time.time(), kind, desk, run_id, _trim(data))
        with self._lock, self._connect() as c:
            cur = c.execute("INSERT INTO events(ts, kind, desk, run_id, data) VALUES (?,?,?,?,?)", row)
            return cur.lastrowid

    def recent(self, limit=100):
        limit = max(1, min(int(limit), 1000))
        with self._lock, self._connect() as c:
            rows = c.execute("SELECT id, ts, kind, desk, run_id, data FROM events "
                             "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for rid, ts, kind, desk, run_id, data in rows:
            try:
                parsed = json.loads(data)
            except ValueError:
                parsed = {"raw": data}
            out.append({"id": rid, "ts": ts, "kind": kind, "desk": desk,
                        "run_id": run_id, "data": parsed})
        return out
