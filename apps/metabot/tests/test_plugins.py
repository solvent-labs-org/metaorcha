"""Plugins install pinned by tree hash; hooks and servers need the person's say."""

from __future__ import annotations

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
