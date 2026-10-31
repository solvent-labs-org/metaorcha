"""Plugins install pinned by tree hash; hooks and servers need the person's say."""

from __future__ import annotations

import errno
import json
import os
import re
import shutil
from pathlib import Path

import pytest

from conftest import FIXTURES
from metabot.cli import main
from metabot.connections import ConnectionStore
from metabot.plugins import (
    PluginError,
    add_marketplace,
    disable,
    enable,
    enabled_plugin_dirs,
    enabled_plugins,
    import_skills,
    inspect_plugin,
    install,
    installed,
    marketplaces,
    permission_diff,
    remove,
    resolve,
    tree_sha256,
)
from plugin_kit import make_market as _market
from plugin_kit import make_plugin as _plugin
from plugin_kit import write as _write
from plugin_kit import yes as _yes


def _installed_tools(tmp_path: Path, **kw):
    market = _market(tmp_path, "tools")
    _plugin(market / "plugins" / "tools", **kw)
    add_marketplace("m", market)
    return install("tools@m", _yes), market / "plugins" / "tools"


def test_tree_hash_is_stable_and_follows_every_byte_and_mode(tmp_path):
    a = _plugin(tmp_path / "a", commands=("go",))
    b = tmp_path / "b"
    shutil.copytree(a, b)
    assert tree_sha256(a) == tree_sha256(b)
    (b / ".git").mkdir()
    (b / ".git" / "HEAD").write_text("ref\n", encoding="utf-8")
    assert tree_sha256(a) == tree_sha256(b)  # a checkout's .git is not the plugin
    (b / "commands" / "go.md").write_text("Do go!\n", encoding="utf-8")
    assert tree_sha256(a) != tree_sha256(b)
    c = tmp_path / "c"
    shutil.copytree(a, c)
    os.chmod(c / "commands" / "go.md", 0o700)  # runnable by its owner, and read by no one else
    assert tree_sha256(a) != tree_sha256(c)
    d = tmp_path / "d"
    shutil.copytree(a, d)
    (d / "empty").write_text("", encoding="utf-8")
    assert tree_sha256(a) != tree_sha256(d)


def test_a_link_or_special_file_in_a_plugin_is_refused(tmp_path):
    linked = _plugin(tmp_path / "linked")
    (linked / "skills" / "evil").symlink_to(tmp_path)
    with pytest.raises(PluginError, match="link"):
        tree_sha256(linked)
    piped = _plugin(tmp_path / "piped")
    os.mkfifo(piped / "pipe")
    with pytest.raises(PluginError, match="regular file"):
        tree_sha256(piped)


@pytest.mark.parametrize("where", ["file", "folder"])
def test_a_plugin_file_whose_name_is_not_utf_8_is_refused_by_name(tmp_path, capsys, where):
    market = _market(tmp_path, "tools")
    plugin = _plugin(market / "plugins" / "tools")
    odd = os.fsdecode(b"notes-caf\xe9")  # a name made of bytes, as a Latin-1 disk holds it
    if where == "file":
        (plugin / odd).write_text("x\n", encoding="utf-8")
    else:
        (plugin / odd).mkdir()
        (plugin / odd / "notes.txt").write_text("x\n", encoding="utf-8")
    with pytest.raises(PluginError, match="not UTF-8"):
        tree_sha256(plugin)
    add_marketplace("m", market)
    code = main(["plugin", "install", "tools@m", "--yes"])  # before: a traceback
    out, err = capsys.readouterr()
    assert code == 1 and "notes-caf\\udce9" in err and "a file name that is not UTF-8" in err
    assert installed() == []


def test_facts_name_everything_a_plugin_carries(tmp_path):
    root = _plugin(
        tmp_path / "p",
        skills=("hello", "bye"),
        hooks={"PreToolUse": "sh ${CLAUDE_PLUGIN_ROOT}/x.sh"},
        servers={
            "db": {"command": "uvx", "args": ["db-server", "--ro"]},
            "web": {"url": "https://x.test/mcp"},
        },
        commands=("deploy",),
        routines=("nightly",),
    )
    facts = inspect_plugin(root)
    assert (facts.name, facts.version) == ("tools", "1.0.0")
    assert facts.skills == {"bye": "Says bye.", "hello": "Says hello."}
    assert facts.commands == ("deploy",)
    assert facts.hooks == {"PreToolUse": ("sh ${CLAUDE_PLUGIN_ROOT}/x.sh",)}
    assert facts.servers == {"db": "uvx db-server --ro", "web": "https://x.test/mcp"}
    assert facts.routines == ("nightly",)


def test_a_manifest_path_outside_the_plugin_is_refused(tmp_path):
    root = _plugin(tmp_path / "p")
    manifest = json.loads((root / ".claude-plugin" / "plugin.json").read_text())
    manifest["hooks"] = "../../etc/hooks.json"
    _write(root / ".claude-plugin" / "plugin.json", json.dumps(manifest))
    with pytest.raises(PluginError, match="outside"):
        inspect_plugin(root)


def test_install_copies_and_pins_by_tree_hash(tmp_path, home):
    plugin, source = _installed_tools(tmp_path)
    assert plugin.tree == tree_sha256(source)
    assert plugin.root == home / "plugins" / f"tools@{plugin.tree}"
    assert tree_sha256(plugin.root) == plugin.tree
    assert (plugin.name, plugin.market, plugin.version, plugin.enabled) == (
        "tools",
        "m",
        "1.0.0",
        False,
    )
    (source / "skills" / "hello" / "SKILL.md").write_text("changed\n", encoding="utf-8")
    assert tree_sha256(plugin.root) == plugin.tree  # the installed copy is its own
    assert json.loads((home / "plugins.json").read_text())[plugin.tree]["name"] == "tools"


def test_facts_are_shown_before_install_and_no_installs_nothing(tmp_path, home):
    market = _market(tmp_path, "tools")
    _plugin(market / "plugins" / "tools", hooks={"Stop": "true"}, servers={"db": {"command": "db"}})
    add_marketplace("m", market)
    seen = []

    def no(facts, diff) -> bool:
        seen.append((facts, diff))
        return False

    assert install("tools@m", no) is None
    facts, diff = seen[0]
    assert facts.skills and facts.hooks == {"Stop": ("true",)} and facts.servers == {"db": "db"}
    assert diff is None
    assert installed() == []
    assert not (home / "plugins").exists() or not any((home / "plugins").iterdir())


