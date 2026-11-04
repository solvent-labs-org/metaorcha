"""Connections: the MCP servers a user has added, keyed by DID.

A connection's token lives in its own 0600 file under ``<home>/secrets``.
It never goes into ``connections.json``, an engine's environment or
arguments, a prompt, a journal or a receipt. Revoking deletes the token, so
a revoked connection cannot be used again without being added again.
"""

from __future__ import annotations

import builtins
import contextlib
import json
import re
import secrets
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from emerge.journal import utc_now

from metabot.origins import origin_pattern
from metabot.paths import metabot_home, private_dir, write_private
from metabot.scope_classes import ScopeClass

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
CONN_ID_RE = re.compile(r"did:orcha:agent:conn-[0-9a-f]{12}")
ENV_RE = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")
HEADER_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
RESERVED = frozenset({"skills"})  # the switchboard's own tools are skills__*


@dataclass
class Connection:
    id: str
    name: str
    transport: str
    command: list[str] | None = None
    url: str | None = None
    token_env: str | None = None
    header: str | None = None
    state: str = "active"
    overrides: dict[str, str] = field(default_factory=dict)
    added_at: str = ""
    origins: list[str] | None = None  # a browser: where it may act; None is not a browser

    @property
    def suffix(self) -> str:
        return self.id.rsplit("-", 1)[-1]

    @property
    def active(self) -> bool:
        return self.state == "active"

    @property
    def redacted_as(self) -> str:
        """The name a redacted token shows as: ``[REDACTED:<redacted_as>]``."""
        return self.token_env or f"{self.name}-token"


def new_connection_id() -> str:
    """A fresh connection id, as :meth:`ConnectionStore.add` gives one."""
    return f"did:orcha:agent:conn-{secrets.token_hex(6)}"


def _validate(transport: str, command, url, token, token_env, header) -> None:
    if transport == "stdio":
        if not command or not all(isinstance(c, str) and c for c in command):
            raise ValueError("a stdio connection needs a command")
        if url or header:
            raise ValueError("a stdio connection takes no url or header")
        if token and not token_env:
            raise ValueError("a stdio connection's token needs --token-env NAME")
        if token_env and not ENV_RE.match(token_env):
            raise ValueError("--token-env must look like AN_ENV_VAR")
    elif transport == "http":
        if command or token_env:
            raise ValueError("an http connection takes no command or token-env")
        parsed = urlparse(url or "")
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("an http connection needs an http(s) url")
        if parsed.scheme == "http" and parsed.hostname not in LOCAL_HOSTS:
            raise ValueError("use https for a remote server (plain http only on this machine)")
        if header and not HEADER_RE.match(header):
            raise ValueError("--header must be a header name")
    else:
        raise ValueError("transport must be stdio or http")


def _exact_origins(origins: Any) -> list[str] | None:
    """A browser connection's origins made exact, each once; ValueError on one that is not."""
    if origins is None:
        return None
    out: list[str] = []
    for given in origins:
        pattern = origin_pattern(str(given))
        if pattern not in out:
            out.append(pattern)
    return out


def _stored_origins(raw: Any) -> list[str] | None:
    """Origins as stored; a hand-edited value that does not read allows nowhere."""
    if raw is None:
        return None
    if isinstance(raw, list) and all(isinstance(o, str) for o in raw):
        return raw
    return []


def check_spec(transport: str, command: list[str] | None = None, url: str | None = None) -> None:
    """ValueError when ``ConnectionStore.add`` would refuse this server, tokens aside."""
    _validate(transport, command, url, None, None, None)


class ConnectionFileError(ValueError):
    """``connections.json`` does not read. Every run reads it, so the run stops and names it."""


def _whole(conn: Connection) -> bool:
    """Whether a record's fields have the types MetaBot reads them as."""
    named = (conn.id, conn.name, conn.transport, conn.state, conn.added_at)
    optional = (conn.url, conn.token_env, conn.header)
    command = conn.command if conn.command is not None else []
    return (
        all(isinstance(v, str) for v in named)
        and all(v is None or isinstance(v, str) for v in optional)
        and isinstance(command, list)
        and all(isinstance(part, str) for part in command)
        and isinstance(conn.overrides, dict)
    )


