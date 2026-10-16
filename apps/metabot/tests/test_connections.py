from __future__ import annotations

import json
import re
import sys

import pytest

from conftest import FAKE_MCP, private_mode
from metabot.cli import main
from metabot.connections import ConnectionFileError, ConnectionStore
from metabot.paths import metabot_home, secrets_dir


def _stdio(store, name="gh", token="tok-123"):
    return store.add(
        name=name,
        transport="stdio",
        command=[sys.executable, str(FAKE_MCP)],
        token=token,
        token_env="FAKE_TOKEN",
    )


def test_add_never_writes_token_to_json():
    store = ConnectionStore()
    conn = _stdio(store)
    raw = (metabot_home() / "connections.json").read_text()
    assert "tok-123" not in raw
    assert conn.id.startswith("did:orcha:agent:conn-")
    assert len(conn.id.rsplit("-", 1)[-1]) == 12
    assert json.loads(raw)["connections"][0]["name"] == "gh"
    assert store.secret(conn) == "tok-123"


def test_secret_mode_0600():
    conn = _stdio(ConnectionStore())
    assert private_mode(secrets_dir()) == 0o700
    assert private_mode(secrets_dir() / conn.suffix) == 0o600
    assert private_mode(metabot_home() / "connections.json") == 0o600


def test_names_unique_and_validated():
    store = ConnectionStore()
    _stdio(store)
    with pytest.raises(ValueError, match="already"):
        _stdio(store)
    for bad in ["", "GH", "a b", "-x", "x" * 33, "a/b"]:
        with pytest.raises(ValueError):
            _stdio(store, name=bad)


def test_get_by_name_or_id():
    store = ConnectionStore()
    conn = _stdio(store)
    assert store.get("gh").id == conn.id
    assert store.get(conn.id).name == "gh"
    with pytest.raises(KeyError):
        store.get("nope")


def test_revoke_persists_and_drops_the_secret():
    store = ConnectionStore()
    conn = _stdio(store)
    store.revoke("gh")
    again = ConnectionStore().get(conn.id)
    assert again.state == "revoked"
    assert ConnectionStore().secret(again) is None
    assert not (secrets_dir() / conn.suffix).exists()
    assert ConnectionStore().active() == []


def test_http_requires_https_unless_local():
    store = ConnectionStore()
    store.add(name="local", transport="http", url="http://127.0.0.1:9/mcp", token="t")
    store.add(name="remote", transport="http", url="https://mcp.example.com/mcp", token="t")
    with pytest.raises(ValueError, match="https"):
        store.add(name="plain", transport="http", url="http://mcp.example.com/mcp", token="t")


def test_stdio_token_needs_an_env_name():
    store = ConnectionStore()
    with pytest.raises(ValueError, match="token-env"):
        store.add(name="x", transport="stdio", command=["srv"], token="t")
    with pytest.raises(ValueError):
        store.add(name="y", transport="stdio", command=["srv"], token="t", token_env="lower")


def test_override_tightens_only_with_a_known_class():
    store = ConnectionStore()
    _stdio(store)
    store.set_override("gh", "create_issue", "destructive")
    assert ConnectionStore().get("gh").overrides == {"create_issue": "destructive"}
    with pytest.raises(ValueError):
        store.set_override("gh", "create_issue", "harmless")


def test_remove_deletes_the_connection_and_its_token():
    store = ConnectionStore()
    conn = _stdio(store)
    token_file = secrets_dir() / conn.suffix
    assert token_file.exists()
    assert store.remove("gh").id == conn.id
    assert store.list() == [] and not token_file.exists()
    with pytest.raises(KeyError):
        store.remove("gh")


# ── a connections file that does not read ───────────────────────────────────

BROKEN = [
    b"{not json",
    b"\xff\xfe",
    b"[1, 2]",
    b'{"connections": 1}',
    b'{"connections": [1]}',
    b'{"connections": [{"name": "x"}]}',
    b'{"connections": [{"id": 5, "name": "x", "transport": "stdio"}]}',
    b'{"connections": [{"id": "d-1", "name": "x", "transport": "stdio", "command": "ls"}]}',
]


@pytest.mark.parametrize("raw", BROKEN)
def test_a_connections_file_that_does_not_read_is_named(raw):
    store = ConnectionStore()
    store.path.write_bytes(raw)
    for read in (store.list, store.active, store.redaction_map):
        with pytest.raises(ConnectionFileError, match=f"^cannot read {re.escape(str(store.path))}"):
            read()
    with pytest.raises(ConnectionFileError):
        _stdio(store, name="new")
    assert store.path.read_bytes() == raw  # never written over


def test_a_connections_path_that_is_a_folder_is_named():
    store = ConnectionStore()
    store.path.mkdir()
    with pytest.raises(ConnectionFileError, match="^cannot read "):
        store.list()


def test_every_command_says_the_connections_file_does_not_read(home, capsys, monkeypatch):
    path = ConnectionStore().path
    path.write_text("{not json", encoding="utf-8")
    replies = iter(["hello"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(replies))
    for argv in (
        ["ask", "-e", "openai", "-q", "hi"],
        ["chat", "-e", "openai"],
        ["connect", "list"],
        ["connect", "revoke", "gh"],
    ):
        code = main(argv)
        _, err = capsys.readouterr()
        assert code == 1, argv
        assert err.startswith(f"metabot: cannot read {path}"), (argv, err)