def test_an_update_shows_the_permission_diff(tmp_path):
    first, source = _installed_tools(tmp_path)
    enable("tools", without_hooks=True)
    _plugin(
        source,
        version="2.0.0",
        skills=("hello", "bye"),
        hooks={"PreToolUse": "sh x.sh"},
        servers={"db": {"command": "uvx", "args": ["db"]}},
    )
    diffs = []
    second = install("tools@m", lambda facts, diff: diffs.append(diff) or True)
    assert sorted(diffs[0]) == sorted(
        [
            "+ skill bye",
            "+ hook PreToolUse: sh x.sh",
            "+ server db: uvx db",
            "~ version 1.0.0 -> 2.0.0",
        ]
    )
    assert second.tree != first.tree and second.enabled is False
    assert resolve(first.tree).enabled is True  # the old one runs until the new one is enabled
    enable(second.tree[:12], allow_hooks=True)
    assert resolve(first.tree).enabled is False and resolve(second.tree).enabled is True


def test_permission_diff_names_removed_and_changed_things(tmp_path):
    old = inspect_plugin(
        _plugin(tmp_path / "a", skills=("a", "b"), servers={"db": {"command": "x"}})
    )
    new = inspect_plugin(_plugin(tmp_path / "b", skills=("a",), servers={"db": {"command": "y"}}))
    assert sorted(permission_diff(old, new)) == ["- skill b", "~ server db: x -> y"]


def test_hooks_need_the_persons_say(tmp_path):
    plugin, _ = _installed_tools(tmp_path, hooks={"PreToolUse": "sh ${CLAUDE_PLUGIN_ROOT}/x.sh"})
    with pytest.raises(PluginError) as caught:
        enable("tools")
    assert "sh ${CLAUDE_PLUGIN_ROOT}/x.sh" in str(caught.value)
    assert "--allow-hooks" in str(caught.value) and "--without-hooks" in str(caught.value)
    view = enable("tools", without_hooks=True).view
    assert not (view / "hooks").exists()
    assert "hooks" not in json.loads((view / ".claude-plugin" / "plugin.json").read_text())
    view = enable("tools", allow_hooks=True).view
    assert (view / "hooks" / "hooks.json").is_file()


def test_the_copy_engines_get_never_carries_servers(tmp_path):
    plugin, source = _installed_tools(tmp_path, servers={"db": {"command": "db"}})
    manifest = json.loads((source / ".claude-plugin" / "plugin.json").read_text())
    manifest["mcpServers"] = {"inline": {"command": "evil"}}
    _write(source / ".claude-plugin" / "plugin.json", json.dumps(manifest))
    plugin = install("tools@m", _yes)
    view = enable(plugin.tree).view
    assert not (view / ".mcp.json").exists()
    claude = json.loads((view / ".claude-plugin" / "plugin.json").read_text())
    cursor = json.loads((view / ".cursor-plugin" / "plugin.json").read_text())
    assert "mcpServers" not in claude and claude == cursor
    assert (view / "skills" / "hello" / "SKILL.md").is_file()
    assert enabled_plugin_dirs() == [view]


def test_servers_become_gated_connections_only_on_request(tmp_path):
    servers = {"db": {"command": "uvx", "args": ["db-server", "${CLAUDE_PLUGIN_ROOT}/conf.json"]}}
    plugin, _ = _installed_tools(tmp_path, servers=servers)
    enable("tools")
    assert ConnectionStore().list() == []
    enable("tools", add_servers=True)
    [conn] = ConnectionStore().active()
    assert conn.name == "tools-db"
    assert conn.command == ["uvx", "db-server", f"{plugin.root}/conf.json"]
    enable("tools", add_servers=True)  # again: no second connection
    assert len(ConnectionStore().list()) == 1
    disable("tools")
    assert ConnectionStore().list() == []
    assert enabled_plugin_dirs() == []


def test_enabling_again_keeps_the_connections_it_made(tmp_path):
    _installed_tools(tmp_path, servers={"db": {"command": "uvx", "args": ["db-server"]}})
    enable("tools", add_servers=True)
    [conn] = ConnectionStore().list()
    ConnectionStore().set_override(conn.id, "list_issues", "destructive")
    enable("tools", add_servers=True)
    [again] = ConnectionStore().list()
    # before: made anew, with a new id and no scope settings, so a routine naming it stopped
    # firing and a call the person had made ask ran unasked
    assert (again.id, again.overrides) == (conn.id, {"list_issues": "destructive"})
    ConnectionStore().revoke(conn.id)
    enable("tools", add_servers=True)
    assert [(c.id, c.state) for c in ConnectionStore().list()] == [(conn.id, "revoked")]


def test_enabling_another_version_keeps_the_connections_it_made(tmp_path):
    first, source = _installed_tools(tmp_path, servers={"db": {"command": "uvx", "args": ["db"]}})
    enable("tools", add_servers=True)
    [conn] = ConnectionStore().list()
    ConnectionStore().set_override(conn.id, "list_issues", "destructive")
    ConnectionStore().revoke(conn.id)
    _plugin(source, version="2.0.0", servers={"db": {"command": "uvx", "args": ["db@2"]}})
    second = install("tools@m", _yes)
    enable(second.tree[:12], add_servers=True)
    [again] = ConnectionStore().list()
    # before: it went with the version turned off and was made anew, active, with a new id and
    # no scope settings, so a routine naming it stopped firing and a call made to ask ran
    assert (again.id, again.state) == (conn.id, "revoked")
    assert (again.command, again.overrides) == (["uvx", "db@2"], {"list_issues": "destructive"})
    assert {p.tree: p.connections for p in installed()} == {first.tree: [], second.tree: [conn.id]}