class ConnectionStore:
    def __init__(self, home: Path | None = None) -> None:
        self.home = home or metabot_home()

    @property
    def path(self) -> Path:
        return self.home / "connections.json"

    def _secrets(self) -> Path:
        return private_dir(self.home / "secrets")

    def _load(self) -> list[Connection]:
        """Every connection; ConnectionFileError naming the file when it does not read."""
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError) as exc:
            raise ConnectionFileError(f"cannot read {self.path}: {exc}") from exc
        rows = data.get("connections", []) if isinstance(data, dict) else None
        if not isinstance(rows, list) or not all(isinstance(raw, dict) for raw in rows):
            raise ConnectionFileError(f"cannot read {self.path}: it holds no list of connections")
        known = {f.name for f in fields(Connection)}
        try:
            conns = [Connection(**{k: v for k, v in raw.items() if k in known}) for raw in rows]
        except TypeError:
            conns = []
        if len(conns) != len(rows) or not all(map(_whole, conns)):
            raise ConnectionFileError(f"cannot read {self.path}: a connection is not whole")
        for conn in conns:
            conn.origins = _stored_origins(conn.origins)
        return conns

    def _save(self, conns: list[Connection]) -> None:
        doc: dict[str, Any] = {"version": 1, "connections": [asdict(c) for c in conns]}
        write_private(self.path, json.dumps(doc, indent=2) + "\n")

    def list(self) -> list[Connection]:
        return self._load()

    def active(self) -> builtins.list[Connection]:
        return [c for c in self._load() if c.active]

    def get(self, ref: str) -> Connection:
        for conn in self._load():
            if ref in (conn.id, conn.name):
                return conn
        raise KeyError(f"no connection named {ref!r}")

    def add(
        self,
        *,
        name: str,
        transport: str,
        command: builtins.list[str] | None = None,
        url: str | None = None,
        token: str | None = None,
        token_env: str | None = None,
        header: str | None = None,
        origins: Any = None,
        conn_id: str | None = None,
    ) -> Connection:
        """Add a connection; ``origins`` makes it a browser that may act only there.

        ``conn_id`` is one :func:`new_connection_id` made: a plugin lists it first.
        """
        if not NAME_RE.match(name or ""):
            raise ValueError("a name is 1-32 of a-z, 0-9 and '-', starting with a letter or digit")
        if name in RESERVED:
            raise ValueError(f"{name} is kept for MetaBot's own tools; choose another name")
        _validate(transport, command, url, token, token_env, header)
        exact = _exact_origins(origins)
        conns = self._load()
        if any(c.name == name for c in conns):
            raise ValueError(f"a connection named {name!r} already exists")
        if conn_id is not None and (
            not CONN_ID_RE.fullmatch(conn_id) or any(c.id == conn_id for c in conns)
        ):
            raise ValueError(f"{conn_id} is not a connection id this store can take")
        conn = Connection(
            id=conn_id or new_connection_id(),
            name=name,
            transport=transport,
            command=list(command) if command else None,
            url=url,
            token_env=token_env,
            header=header,
            added_at=utc_now(),
            origins=exact,
        )
        if token:
            write_private(self._secrets() / conn.suffix, token)
        self._save([*conns, conn])
        return conn

    def _update(self, ref: str, change) -> Connection:
        conns = self._load()
        target = self.get(ref)
        for i, conn in enumerate(conns):
            if conn.id == target.id:
                change(conn)
                conns[i] = conn
                self._save(conns)
                return conn
        raise KeyError(ref)

    def respec(
        self,
        ref: str,
        *,
        transport: str,
        command: builtins.list[str] | None = None,
        url: str | None = None,
    ) -> Connection:
        """Write what a connection starts or calls again. Its id, name, scope settings, state
        and token stay, so a routine that names it still does."""

        def _respec(conn: Connection) -> None:
            _validate(transport, command, url, None, conn.token_env, conn.header)
            conn.transport = transport
            conn.command = list(command) if command else None
            conn.url = url

        return self._update(ref, _respec)

    def revoke(self, ref: str) -> Connection:
        def _revoke(conn: Connection) -> None:
            conn.state = "revoked"
            with contextlib.suppress(FileNotFoundError):
                (self._secrets() / conn.suffix).unlink()

        return self._update(ref, _revoke)

    def remove(self, ref: str) -> Connection:
        """Delete a connection and its token; KeyError when there is none."""
        target = self.get(ref)
        with contextlib.suppress(FileNotFoundError):
            (self._secrets() / target.suffix).unlink()
        self._save([c for c in self._load() if c.id != target.id])
        return target

    def set_override(self, ref: str, capability: str, cls: str) -> Connection:
        parsed = ScopeClass.parse(cls)
        if parsed is None:
            raise ValueError("a class is read, write or destructive")
        return self._update(ref, lambda c: c.overrides.__setitem__(capability, parsed.label))

    def redaction_map(self) -> dict[str, str]:
        """``{VAR: token}`` for every active connection, for redacting a run's record."""
        out: dict[str, str] = {}
        for conn in self.active():
            token = self.secret(conn)
            if token:
                out[conn.redacted_as] = token
        return out

    def secret(self, conn: Connection) -> str | None:
        if not conn.active:
            return None
        try:
            return (self._secrets() / conn.suffix).read_text(encoding="utf-8") or None
        except FileNotFoundError:
            return None


def merged_secrets(*maps: Mapping[str, str]) -> dict[str, str]:
    """One ``{VAR: secret}`` map from several, never dropping a secret.

    Two different secrets under one name are both kept, the later as ``VAR_2``, ``VAR_3``.
    """
    out: dict[str, str] = {}
    for secrets_of in maps:
        for var, secret in secrets_of.items():
            name, n = var, 2
            while name in out and out[name] != secret:
                name, n = f"{var}_{n}", n + 1
            out[name] = secret
    return out


def record_secrets(store: ConnectionStore | None = None) -> dict[str, str]:
    """Every secret a record must not keep: each connection's token in ``store``, by default
    the person's, and each key the settings name for a model, whichever one a run called.

    Settings that do not read give no keys here: the command that needs them stops on them.
    """
    from metabot.config import ConfigError, configured_keys, load_config

    try:
        keys = configured_keys(load_config())
    except ConfigError:
        keys = {}
    return merged_secrets((store or ConnectionStore()).redaction_map(), keys)
