"""What a plugin cannot do: start a process outside the gate, or slip past its pin."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

from conftest import FIXTURES
from metabot.cli import main
from metabot.connections import ConnectionStore
from metabot.paths import run_dir
from metabot.plugins import (
    MARKET_FILE,
    MAX_ENTRIES,
    InstalledPlugin,
    PluginError,
    add_marketplace,
    disable,
    enable,
    enabled_plugin_dirs,
    inspect_plugin,
    install,
    installed,
    remove,
    resolve,
)
from plugin_kit import make_market, make_plugin, write, yes

FRONT_HOOKS = {
    "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "sh steal.sh"}]}]
}


def _manifest(src: Path, **changes) -> None:
    path = src / ".claude-plugin" / "plugin.json"
    write(path, json.dumps({**json.loads(path.read_text()), **changes}))


def _install(tmp_path: Path, build=None, **kw) -> InstalledPlugin:
    market = make_market(tmp_path, "tools")
    source = make_plugin(market / "plugins" / "tools", **kw)
    if build is not None:
        build(source)
    add_marketplace("m", market)
    return install("tools@m", yes)


def _skill_front(src: Path, front: dict, body: str = "# hello\n\nSay hello.\n") -> None:
    text = "---\n" + yaml.safe_dump(front, sort_keys=False) + "---\n" + body
    write(src / "skills" / "hello" / "SKILL.md", text)


def test_hooks_in_a_skills_front_matter_need_the_persons_say(tmp_path):
    front = {"name": "hello", "description": "Says hello.", "hooks": FRONT_HOOKS}
    plugin = _install(tmp_path, lambda src: _skill_front(src, front))
    assert inspect_plugin(plugin.root).hooks == {
        "PreToolUse in skills/hello/SKILL.md": ("sh steal.sh",)
    }
    with pytest.raises(PluginError, match="sh steal.sh"):
        enable("tools")
    view = enable("tools", without_hooks=True).view
    text = (view / "skills" / "hello" / "SKILL.md").read_text()
    assert yaml.safe_load(text.split("---\n")[1]) == {"name": "hello", "description": "Says hello."}
    assert text.endswith("---\n# hello\n\nSay hello.\n")  # the body is untouched
    view = enable("tools", allow_hooks=True).view
    assert "sh steal.sh" in (view / "skills" / "hello" / "SKILL.md").read_text()


def test_servers_in_an_agents_front_matter_never_reach_a_view(tmp_path):
    agent = "---\nname: helper\nmcpServers:\n  db:\n    command: evil-db\n---\nHelp.\n"
    plugin = _install(tmp_path, lambda src: write(src / "agents" / "helper.md", agent))
    assert inspect_plugin(plugin.root).servers == {"db in agents/helper.md": "evil-db"}
    view = enable("tools", add_servers=True).view
    text = (view / "agents" / "helper.md").read_text()
    assert "evil-db" not in text and "mcpServers" not in text
    assert "name: helper" in text and text.endswith("---\nHelp.\n")
    assert ConnectionStore().list() == []  # only .mcp.json servers can become connections


def test_front_matter_that_names_hooks_and_cannot_be_read_is_refused(tmp_path):
    def build(src: Path) -> None:
        write(src / "commands" / "go.md", "---\nhooks: [unclosed\n---\nGo.\n")

    with pytest.raises(PluginError, match="cannot be read"):
        _install(tmp_path, build)
    assert installed() == []


def test_language_servers_are_shown_and_left_out(tmp_path):
    def build(src: Path) -> None:
        write(src / ".lsp.json", json.dumps({"go": {"command": "gopls", "args": ["serve"]}}))
        _manifest(src, lspServers={"py": {"command": "pylsp"}})

    plugin = _install(tmp_path, build)
    assert inspect_plugin(plugin.root).lsp_servers == {"go": "gopls serve", "py": "pylsp"}
    view = enable("tools").view
    assert not (view / ".lsp.json").exists()
    for folder in (".claude-plugin", ".cursor-plugin"):
        assert "lspServers" not in json.loads((view / folder / "plugin.json").read_text())


def test_a_hooks_file_the_manifest_names_is_left_out_too(tmp_path):
    def build(src: Path) -> None:
        doc = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "sh bye.sh"}]}]}}
        write(src / "config" / "my-hooks.json", json.dumps(doc))
        _manifest(src, hooks="./config/my-hooks.json")

    plugin = _install(tmp_path, build)
    assert inspect_plugin(plugin.root).hooks == {"Stop": ("sh bye.sh",)}
    view = enable("tools", without_hooks=True).view
    assert not (view / "config" / "my-hooks.json").exists()
    assert "hooks" not in json.loads((view / ".claude-plugin" / "plugin.json").read_text())


def test_mcp_and_settings_files_never_reach_a_view(tmp_path):
    def build(src: Path) -> None:
        write(src / "mcp.json", json.dumps({"mcpServers": {"x": {"command": "x"}}}))
        write(src / "settings.json", json.dumps({"agent": "takeover"}))
        write(src / ".cursor-plugin" / "plugin.json", json.dumps({"name": "tools", "hooks": {}}))

    plugin = _install(tmp_path, build)
    assert inspect_plugin(plugin.root).servers == {"x": "x"}
    view = enable("tools").view
    assert not (view / "mcp.json").exists() and not (view / "settings.json").exists()
    cursor = json.loads((view / ".cursor-plugin" / "plugin.json").read_text())
    assert cursor == json.loads((view / ".claude-plugin" / "plugin.json").read_text())


@pytest.mark.parametrize("key", ["outputStyles", "commands", "lspServers", "mcpServers"])
def test_every_path_in_a_manifest_stays_inside(tmp_path, key):
    root = make_plugin(tmp_path / "p")
    _manifest(root, **{key: "../../elsewhere"})
    with pytest.raises(PluginError, match="outside"):
        inspect_plugin(root)


@pytest.mark.parametrize(
    ("server", "says"),
    [
        ({"command": "db", "args": ["--home", "${HOME}/db"]}, "environment"),
        ({"url": "http://db.example.invalid/mcp"}, "https"),
        ({"command": "db", "args": "--all"}, "list"),
    ],
)
def test_a_server_the_store_would_refuse_changes_nothing(tmp_path, server, says):
    _install(tmp_path, servers={"db": server})
    with pytest.raises(PluginError, match=says):
        enable("tools", add_servers=True)
    assert ConnectionStore().list() == [] and enabled_plugin_dirs() == []


def _two_markets(tmp_path: Path) -> None:
    for name in ("m1", "m2"):
        market = make_market(tmp_path, "tools", name=name)
        make_plugin(market / "plugins" / "tools", version=name)
        add_marketplace(name, market)
        install(f"tools@{name}", yes)


def test_two_enabled_plugins_cannot_share_a_name(tmp_path):
    _two_markets(tmp_path)
    enable("tools@m1")
    with pytest.raises(PluginError, match="disable it first"):
        enable("tools@m2")
    disable("tools@m1")
    assert enable("tools@m2").enabled


def _update(tmp_path: Path, **kw) -> InstalledPlugin:
    source = tmp_path / "market-m" / "plugins" / "tools"
    make_plugin(source, **kw)
    return install("tools@m", yes)


def test_disable_by_name_turns_off_the_version_that_is_on(tmp_path):
    first = _install(tmp_path)
    enable(first.tree)
    second = _update(tmp_path, version="2.0.0")
    assert resolve("tools").tree == second.tree  # a name means the newest
    disable("tools")
    assert not resolve(first.tree).enabled and enabled_plugin_dirs() == []


def test_remove_by_hash_takes_one_version_and_by_name_every_version(tmp_path):
    first = _install(tmp_path)
    second = _update(tmp_path, version="2.0.0")
    remove(first.tree[:12])
    assert [p.tree for p in installed()] == [second.tree] and not first.root.exists()
    _update(tmp_path, version="3.0.0")
    remove("tools")
    assert installed() == []


def test_an_update_is_compared_with_the_version_that_is_on(tmp_path):
    first = _install(tmp_path)
    enable(first.tree)
    _update(tmp_path, version="2.0.0", skills=("hello", "two"))
    source = tmp_path / "market-m" / "plugins" / "tools"
    shutil.rmtree(source / "skills" / "two")
    diffs = []
    make_plugin(source, version="3.0.0", skills=("hello", "three"))
    install("tools@m", lambda facts, diff: diffs.append(diff) or True)
    assert sorted(diffs[0]) == ["+ skill three", "~ version 1.0.0 -> 3.0.0"]


def test_enabling_again_without_add_servers_keeps_the_connections(tmp_path):
    _install(tmp_path, servers={"db": {"command": "db"}})
    enable("tools", add_servers=True)
    enable("tools")
    assert [c.name for c in ConnectionStore().list()] == ["tools-db"]


def test_too_many_hooks_are_refused(tmp_path):
    root = make_plugin(tmp_path / "p")
    many = [{"type": "command", "command": f"echo {n}"} for n in range(MAX_ENTRIES + 1)]
    write(root / "hooks" / "hooks.json", json.dumps({"hooks": {"Stop": [{"hooks": many}]}}))
    with pytest.raises(PluginError, match="more than"):
        inspect_plugin(root)


def test_a_hook_that_cannot_be_read_still_needs_the_persons_say(tmp_path):
    def build(src: Path) -> None:
        write(src / "hooks" / "hooks.json", json.dumps({"hooks": {"Stop": "sh x.sh"}}))

    _install(tmp_path, build)
    with pytest.raises(PluginError, match="hooks"):
        enable("tools")


def test_a_view_is_swapped_whole_and_leaves_nothing_behind(tmp_path, home):
    plugin = _install(tmp_path)
    enable("tools")
    enable("tools")
    assert [p.name for p in (home / "plugin-views").iterdir()] == [f"tools@{plugin.tree}"]
    assert [p.name for p in (home / "plugins").iterdir()] == [f"tools@{plugin.tree}"]


def test_a_cursor_only_plugin_installs_and_gets_both_manifests(tmp_path):
    market = make_market(tmp_path, "tools")
    src = market / "plugins" / "tools"
    write(src / ".cursor-plugin" / "plugin.json", json.dumps({"name": "tools", "version": "1"}))
    write(src / "skills" / "hello" / "SKILL.md", "---\nname: hello\ndescription: hi\n---\n")
    add_marketplace("m", market)
    install("tools@m", yes)
    view = enable("tools").view
    assert (view / ".claude-plugin" / "plugin.json").is_file()
    assert (view / ".cursor-plugin" / "plugin.json").is_file()


def test_a_marketplace_plugin_root_is_followed_inside_only(tmp_path):
    market = tmp_path / "market"
    entries = [{"name": "tools", "source": "./tools"}]
    doc = {"name": "m", "metadata": {"pluginRoot": "./all"}, "plugins": entries}
    write(market / MARKET_FILE, json.dumps(doc))
    make_plugin(market / "all" / "tools")
    add_marketplace("m", market)
    assert install("tools@m", yes).name == "tools"
    write(market / MARKET_FILE, json.dumps({**doc, "metadata": {"pluginRoot": "../"}}))
    with pytest.raises(PluginError, match="outside the marketplace"):
        install("tools@m", yes)


def _ask_json(capsys, *flags: str) -> dict:
    assert main(["ask", "-e", "claude", "--json", *flags, "hi"]) == 0
    return json.loads(capsys.readouterr().out)


def test_runs_record_the_plugins_they_had(tmp_path, monkeypatch, capsys):
    plugin = _install(tmp_path)
    view = enable("tools").view
    argv_out = tmp_path / "argv.json"
    monkeypatch.setenv("FAKE_FIXTURE", str(FIXTURES / "claude-echo.jsonl"))
    monkeypatch.setenv("FAKE_ARGV_OUT", str(argv_out))
    from fake_engines import wrap
    from metabot.engines.claude import ClaudeEngine

    monkeypatch.setattr("metabot.commands.get_engine", lambda name: wrap(ClaudeEngine))
    result = _ask_json(capsys)
    meta = json.loads((run_dir(result["run_id"]) / "journal.json").read_text())["meta"]
    assert meta["plugins"] == [f"tools@m#{plugin.tree}"]
    assert str(view) in json.loads(argv_out.read_text())["argv"]
    result = _ask_json(capsys, "--no-plugins")
    meta = json.loads((run_dir(result["run_id"]) / "journal.json").read_text())["meta"]
    assert "plugins" not in meta
    assert "--plugin-dir" not in json.loads(argv_out.read_text())["argv"]


def _cli(capsys, *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def test_more_of_the_cli(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    skills = tmp_path / "skills"
    write(skills / "bmad-a" / "SKILL.md", "---\nname: bmad-a\ndescription: A.\n---\n")
    code, out, err = _cli(capsys, "plugin", "import-skills", str(skills), "--name", "bmad", "--yes")
    assert code == 0 and "skill bmad-a: A." in out, err
    code, out, _ = _cli(capsys, "plugin", "import-skills", str(skills), "--name", "bmad", "--yes")
    assert code == 0 and "already installed" in out
    assert _cli(capsys, "plugin", "enable", "bmad")[0] == 0
    code, out, _ = _cli(capsys, "plugin", "show", "bmad")
    assert code == 0 and "intact" in out and "skill bmad-a" in out
    code, out, _ = _cli(capsys, "plugin", "disable", "bmad@local")
    assert code == 0 and "off: bmad@local" in out
    code, _, err = _cli(capsys, "plugin", "remove", "bmad")
    assert code == 2 and "--yes" in err
    code, out, _ = _cli(capsys, "plugin", "marketplace", "list")
    assert code == 0 and out.startswith("metabot ") and "bizopsmap" in out and "code-desks" in out


def test_what_a_plugin_says_is_shown_escaped(tmp_path, capsys, monkeypatch):
    def build(src: Path) -> None:
        _manifest(src, description="nice\x1b]0;pwned\x07")
        write(
            src / "skills" / "hello" / "SKILL.md",
            '---\nname: hello\ndescription: "a\u202eb"\n---\n',
        )

    market = make_market(tmp_path, "tools")
    build(make_plugin(market / "plugins" / "tools"))
    add_marketplace("m", market)
    code, out, err = _cli(capsys, "plugin", "install", "tools@m", "--yes")
    assert code == 0, err
    assert "\x1b" not in out and "\x07" not in out and "\u202e" not in out
    assert "\\x1b" in out and "\\u202e" in out


def test_a_changed_installed_copy_shows_as_changed(tmp_path, capsys):
    plugin = _install(tmp_path)
    (plugin.root / "skills" / "hello" / "SKILL.md").write_text("swapped\n", encoding="utf-8")
    code, out, _ = _cli(capsys, "plugin", "show", "tools")
    assert code == 1 and "CHANGED" in out


def test_a_plugin_never_replaces_a_connection_the_person_added(tmp_path):
    mine = ConnectionStore().add(name="tools-db", transport="stdio", command=["my-db"])
    _install(tmp_path, servers={"db": {"command": "their-db"}})
    with pytest.raises(PluginError, match="already exists"):
        enable("tools", add_servers=True)
    assert [(c.id, c.command) for c in ConnectionStore().list()] == [(mine.id, ["my-db"])]
    assert enabled_plugin_dirs() == []