def test_enabling_another_version_hands_no_connection_to_another_server_of_its_name(tmp_path):
    cut = "database-primary-connection-"  # tools-<server>, cut at 32, is one name for both
    _, source = _installed_tools(tmp_path, servers={f"{cut}a": {"command": "db-a"}})
    enable("tools", add_servers=True)
    [conn] = ConnectionStore().list()
    ConnectionStore().set_override(conn.id, "list_issues", "write")
    ConnectionStore().revoke(conn.id)
    _plugin(source, version="2.0.0", servers={f"{cut}b": {"command": "db-b"}})
    second = install("tools@m", _yes)
    enable(second.tree[:12], add_servers=True)
    [again] = ConnectionStore().list()
    # before: the other server took its id, its scope settings and its state, so a routine's
    # allow on the first server reached the second, and a revoke of it held the second off
    assert again.name == conn.name and again.id != conn.id
    assert (again.command, again.overrides, again.state) == (["db-b"], {}, "active")
    assert {p.tree: p.connections for p in installed()}[second.tree] == [again.id]


def test_enabling_another_version_drops_a_connection_whose_server_it_no_longer_has(tmp_path):
    _, source = _installed_tools(tmp_path, servers={"db": {"command": "db"}})
    enable("tools", add_servers=True)
    _plugin(source, version="2.0.0", servers={"web": {"command": "web"}})
    second = install("tools@m", _yes)
    enable(second.tree[:12], add_servers=True)
    assert [c.name for c in ConnectionStore().list()] == ["tools-web"]


