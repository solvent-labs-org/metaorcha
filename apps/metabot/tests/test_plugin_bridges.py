"""Plugin bridges: a plugin may carry an engine, which runs only once the person allows it."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from metabot.cli import main
from metabot.engines import engine_names, get_engine, known_engine
from metabot.paths import run_path
from metabot.plugins import (
    PluginError,
    add_marketplace,
    disable,
    enable,
    inspect_plugin,
    install,
    marketplaces,
    permission_diff,
)
from plugin_kit import make_market as _market
from plugin_kit import make_plugin as _plugin
from plugin_kit import write as _write
from plugin_kit import yes as _yes
from test_engine_bridge import ECHO


def _with_bridge(root: Path, name: str = "echo", **spec) -> Path:
    """A plugin that carries one bridge: ``bin/<name>.py``, run by this Python."""
    _plugin(root)
    _write(root / "bin" / f"{name}.py", ECHO)
    doc = {"command": ["python3", f"bin/{name}.py"], **spec}
    _write(root / "metabot" / "bridges" / f"{name}.yaml", json.dumps(doc))  # JSON is YAML
    return root


def _installed(tmp_path: Path, **spec):
    market = _market(tmp_path, "tools")
    source = _with_bridge(market / "plugins" / "tools", **spec)
    add_marketplace("m", market)
    return install("tools@m", _yes), source


# ── what a plugin carries ───────────────────────────────────────────────────


def test_the_facts_and_the_diff_name_a_bridge_and_its_command(tmp_path):
    root = _with_bridge(tmp_path / "p")
    facts = inspect_plugin(root)
    assert facts.bridges == {"echo": "python3 bin/echo.py"}
    before = inspect_plugin(_plugin(tmp_path / "q"))
    assert "+ bridge echo: python3 bin/echo.py" in permission_diff(before, facts)


@pytest.mark.parametrize(
    ("spec", "said"),
    [
        ({"command": "bin/echo.py"}, "echo.yaml"),  # a command is a list of words
        ({"command": ["../outside.py"]}, "outside the plugin"),
        ({"command": ["/usr/bin/env", "x"]}, "outside the plugin"),
        ({"command": ["bin/echo.py"], "read_only": True}, "echo.yaml"),
    ],
)
def test_a_bridge_a_plugin_cannot_carry_is_refused_at_install(tmp_path, spec, said):
    root = _plugin(tmp_path / "p")
    _write(root / "metabot" / "bridges" / "echo.yaml", json.dumps(spec))
    with pytest.raises(PluginError, match=said):
        inspect_plugin(root)


def test_engines_shows_a_plugin_bridge_s_words_with_their_controls_escaped(tmp_path, capsys):
    words = ["\x1b[2Kpy\u202ethon9", "bin/echo.py", "--title=\x1b]0;mine\x07"]
    _installed(tmp_path, command=words)
    enable("tools", allow_bridges=True)
    code, out, _ = _cli(capsys, "engines")
    assert code == 0
    assert not {"\x1b", "\x07", "\u202e"} & set(out)  # it could retitle or reorder the line
    [line] = [line for line in out.splitlines() if "bridge from tools@m" in line]
    assert line.count("\\x1b[2Kpy\\u202ethon9") == 2  # the command, and the word not found


def test_a_plugin_bridge_may_not_take_a_built_in_engines_name(tmp_path):
    root = _with_bridge(tmp_path / "p", name="claude")
    with pytest.raises(PluginError, match="claude"):
        inspect_plugin(root)


# ── enabling ────────────────────────────────────────────────────────────────


def test_a_bridge_needs_the_persons_say(tmp_path):
    _installed(tmp_path)
    with pytest.raises(PluginError) as caught:
        enable("tools")
    assert "--allow-bridges" in str(caught.value) and "--without-bridges" in str(caught.value)
    enable("tools", without_bridges=True)
    assert not known_engine("echo")
    enable("tools", allow_bridges=True)
    assert known_engine("echo") and "echo" in engine_names()
    disable("tools")
    assert not known_engine("echo")


@pytest.mark.parametrize("chose", ["allow_bridges", "without_bridges"])
def test_the_line_a_copy_outside_this_home_is_refused_with_keeps_its_bridges(tmp_path, home, chose):
    from metabot.connections import ConnectionStore
    from metabot.upstream import misplaced_copy

    market = _market(tmp_path, "tools")
    source = _with_bridge(market / "plugins" / "tools")
    server = {"command": "python3", "args": ["${CLAUDE_PLUGIN_ROOT}/server.py"]}
    _write(source / ".mcp.json", json.dumps({"mcpServers": {"db": server}}))
    add_marketplace("m", market)
    plugin = install("tools@m", _yes)
    enable("tools", add_servers=True, **{chose: True})
    doc = json.loads((home / "connections.json").read_text(encoding="utf-8"))
    whole = doc["connections"][0]["command"]
    # as 0.2.0 kept it, enabled under METABOT_HOME=rh
    doc["connections"][0]["command"] = [w.replace(str(home), "rh") for w in whole]
    (home / "connections.json").write_text(json.dumps(doc), encoding="utf-8")
    [conn] = ConnectionStore().list()
    flag = "--" + chose.replace("_", "-")
    remedy = f"metabot plugin enable {plugin.tree[:12]} --add-servers {flag}"
    said = misplaced_copy(conn.command)
    assert said is not None and said.endswith(remedy)  # before: no --without-bridges: it failed
    assert main(remedy.split()[1:]) == 0
    assert ConnectionStore().get(conn.id).command == whole
    assert known_engine("echo") == (chose == "allow_bridges")


def test_a_plugin_that_is_off_has_no_bridge_whatever_its_record_holds(tmp_path, home):
    plugin, _ = _installed(tmp_path)
    enable("tools", allow_bridges=True)
    records = home / "plugins.json"
    data = json.loads(records.read_text(encoding="utf-8"))
    data[plugin.tree]["enabled"] = False  # an edited record: off, with its bridges left in
    records.write_text(json.dumps(data), encoding="utf-8")
    assert not known_engine("echo")


def test_a_plugin_bridge_runs_from_the_installed_copy(tmp_path):
    plugin, source = _installed(tmp_path)
    enable("tools", allow_bridges=True)
    engine = get_engine("echo")
    assert engine.command == ["python3", str(plugin.root / "bin" / "echo.py")]
    assert engine.source == f"plugin {plugin.pin}"


def test_a_bridge_name_already_taken_is_refused_at_enable(tmp_path, home):
    _installed(tmp_path)
    (home / "config.json").write_text(
        json.dumps({"bridges": {"echo": {"command": ["x"]}}}), encoding="utf-8"
    )
    with pytest.raises(PluginError, match="echo"):
        enable("tools", allow_bridges=True)


def test_a_plugin_whose_view_is_gone_runs_no_bridge(tmp_path):
    import shutil

    _installed(tmp_path)
    view = enable("tools", allow_bridges=True).view
    shutil.rmtree(view)  # as a disable that stopped once it had deleted it leaves it
    with pytest.raises(PluginError, match="disable it and enable it again"):
        get_engine("echo")


def test_a_changed_installed_copy_never_runs_its_bridge(tmp_path):
    plugin, _ = _installed(tmp_path)
    enable("tools", allow_bridges=True)
    (plugin.root / "bin" / "echo.py").write_text("print('changed')\n", encoding="utf-8")
    with pytest.raises(PluginError, match="changed since it was installed"):
        get_engine("echo")


def test_no_plugin_bridge_runs_its_copy_under_a_home_a_program_can_split(tmp_path, monkeypatch):
    where = tmp_path / "a b" / "mb"
    where.mkdir(parents=True)
    (tmp_path / "plain").symlink_to(where)
    monkeypatch.setenv("METABOT_HOME", str(tmp_path / "plain"))
    _installed(tmp_path)
    enable("tools", allow_bridges=True)  # the home by a name with nothing in it to split
    monkeypatch.setenv("METABOT_HOME", str(where))
    # before: it ran, and a shell behind env split the copy's path to run a file beside the home
    with pytest.raises(PluginError, match="bridge echo .* move the home"):
        get_engine("echo")
    disable("tools")
    with pytest.raises(PluginError, match="move the home .* --without-bridges"):
        enable("tools", allow_bridges=True)
    enable("tools", without_bridges=True)
    assert not known_engine("echo")


async def test_a_plugin_bridge_run_names_the_plugin_in_its_receipt(tmp_path):
    from test_engine_bridge import _run

    plugin, _ = _installed(tmp_path)
    enable("tools", allow_bridges=True)
    result = await _run(tmp_path)
    assert result.ok and result.final_text == "echo: hello"
    meta = json.loads((run_path(result.run_id) / "journal.json").read_text(encoding="utf-8"))
    assert meta["meta"]["bridge"]["source"] == f"plugin {plugin.pin}"


# ── plugin new ──────────────────────────────────────────────────────────────


def _cli(capsys, *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def test_plugin_new_writes_a_bridge_that_installs_and_runs(tmp_path, capsys, monkeypatch):
    folder = tmp_path / "mine"
    code, out, err = _cli(capsys, "plugin", "new", "hello", "--bridge", "--dir", str(folder))
    assert code == 0, err
    for rel in (
        ".claude-plugin/plugin.json",
        ".claude-plugin/marketplace.json",
        "metabot/bridges/hello.yaml",
        "bin/hello",
        "README.md",
    ):
        assert (folder / rel).is_file(), rel
    assert (folder / "bin" / "hello").stat().st_mode & stat.S_IXUSR
    assert "metabot plugin marketplace add" in out
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert _cli(capsys, "plugin", "marketplace", "add", "mine", str(folder))[0] == 0
    code, out, err = _cli(capsys, "plugin", "install", "hello@mine", "--yes")
    assert code == 0, err
    assert "bridge hello:" in out
    code, out, err = _cli(capsys, "plugin", "enable", "hello", "--allow-bridges")
    assert code == 0, err
    assert "metabot ask -e hello" in out
    code, out, _ = _cli(capsys, "plugin", "list")
    assert "bridges on: hello" in out
    code, out, _ = _cli(capsys, "engines")
    assert "bridge from hello@mine:" in out
    work = tmp_path / "work"
    work.mkdir()
    code, out, err = _cli(capsys, "ask", "-e", "hello", "--cwd", str(work), "say hi")
    assert code == 0, err
    assert out == "hello: say hi\n"


def test_the_steps_plugin_new_prints_install_two_new_plugins(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    steps = []
    for name, more in (("hello", ["--bridge"]), ("notes", [])):
        code, out, err = _cli(capsys, "plugin", "new", name, *more, "--dir", str(tmp_path / name))
        assert code == 0, err
        steps += [line.split()[1:] for line in out.splitlines()[1:]]
    installs = [argv + ["--yes"] for argv in steps if argv[1] == "install"]  # no terminal here
    for argv in steps:
        code, _, err = _cli(capsys, *(argv + ["--yes"] if argv[1] == "install" else argv))
        assert code == 0, (argv, err)
    _, out, _ = _cli(capsys, "plugin", "list")
    assert out.count("enabled") == 2, out
    (tmp_path / "hello" / "README.md").write_text("changed\n", encoding="utf-8")
    code, _, err = _cli(capsys, *installs[0])  # the first plugin again, once it changed
    assert code == 0, err


def _follow(capsys, out: str) -> None:
    """Run the steps ``plugin new`` printed, as a person would, with no terminal."""
    for argv in (line.split()[1:] for line in out.splitlines()[1:]):
        code, _, err = _cli(capsys, *(argv + ["--yes"] if argv[1] == "install" else argv))
        assert code == 0, (argv, err)


@pytest.mark.parametrize("name", ["local", "metabot"])
def test_plugin_new_never_names_a_marketplace_metabot_keeps(tmp_path, capsys, monkeypatch, name):
    # Review 10-28: its steps added a marketplace by a name MetaBot refuses.
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    code, out, err = _cli(capsys, "plugin", "new", name, "--dir", str(tmp_path / name))
    assert code == 0, err
    assert f"metabot plugin install {name}@{name}-2" in out
    _follow(capsys, out)
    readme = (tmp_path / name / "README.md").read_text(encoding="utf-8")
    assert f"metabot plugin marketplace add {name}-2 ." in readme
    assert f"metabot plugin install {name}@{name}-2" in readme


def test_plugin_new_keeps_a_marketplace_already_added_under_its_name(tmp_path, capsys, monkeypatch):
    # Review 10-28: its steps replaced that marketplace, whose plugins left the list.
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    theirs = add_marketplace("notes", _market(tmp_path, "tools", name="notes"))
    code, out, err = _cli(capsys, "plugin", "new", "notes", "--dir", str(tmp_path / "notes"))
    assert code == 0, err
    assert "metabot plugin install notes@notes-2" in out
    _follow(capsys, out)
    assert marketplaces()["notes"] == theirs
    assert marketplaces()["notes-2"] == (tmp_path / "notes").resolve()


def test_plugin_new_without_a_bridge_writes_a_skill(tmp_path, capsys):
    folder = tmp_path / "mine"
    assert _cli(capsys, "plugin", "new", "notes", "--dir", str(folder))[0] == 0
    facts = inspect_plugin(folder)
    assert list(facts.skills) == ["notes"] and facts.bridges == {}


def test_plugin_new_never_writes_over_a_folder(tmp_path, capsys):
    folder = tmp_path / "mine"
    folder.mkdir()
    (folder / "keep.txt").write_text("mine\n", encoding="utf-8")
    code, _, err = _cli(capsys, "plugin", "new", "hello", "--dir", str(folder))
    assert code == 2 and "not empty" in err
    assert sorted(p.name for p in folder.iterdir()) == ["keep.txt"]


def test_plugin_new_refuses_a_bad_name(tmp_path, capsys):
    code, _, err = _cli(capsys, "plugin", "new", "Bad Name", "--dir", str(tmp_path / "x"))
    assert code == 2 and "name" in err
    assert not (tmp_path / "x").exists()