def test_an_enable_that_cannot_read_the_connections_leaves_on_the_version_that_was(tmp_path, home):
    _, source = _installed_tools(tmp_path)
    enable("tools")
    first = [p.tree for p in installed() if p.enabled]
    _plugin(source, version="2.0.0")
    second = install("tools@m", _yes)
    (home / "connections.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="connections.json"):
        enable(second.tree[:12], add_servers=True)
    # before: the version that was on went off first, and no version was left on
    assert [p.tree for p in installed() if p.enabled] == first


@pytest.mark.parametrize("chose", ["allow_hooks", "without_hooks"])
def test_the_line_that_adds_a_connection_not_there_names_how_hooks_were_chosen(
    tmp_path, capsys, chose
):
    hooks = {"PreToolUse": "true"}
    plugin, _ = _installed_tools(tmp_path, servers={"db": {"command": "db"}}, hooks=hooks)
    enable("tools", add_servers=True, **{chose: True})
    [conn] = ConnectionStore().list()
    ConnectionStore().remove(conn.id)  # as an enable that stopped part way can leave it
    flag = "--" + chose.replace("_", "-")
    capsys.readouterr()
    assert main(["plugin", "enable", "tools", flag]) == 0
    remedy = f"metabot plugin enable {plugin.tree[:12]} --add-servers {flag}"
    # before: no hooks flag, so the line failed as it was printed
    assert f"connection {conn.id} is not there: add it with {remedy}\n" in capsys.readouterr().out
    assert main(remedy.split()[1:]) == 0
    assert [c.name for c in ConnectionStore().list()] == ["tools-db"]


@pytest.mark.parametrize("chose", ["allow_hooks", "without_hooks"])
def test_the_line_a_copy_outside_this_home_is_refused_with_makes_it_whole(tmp_path, home, chose):
    from metabot.upstream import misplaced_copy

    servers = {"db": {"command": "python3", "args": ["${CLAUDE_PLUGIN_ROOT}/server.py"]}}
    plugin, _ = _installed_tools(tmp_path, servers=servers, hooks={"PreToolUse": "true"})
    enable("tools", add_servers=True, **{chose: True})
    [conn] = ConnectionStore().list()
    ConnectionStore().set_override(conn.id, "list_issues", "destructive")
    doc = json.loads((home / "connections.json").read_text(encoding="utf-8"))
    whole = doc["connections"][0]["command"]
    # as 0.2.0 kept it, enabled under METABOT_HOME=rh
    doc["connections"][0]["command"] = [w.replace(str(home), "rh") for w in whole]
    (home / "connections.json").write_text(json.dumps(doc), encoding="utf-8")
    said = misplaced_copy(ConnectionStore().get(conn.id).command)
    flag = "--" + chose.replace("_", "-")
    remedy = f"metabot plugin enable {plugin.tree[:12]} --add-servers {flag}"
    assert said is not None and said.endswith(remedy)  # before: no hooks flag, so it failed
    assert main(remedy.split()[1:]) == 0
    again = ConnectionStore().get(conn.id)  # before: removed, and made anew with no settings
    assert (again.command, again.overrides) == (whole, {"list_issues": "destructive"})
    assert misplaced_copy(again.command) is None
    [held] = installed()
    assert held.hooks_allowed == (chose == "allow_hooks")


@pytest.mark.parametrize(
    "server",
    [
        {"command": "db", "env": {"DB_TOKEN": "${DB_TOKEN}"}},
        {"url": "https://x.test/mcp", "headers": {"Authorization": "Bearer ${T}"}},
        {"type": "sse", "url": "https://x.test/sse"},
    ],
)
def test_a_server_that_needs_a_secret_is_not_added_for_the_person(tmp_path, server):
    _installed_tools(tmp_path, servers={"db": server})
    with pytest.raises(PluginError, match="connect add"):
        enable("tools", add_servers=True)
    assert ConnectionStore().list() == []
    assert enabled_plugin_dirs() == []


@pytest.mark.parametrize(
    "args",
    [
        ["${CLAUDE_PLUGIN_ROOT}/server.py"],
        ["--root=${CLAUDE_PLUGIN_ROOT}"],
        ["-c", "exec python3 ${CLAUDE_PLUGIN_ROOT}/server.py"],
        ["-c", 'cd "${CLAUDE_PLUGIN_ROOT}" && exec ./run'],
        ["-c", "-", "exec python3 ${CLAUDE_PLUGIN_ROOT}/server.py"],
    ],
)
def test_a_server_enable_adds_names_its_copy_as_the_check_reads_it(tmp_path, args):
    from metabot.upstream import misplaced_copy

    _installed_tools(tmp_path, servers={"db": {"command": "sh", "args": args}})
    enable("tools", add_servers=True)
    [conn] = ConnectionStore().list()
    # before: after a space or a quote it was refused, and enabling again wrote it so
    assert misplaced_copy(conn.command) is None


@pytest.mark.parametrize(
    ("command", "args"),
    [
        ("cc", ["-I${CLAUDE_PLUGIN_ROOT}/include"]),
        ("python3", ["@${CLAUDE_PLUGIN_ROOT}/server.py"]),
        ("python3", ['"${CLAUDE_PLUGIN_ROOT}/server.py"']),
        ("python3 ${CLAUDE_PLUGIN_ROOT}/server.py", []),
        ("sh", ["-c", "exec $(pwd)/${CLAUDE_PLUGIN_ROOT}/server.sh"]),
    ],
)
def test_a_server_naming_its_copy_inside_a_word_is_not_added(tmp_path, command, args):
    _installed_tools(tmp_path, servers={"db": {"command": command, "args": args}})
    # before: added, and then never started, or started a file in its folder by that name
    with pytest.raises(PluginError, match="server db"):
        enable("tools", add_servers=True)
    assert ConnectionStore().list() == []
    assert enabled_plugin_dirs() == []


@pytest.mark.parametrize(
    ("folder", "args"),
    [
        ("x:y", ["PYTHONPATH=${CLAUDE_PLUGIN_ROOT}/lib", "srv"]),
        ("App Support", ["${CLAUDE_PLUGIN_ROOT}/server.sh"]),
        ("App Support", ["sh", "-c", "${CLAUDE_PLUGIN_ROOT}/server.sh"]),
    ],
)
def test_a_server_naming_its_copy_is_not_added_under_a_home_a_program_can_split(
    tmp_path, monkeypatch, folder, args
):
    monkeypatch.setenv("METABOT_HOME", str(tmp_path / folder / "mb"))
    _installed_tools(tmp_path, servers={"db": {"command": "env", "args": args}})
    # before: added, and the server's Python read its modules from the relative y/mb/..., or
    # env or sh ran the part of the home's path before its space
    with pytest.raises(PluginError, match="move the home") as refused:
        enable("tools", add_servers=True)
    assert "connect add" not in str(refused.value)
    assert ConnectionStore().list() == []


def test_a_server_under_such_a_home_that_names_its_copy_inside_a_word_is_told_both(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("METABOT_HOME", str(tmp_path / "App Support" / "mb"))
    servers = {"db": {"command": "cc", "args": ["-I${CLAUDE_PLUGIN_ROOT}/include"]}}
    _installed_tools(tmp_path, servers=servers)
    # before: told only to move the home, and once it moved the server was refused again
    with pytest.raises(PluginError, match="move the home .*, then add it yourself"):
        enable("tools", add_servers=True)
    assert ConnectionStore().list() == []


def test_a_changed_installed_copy_is_refused(tmp_path):
    plugin, _ = _installed_tools(tmp_path)
    (plugin.root / "skills" / "hello" / "SKILL.md").write_text("swapped\n", encoding="utf-8")
    with pytest.raises(PluginError, match="changed"):
        enable("tools")


def test_a_ref_by_name_must_be_unique(tmp_path):
    for market_name in ("m1", "m2"):
        market = _market(tmp_path, "tools", name=market_name)
        _plugin(market / "plugins" / "tools", version=market_name)
        add_marketplace(market_name, market)
        install(f"tools@{market_name}", _yes)
    with pytest.raises(PluginError, match="more than one"):
        resolve("tools")
    assert resolve("tools@m2").version == "m2"
    tree = resolve("tools@m1").tree
    assert resolve(tree[:10]).tree == tree
    with pytest.raises(PluginError):
        resolve(tree[:5])  # too short to be a hash prefix
    with pytest.raises(PluginError):
        resolve("ghost")


@pytest.mark.parametrize(
    ("source", "says"),
    [
        ("../outside", "outside"),
        ({"source": "url", "url": "https://example.invalid/x.git"}, "network"),
        ({"source": "git-subdir", "url": "https://example.invalid/x.git", "path": "p"}, "network"),
    ],
)
def test_a_source_outside_the_marketplace_or_remote_is_refused(tmp_path, source, says):
    market = tmp_path / "market"
    _write(
        market / ".claude-plugin" / "marketplace.json",
        json.dumps({"name": "m", "plugins": [{"name": "tools", "source": source}]}),
    )
    _plugin(tmp_path / "outside")
    add_marketplace("m", market)
    with pytest.raises(PluginError, match=says):
        install("tools@m", _yes)


def test_a_plugin_that_calls_itself_something_else_is_refused(tmp_path):
    market = _market(tmp_path, "tools")
    _plugin(market / "plugins" / "tools", name="other")
    add_marketplace("m", market)
    with pytest.raises(PluginError, match="other"):
        install("tools@m", _yes)


def test_a_marketplace_needs_its_file_and_a_safe_name(tmp_path):
    with pytest.raises(PluginError):
        add_marketplace("m", tmp_path)
    market = _market(tmp_path, "tools")
    for bad in ("", "Bad Name", "../x", "local"):
        with pytest.raises(PluginError):
            add_marketplace(bad, market)


def test_import_skills_makes_a_local_plugin(tmp_path, home):
    skills = tmp_path / "skills"
    for name in ("bmad-a", "bmad-b"):
        _write(skills / name / "SKILL.md", f"---\nname: {name}\ndescription: {name} skill\n---\n")
    _write(skills / "notes.txt", "not a skill\n")
    (skills / "empty").mkdir()
    plugin = import_skills(skills, "bmad", _yes)
    assert (plugin.name, plugin.market) == ("bmad", "local")
    assert inspect_plugin(plugin.root).skills == {
        "bmad-a": "bmad-a skill",
        "bmad-b": "bmad-b skill",
    }
    assert plugin.root.is_relative_to(home)
    assert enable("bmad@local").view.is_dir()


def test_remove_takes_the_plugin_and_its_connections(tmp_path, home):
    plugin, _ = _installed_tools(tmp_path, servers={"db": {"command": "db"}})
    view = enable("tools", add_servers=True).view
    remove("tools")
    assert installed() == [] and not plugin.root.exists() and not view.exists()
    assert ConnectionStore().list() == []


def _shut(folder: Path) -> None:
    """Take the way into a folder: it reads and writes, and cannot be entered."""
    assert folder.is_dir()
    folder.chmod(0o600)
    if os.access(folder, os.X_OK):
        folder.chmod(0o755)
        pytest.skip("this user enters any folder")


def test_remove_takes_a_copy_whose_folder_cannot_be_entered(tmp_path, home, capsys):
    plugin, _ = _installed_tools(tmp_path)
    view = enable("tools").view
    _shut(plugin.root / "skills" / "hello")
    with pytest.raises(PluginError, match="remove it and install it again"):
        enabled_plugin_dirs()
    assert main(["plugin", "remove", "tools@m", "--yes"]) == 0
    assert installed() == [] and not plugin.root.exists() and not view.exists()


def test_disable_takes_a_view_whose_folder_cannot_be_entered(tmp_path, home):
    _installed_tools(tmp_path)
    view = enable("tools").view
    _shut(view / "skills" / "hello")
    with pytest.raises(PluginError, match="disable it and enable it again"):
        enabled_plugin_dirs()
    assert main(["plugin", "disable", "tools@m"]) == 0
    assert not view.exists() and [p.enabled for p in installed()] == [False]
    assert enable("tools").view.is_dir()  # and it is enabled again, as the refusal says


def test_enable_again_over_a_view_whose_folder_cannot_be_entered(tmp_path, home):
    _installed_tools(tmp_path)
    view = enable("tools").view
    _shut(view / "skills" / "hello")
    assert enable("tools").view == view
    assert enabled_plugin_dirs() == [view]


def test_a_view_that_cannot_be_deleted_leaves_the_plugin_on_with_its_connections(
    tmp_path, home, monkeypatch, capsys
):
    _installed_tools(tmp_path, servers={"db": {"command": "db"}})
    view = enable("tools", add_servers=True).view

    def refuse(path, *args, **kw):
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr("metabot.plugins.shutil.rmtree", refuse)
    assert main(["plugin", "disable", "tools@m"]) != 0
    said = capsys.readouterr().err
    assert f"cannot delete {view}" in said and "Traceback" not in said
    assert [p.enabled for p in installed()] == [True] and view.is_dir()
    assert [c.name for c in ConnectionStore().list()] == ["tools-db"]


def test_a_disable_whose_connections_cannot_go_leaves_the_plugin_off_to_try_again(
    tmp_path, home, monkeypatch, capsys
):
    _installed_tools(tmp_path, servers={"db": {"command": "db"}})
    view = enable("tools", add_servers=True).view
    remove_one, full = ConnectionStore.remove, [True]

    def remove_on_a_full_disk(store, ref):
        if full:
            raise OSError(errno.ENOSPC, "No space left on device")
        return remove_one(store, ref)

    monkeypatch.setattr(ConnectionStore, "remove", remove_on_a_full_disk)
    assert main(["plugin", "disable", "tools@m"]) != 0
    said = capsys.readouterr().err
    assert "No space left on device" in said and "disable it again" in said
    [plugin] = installed()
    assert (plugin.enabled, plugin.bridges, view.exists()) == (False, {}, False)
    assert enabled_plugin_dirs() == [] and plugin.connections  # kept, to drop on the next try
    full.clear()
    assert main(["plugin", "disable", "tools@m"]) == 0
    assert installed()[0].connections == [] and ConnectionStore().list() == []


def _full_records(monkeypatch) -> list[bool]:
    """plugins.json cannot be written, while the list holds anything."""
    from metabot import plugins as plugins_module

    store, full = plugins_module._store_state, [True]

    def store_on_a_full_disk(name, data):
        if full and name == plugins_module.RECORDS:
            raise OSError(errno.ENOSPC, "No space left on device")
        return store(name, data)

    monkeypatch.setattr(plugins_module, "_store_state", store_on_a_full_disk)
    return full


def test_a_disable_whose_record_cannot_be_written_refuses_runs_until_it_is_done(
    tmp_path, home, monkeypatch, capsys
):
    _installed_tools(tmp_path)
    view = enable("tools").view
    full = _full_records(monkeypatch)
    assert main(["plugin", "disable", "tools@m"]) != 0
    said = capsys.readouterr().err
    assert "tools@m" in said and "No space left on device" in said and "disable it again" in said
    assert not view.exists() and [p.enabled for p in installed()] == [True]
    with pytest.raises(PluginError, match="disable it and enable it again"):
        enabled_plugin_dirs()  # never left out of a run without a word
    full.clear()
    assert main(["plugin", "disable", "tools@m"]) == 0
    assert enabled_plugin_dirs() == [] and [p.enabled for p in installed()] == [False]


def _connections_on_a_full_disk(monkeypatch) -> list[bool]:
    remove_one, full = ConnectionStore.remove, [True]

    def remove_on_a_full_disk(store, ref):
        if full:
            raise OSError(errno.ENOSPC, "No space left on device")
        return remove_one(store, ref)

    monkeypatch.setattr(ConnectionStore, "remove", remove_on_a_full_disk)
    return full


def test_a_remove_whose_connections_cannot_go_says_to_remove_it_again(
    tmp_path, home, monkeypatch, capsys
):
    first, _ = _installed_tools(tmp_path, servers={"db": {"command": "db"}})
    enable("tools", add_servers=True)
    full = _connections_on_a_full_disk(monkeypatch)
    assert main(["plugin", "remove", "tools@m", "--yes"]) != 0
    said = capsys.readouterr().err
    assert "no copy was removed" in said and "remove it again" in said
    assert "disable it again" not in said and first.root.is_dir()
    full.clear()
    assert main(["plugin", "remove", "tools@m", "--yes"]) == 0
    assert installed() == [] and ConnectionStore().list() == []


def test_an_enable_that_cannot_turn_another_version_off_says_to_enable_it_again(
    tmp_path, home, monkeypatch, capsys
):
    _, source = _installed_tools(tmp_path, servers={"db": {"command": "db"}})
    enable("tools", add_servers=True)
    _plugin(source, version="2.0.0")
    second = install("tools@m", _yes)
    full = _connections_on_a_full_disk(monkeypatch)
    assert main(["plugin", "enable", second.tree[:12]]) != 0
    said = capsys.readouterr().err
    assert "enable it again" in said and "disable it again" not in said
    full.clear()
    assert main(["plugin", "enable", second.tree[:12]]) == 0
    assert [p.tree for p in installed() if p.enabled] == [second.tree]
    assert ConnectionStore().list() == []


def test_an_enable_whose_servers_cannot_all_be_added_lists_those_it_added(
    tmp_path, home, monkeypatch, capsys
):
    _installed_tools(tmp_path, servers={"db": {"command": "db"}, "web": {"command": "web"}})
    add_one = ConnectionStore.add
    _adds_on_a_full_disk(monkeypatch, 2)
    assert main(["plugin", "enable", "tools@m", "--add-servers"]) != 0
    [plugin] = installed()
    [added] = ConnectionStore().list()
    assert not plugin.enabled and added.id in plugin.connections  # the one it added, listed
    monkeypatch.setattr(ConnectionStore, "add", add_one)
    assert main(["plugin", "enable", "tools@m", "--add-servers"]) == 0  # a retry is not refused
    assert sorted(c.name for c in ConnectionStore().list()) == ["tools-db", "tools-web"]
    assert main(["plugin", "disable", "tools@m"]) == 0
    assert ConnectionStore().list() == []


def test_an_enable_on_a_disk_that_fills_as_it_adds_a_connection_lists_it(
    tmp_path, home, monkeypatch, capsys
):
    from metabot import connections, plugins

    _installed_tools(tmp_path, servers={"db": {"command": "db"}})
    add_one, wrote, full = ConnectionStore.add, plugins.write_private, []

    def add_then_the_disk_is_full(store, **kw):
        conn = add_one(store, **kw)
        full.append(True)  # its write took the last of the disk
        return conn

    def write(path, data):
        if full:
            raise OSError(errno.ENOSPC, "No space left on device")
        return wrote(path, data)

    monkeypatch.setattr(ConnectionStore, "add", add_then_the_disk_is_full)
    monkeypatch.setattr(plugins, "write_private", write)
    monkeypatch.setattr(connections, "write_private", write)  # so no remove can be written
    assert main(["plugin", "enable", "tools@m", "--add-servers"]) != 0
    [plugin] = installed()
    [added] = ConnectionStore().list()
    assert added.id in plugin.connections  # so a retry, a disable or a remove takes it
    full.clear()
    monkeypatch.setattr(ConnectionStore, "add", add_one)
    assert main(["plugin", "enable", "tools@m", "--add-servers"]) == 0
    assert main(["plugin", "remove", "tools@m", "--yes"]) == 0
    assert ConnectionStore().list() == []


def _adds_on_a_full_disk(monkeypatch, at: int) -> list[str]:
    """ConnectionStore.add fails on its ``at``-th call."""
    add_one, asked = ConnectionStore.add, []

    def add_on_a_full_disk(store, **kw):
        asked.append(kw["name"])
        if len(asked) == at:
            raise OSError(errno.ENOSPC, "No space left on device")
        return add_one(store, **kw)

    monkeypatch.setattr(ConnectionStore, "add", add_on_a_full_disk)
    return asked


def test_an_enable_that_stops_part_way_runs_no_view_its_record_never_allowed(
    tmp_path, home, monkeypatch, capsys
):
    servers = {"db": {"command": "db"}, "web": {"command": "web"}}
    hooks = {"PreToolUse": "sh ${CLAUDE_PLUGIN_ROOT}/x.sh"}
    _installed_tools(tmp_path, servers=servers, hooks=hooks)
    enable("tools", without_hooks=True)
    _adds_on_a_full_disk(monkeypatch, 2)
    assert main(["plugin", "enable", "tools@m", "--allow-hooks", "--add-servers"]) != 0
    said = capsys.readouterr().err
    assert "tools@m 1.0.0" in said and "enable it again" in said
    [plugin] = installed()
    assert not plugin.hooks_allowed
    with pytest.raises(PluginError, match="view changed"):
        enabled_plugin_dirs()  # its hooks never run on a record that says they are off


def test_an_enable_whose_record_cannot_list_a_connection_never_adds_it(
    tmp_path, home, monkeypatch, capsys
):
    _installed_tools(tmp_path, servers={"db": {"command": "db"}})
    full = _full_records(monkeypatch)
    assert main(["plugin", "enable", "tools@m", "--add-servers"]) != 0
    said = capsys.readouterr().err
    assert "tools@m 1.0.0" in said and "enable it again" in said
    assert ConnectionStore().list() == []  # none that no record lists
    full.clear()
    assert main(["plugin", "enable", "tools@m", "--add-servers"]) == 0
    assert [c.name for c in ConnectionStore().list()] == ["tools-db"]


def test_a_plugin_s_long_version_never_hides_what_to_do(tmp_path, home, monkeypatch, capsys):
    _installed_tools(tmp_path, version="9" * 5000)
    enable("tools")
    _full_records(monkeypatch)
    assert main(["plugin", "disable", "tools@m"]) != 0
    said = capsys.readouterr().err
    assert "No space left on device" in said and "disable it again" in said


def test_a_server_that_would_take_a_name_metabot_keeps_is_refused_before_any_is_added(
    tmp_path, home
):
    market = _market(tmp_path, "skills")
    servers = {"db": {"command": "db"}, "_": {"command": "x"}}  # "skills-_" folds to "skills"
    _plugin(market / "plugins" / "skills", name="skills", servers=servers)
    add_marketplace("m", market)
    install("skills@m", _yes)
    with pytest.raises(PluginError, match="MetaBot keeps"):
        enable("skills", add_servers=True)
    assert ConnectionStore().list() == [] and [p.enabled for p in installed()] == [False]


def test_a_disable_with_connections_that_do_not_read_changes_nothing(tmp_path, home, capsys):
    _installed_tools(tmp_path, servers={"db": {"command": "db"}})
    view = enable("tools", add_servers=True).view
    ConnectionStore().path.write_text("{", encoding="utf-8")
    assert main(["plugin", "disable", "tools@m"]) != 0
    assert "connections.json" in capsys.readouterr().err
    assert [p.enabled for p in installed()] == [True] and enabled_plugin_dirs() == [view]


def test_a_remove_that_stops_part_way_keeps_no_record_of_a_copy_it_deleted(
    tmp_path, home, monkeypatch, capsys
):
    first, source = _installed_tools(tmp_path)
    _plugin(source, version="2.0.0")
    second = install("tools@m", _yes)
    rmtree = shutil.rmtree

    def refuse_the_second(path, *args, **kw):
        if Path(path) == second.root:
            raise PermissionError(errno.EACCES, "Permission denied", str(path))
        return rmtree(path, *args, **kw)

    monkeypatch.setattr("metabot.plugins.shutil.rmtree", refuse_the_second)
    assert main(["plugin", "remove", "tools@m", "--yes"]) != 0
    assert f"cannot delete {second.root}" in capsys.readouterr().err
    assert not first.root.exists()
    assert [p.tree for p in installed()] == [second.tree]  # the copy that stays, and only it


def test_runs_get_the_enabled_plugins(tmp_path, monkeypatch, capsys):
    _installed_tools(tmp_path)
    view = enable("tools").view
    argv_out = tmp_path / "argv.json"
    monkeypatch.setenv("FAKE_FIXTURE", str(FIXTURES / "claude-echo.jsonl"))
    monkeypatch.setenv("FAKE_ARGV_OUT", str(argv_out))
    from fake_engines import wrap
    from metabot.engines.claude import ClaudeEngine

    monkeypatch.setattr("metabot.commands.get_engine", lambda name: wrap(ClaudeEngine))
    assert main(["ask", "-e", "claude", "-q", "hi"]) == 0
    argv = json.loads(argv_out.read_text())["argv"]
    assert argv[argv.index("--plugin-dir") + 1] == str(view)


@pytest.mark.parametrize("text", ["{not json", "[1, 2]", "\udcff", '{"x": 1'])
@pytest.mark.parametrize(
    ("name", "read"), [("plugins.json", installed), ("marketplaces.json", marketplaces)]
)
def test_a_state_file_that_does_not_read_is_named(home, text, name, read):
    home.mkdir(parents=True, exist_ok=True)
    (home / name).write_text(text, encoding="utf-8", errors="surrogateescape")
    with pytest.raises(PluginError, match=re.escape(f"cannot read {home / name}")):
        read()


def test_a_plugin_record_without_its_fields_is_named(home):
    home.mkdir(parents=True, exist_ok=True)
    tree = "a" * 64
    (home / "plugins.json").write_text(json.dumps({tree: {"tree": tree}}), encoding="utf-8")
    with pytest.raises(PluginError, match="cannot read .*plugins.json"):
        installed()


def test_every_command_says_which_state_file_does_not_read(tmp_path, home, capsys, monkeypatch):
    home.mkdir(parents=True, exist_ok=True)
    (home / "plugins.json").write_text("{not json", encoding="utf-8")
    (home / "marketplaces.json").write_text("{not json", encoding="utf-8")
    replies = iter(["hello"])

    def typed(prompt=""):
        return next(replies)  # then StopIteration: chat never reads past the broken run

    monkeypatch.setattr("builtins.input", typed)
    for argv in (
        ["ask", "-e", "openai", "-q", "hi"],
        ["chat", "-e", "openai"],
        ["plugin", "list"],
        ["plugin", "marketplace", "list"],
        ["plugin", "marketplace", "add", "m", str(tmp_path)],
    ):
        code, out, err = _cli(capsys, *argv)
        assert code == 1, argv
        assert err.startswith(f"metabot: cannot read {home}"), (argv, err)


def _cli(capsys, *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def test_plugins_through_the_cli(tmp_path, capsys, monkeypatch):
    market = _market(tmp_path, "tools")
    _plugin(market / "plugins" / "tools", hooks={"Stop": "true"})
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert _cli(capsys, "plugin", "marketplace", "add", "m", str(market))[0] == 0
    code, out, _ = _cli(capsys, "plugin", "marketplace", "list")
    assert code == 0 and "m" in out and "tools" in out
    code, out, err = _cli(capsys, "plugin", "install", "tools@m")
    assert code == 2 and "--yes" in err
    code, out, err = _cli(capsys, "plugin", "install", "tools@m", "--yes")
    assert code == 0, err
    assert "skill hello" in out and "hook Stop: true" in out
    code, out, err = _cli(capsys, "plugin", "enable", "tools")
    assert code == 1 and "--allow-hooks" in err
    assert _cli(capsys, "plugin", "enable", "tools", "--without-hooks")[0] == 0
    code, out, _ = _cli(capsys, "plugin", "list")
    assert "tools@m" in out and "1.0.0" in out and "enabled" in out
    assert _cli(capsys, "plugin", "remove", "tools", "--yes")[0] == 0
    assert "no plugins" in _cli(capsys, "plugin", "list")[1]


def _installed(tmp_path: Path):
    market = _market(tmp_path, "tools")
    _plugin(market / "plugins" / "tools")
    add_marketplace("m", market)
    plugin = install("tools@m", _yes)
    enable("tools", without_hooks=True)
    return plugin


def test_an_edited_installed_plugin_is_refused_before_a_run(tmp_path, home):
    plugin = _installed(tmp_path)
    (plugin.root / "skills" / "hello" / "SKILL.md").write_text("swapped\n", encoding="utf-8")
    with pytest.raises(PluginError, match="changed"):
        enabled_plugins()


def test_an_added_file_in_an_installed_plugin_is_refused(tmp_path, home):
    plugin = _installed(tmp_path)
    (plugin.root / "extra.txt").write_text("x\n", encoding="utf-8")
    with pytest.raises(PluginError, match="changed"):
        enabled_plugins()


def test_a_removed_file_in_an_installed_plugin_is_refused(tmp_path, home):
    plugin = _installed(tmp_path)
    (plugin.root / "skills" / "hello" / "SKILL.md").unlink()
    with pytest.raises(PluginError, match="changed"):
        enabled_plugins()


def test_an_edited_view_is_refused_before_a_run(tmp_path, home):
    plugin = _installed(tmp_path)
    (plugin.view / "skills" / "hello" / "SKILL.md").write_text("planted\n", encoding="utf-8")
    with pytest.raises(PluginError, match="tools@m"):
        enabled_plugins()


def test_a_file_added_to_a_view_is_refused(tmp_path, home):
    plugin = _installed(tmp_path)
    (plugin.view / "hooks").mkdir()
    (plugin.view / "hooks" / "hooks.json").write_text("{}\n", encoding="utf-8")
    with pytest.raises(PluginError, match="tools@m"):
        enabled_plugins()


def test_a_link_in_a_view_is_refused(tmp_path, home):
    plugin = _installed(tmp_path)
    target = plugin.view / "skills" / "hello" / "SKILL.md"
    other = tmp_path / "elsewhere.md"
    other.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
    target.unlink()
    target.symlink_to(other)
    with pytest.raises(PluginError, match="tools@m"):
        enabled_plugins()


def test_an_untouched_view_still_runs(tmp_path, home):
    plugin = _installed(tmp_path)
    assert enabled_plugin_dirs() == [plugin.view]
    assert (plugin.view / "skills" / "hello" / "SKILL.md").is_file()


def test_a_symlink_in_an_installed_plugin_is_refused(tmp_path, home):
    plugin = _installed(tmp_path)
    target = plugin.root / "skills" / "hello" / "SKILL.md"
    body = target.read_text(encoding="utf-8")
    target.unlink()
    other = tmp_path / "elsewhere.md"
    other.write_text(body, encoding="utf-8")
    target.symlink_to(other)
    with pytest.raises(PluginError):
        enabled_plugins()


@pytest.mark.parametrize("source", ["../outside", "/tmp/outside", "~/outside"])
def test_a_source_that_leaves_the_marketplace_is_refused(tmp_path, home, source):
    market = tmp_path / "market"
    _write(
        market / ".claude-plugin" / "marketplace.json",
        json.dumps({"name": "m", "plugins": [{"name": "tools", "source": source}]}),
    )
    _plugin(tmp_path / "outside")
    add_marketplace("m", market)
    with pytest.raises(PluginError, match="outside|network|no plugin"):
        install("tools@m", _yes)


def test_a_symlinked_source_folder_is_refused(tmp_path, home):
    market = tmp_path / "market"
    outside = tmp_path / "outside"
    _plugin(outside)
    market.mkdir()
    (market / "linked").symlink_to(outside)
    _write(
        market / ".claude-plugin" / "marketplace.json",
        json.dumps({"name": "m", "plugins": [{"name": "tools", "source": "./linked"}]}),
    )
    add_marketplace("m", market)
    with pytest.raises(PluginError, match="outside"):
        install("tools@m", _yes)


@pytest.mark.parametrize("versions", [1, 2])
def test_a_connection_the_store_does_not_hold_is_never_shown_as_one(
    tmp_path, home, capsys, versions
):
    first, source = _installed_tools(tmp_path, servers={"db": {"command": "db"}})
    if versions == 2:  # a later version is installed, and the earlier one is the one enabled
        _plugin(source, version="2.0.0", servers={"db": {"command": "db2"}})
        install("tools@m", _yes)
    enable(first.tree[:12], add_servers=True)
    [conn] = ConnectionStore().list()
    ConnectionStore().remove(conn.id)  # as an enable that stopped part way can leave it
    capsys.readouterr()
    assert main(["plugin", "enable", first.tree[:12]]) == 0
    out = capsys.readouterr().out
    assert "connect test" not in out  # before: it said to test a connection that is not there
    assert f"connection {conn.id} is not there: add it with " in out
    remedy = f"metabot plugin enable {first.tree[:12]} --add-servers"
    assert remedy in out  # before: tools@m, which is the version installed last
    assert main(["plugin", "list"]) == 0
    assert "connection(s)" not in capsys.readouterr().out  # before: 1 connection(s)
    assert main(remedy.split()[1:]) == 0  # the line, as it says: the same version, whole
    assert [p.tree for p in installed() if p.enabled] == [first.tree]
    assert [c.command for c in ConnectionStore().list()] == [["db"]]


@pytest.mark.parametrize("where", ["inside", "itself"])
@pytest.mark.parametrize("how", ["install", "import-skills"])
def test_a_folder_that_cannot_be_listed_is_refused_not_installed_without_it(
    tmp_path, home, how, where
):
    market = _market(tmp_path, "tools")
    source = _plugin(market / "plugins" / "tools")
    skill = source / "skills" / "hello"
    _write(skill / "scripts" / "run.sh", "echo hello\n")
    given = source if how == "install" else skill  # the folder named: the plugin, or the skill
    door = skill / "scripts" if where == "inside" else given
    door.chmod(0o300)  # it can be entered, and not listed
    try:
        if os.access(door, os.R_OK):
            pytest.skip("this user lists any folder")
        # named in full: before, "scripts" or ".", which says neither which nor where
        with pytest.raises(PluginError, match=re.escape(f"{door} cannot be read: Permission")):
            if how == "install":
                add_marketplace("m", market)
                install("tools@m", _yes)
            else:
                import_skills(source / "skills", "mine", _yes)
    finally:
        door.chmod(0o755)
    assert installed() == []  # before: installed without the scripts, and pinned as whole
    for folder in (home / "plugins", home / "local-plugins"):
        assert not folder.is_dir() or list(folder.iterdir()) == []  # and no copy is left


def test_an_installed_copy_that_cannot_be_read_is_shown_with_why(tmp_path, home, capsys):
    plugin, _ = _installed_tools(tmp_path)
    door = plugin.root / "skills" / "hello"
    mode = door.stat().st_mode & 0o7777
    door.chmod(0o300)
    try:
        if os.access(door, os.R_OK):
            pytest.skip("this user lists any folder")
        code, out, err = _cli(capsys, "plugin", "show", "tools")
    finally:
        door.chmod(mode)
    assert code == 1 and err == ""
    assert "tools@m" in out and plugin.tree[:12] in out  # before: no row, the error alone
    assert f"cannot be checked: {door} cannot be read: Permission denied" in out
