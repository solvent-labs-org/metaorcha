"""The ``metabot plugin`` commands: marketplaces, install, enable and the rest.

Everything a plugin says about itself is shown escaped, since a name or a
hook command could otherwise write over what the person reads.
"""

from __future__ import annotations

import argparse
import json
import stat
import sys
from pathlib import Path

from metabot.approvals import printable
from metabot.connections import ConnectionFileError, ConnectionStore
from metabot.plugins import (
    InstalledPlugin,
    PluginError,
    PluginFacts,
    add_marketplace,
    disable,
    enable,
    enable_line,
    import_skills,
    inspect_plugin,
    install,
    installed,
    market_plugins,
    marketplaces,
    remove,
    remove_marketplace,
    resolve,
    tree_sha256,
    versions,
)

EXIT_OK, EXIT_FAILED, EXIT_USAGE = 0, 1, 2
ASK_FIRST = "{what} asks first; run it in a terminal, or pass --yes"


def _err(text: str) -> None:
    print(f"metabot: {printable(text, 4000)}", file=sys.stderr)


def _s(value: object, limit: int = 200) -> str:
    return printable(str(value), limit)


def _unread(exc: OSError | PluginError) -> str:
    """What could not be read, and why."""
    if isinstance(exc, OSError):
        return f"{exc.filename or 'a file'} cannot be read: {exc.strerror or type(exc).__name__}"
    return str(exc)


def facts_lines(facts: PluginFacts) -> list[str]:
    """What a plugin carries, one line each, safe for a terminal."""
    lines = [f"{_s(facts.name, 64)} {_s(facts.version, 64)}  {_s(facts.description)}".rstrip()]
    for name, about in facts.skills.items():
        lines.append(f"  skill {_s(name, 80)}" + (f": {_s(about, 160)}" if about else ""))
    lines += [f"  command {_s(name, 80)}" for name in facts.commands]
    lines += [f"  agent {_s(name, 80)}" for name in facts.agents]
    lines += [f"  routine {_s(name, 80)}" for name in facts.routines]
    lines += [
        f"  hook {_s(event, 160)}: {_s(command, 300)}"
        for event, commands in facts.hooks.items()
        for command in commands
    ]
    lines += [
        f"  server {_s(name, 160)}: {_s(target, 300)}" for name, target in facts.servers.items()
    ]
    lines += [
        f"  lsp {_s(name, 80)}: {_s(target, 300)}" for name, target in facts.lsp_servers.items()
    ]
    lines += [
        f"  bridge {_s(name, 80)}: {_s(command, 300)}" for name, command in facts.bridges.items()
    ]
    if facts.hooks:
        lines.append(
            "hooks run outside MetaBot's gate: enabling asks you to allow them or leave them out"
        )
    if facts.servers:
        lines.append(
            "servers never start on their own: enable --add-servers makes them connections, "
            "behind the gate"
        )
    if facts.lsp_servers:
        lines.append("language servers are left out: they would start outside the gate")
    if facts.bridges:
        lines.append(
            "bridges are programs that run as engines, outside the gate: enabling asks you to "
            "allow them or leave them out"
        )
    return lines


def _ask(question: str, yes: bool) -> bool:
    if yes:
        return True
    if not sys.stdin.isatty():
        return False
    try:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def _installing(args: argparse.Namespace, run) -> int:
    """Show what a plugin carries, ask, install; the shared half of install and import."""
    asked: list[bool] = []

    def confirm(facts: PluginFacts, diff: list[str] | None) -> bool:
        asked.append(True)
        for line in facts_lines(facts):
            print(line)
        if diff is not None:
            print(
                "what changes from the version you have:" if diff else "nothing it carries changes"
            )
            for line in diff:
                print(f"  {_s(line, 400)}")
        return _ask("install it?", args.yes)

    try:
        plugin = run(confirm)
    except (PluginError, OSError) as exc:
        _err(str(exc))
        return EXIT_FAILED
    if plugin is None:
        if not args.yes and not sys.stdin.isatty():
            _err(f"nothing was installed: {ASK_FIRST.format(what='install')}")
            return EXIT_USAGE
        print("nothing was installed")
        return EXIT_FAILED
    if not asked:
        print(f"already installed: {plugin.ref} {_s(plugin.version, 64)} ({plugin.tree[:12]})")
        return EXIT_OK
    print(f"installed {plugin.ref} {_s(plugin.version, 64)} ({plugin.tree[:12]}); it is off")
    print(f"turn it on with: metabot plugin enable {plugin.tree[:12]}")
    return EXIT_OK


def cmd_plugin_install(args: argparse.Namespace) -> int:
    return _installing(args, lambda confirm: install(args.ref, confirm))


def cmd_plugin_import_skills(args: argparse.Namespace) -> int:
    return _installing(args, lambda confirm: import_skills(Path(args.dir), args.name, confirm))


def cmd_plugin_marketplace_add(args: argparse.Namespace) -> int:
    known = args.name in marketplaces()
    try:
        root = add_marketplace(args.name, Path(args.path))
    except PluginError as exc:
        _err(str(exc))
        return EXIT_USAGE
    print(f"{'updated' if known else 'added'} marketplace {args.name}: {_s(root, 300)}")
    print(f"install from it with: metabot plugin install NAME@{args.name}")
    return EXIT_OK


def cmd_plugin_marketplace_list(_args: argparse.Namespace) -> int:
    for name, root in marketplaces().items():
        print(f"{name}  {_s(root, 300)}")
        try:
            listed = market_plugins(root)
        except PluginError as exc:
            print(f"  ({_s(exc, 300)})")
            continue
        for plugin, about in listed:
            print(f"  {_s(plugin, 64):<24} {_s(about, 160)}".rstrip())
    return EXIT_OK


def cmd_plugin_marketplace_remove(args: argparse.Namespace) -> int:
    try:
        remove_marketplace(args.name)
    except PluginError as exc:
        _err(str(exc))
        return EXIT_USAGE
    print(f"removed marketplace {args.name}; plugins installed from it stay")
    return EXIT_OK


def _held() -> set[str] | None:
    """The ids of the connections the store holds; None when it does not read."""
    try:
        return {c.id for c in ConnectionStore().list()}
    except ConnectionFileError:
        return None


def _row(plugin: InstalledPlugin, held: set[str] | None) -> str:
    notes = []
    if plugin.hooks_allowed:
        notes.append("hooks on")
    if plugin.bridges:
        notes.append(f"bridges on: {', '.join(_s(n, 32) for n in plugin.bridges)}")
    # an id the store does not hold, as an enable that stopped part way leaves, is none
    connections = [c for c in plugin.connections if held is None or c in held]
    if connections:
        notes.append(f"{len(connections)} connection(s)")
    state = "enabled" if plugin.enabled else "off"
    head = f"{plugin.ref:<28} {_s(plugin.version, 24):<12} {state:<8} {plugin.tree[:12]}"
    return f"{head}  {', '.join(notes)}".rstrip()


def cmd_plugin_list(_args: argparse.Namespace) -> int:
    plugins = installed()
    if not plugins:
        print("no plugins; see what you can install with: metabot plugin marketplace list")
    held = _held() if plugins else None
    for plugin in plugins:
        print(_row(plugin, held))
    return EXIT_OK


def cmd_plugin_show(args: argparse.Namespace) -> int:
    try:
        plugin = resolve(args.ref)
    except (PluginError, OSError) as exc:
        _err(str(exc))
        return EXIT_FAILED
    try:
        intact = plugin.root.is_dir() and tree_sha256(plugin.root) == plugin.tree
        state = "intact" if intact else "CHANGED since install"
    except (PluginError, OSError) as exc:  # a folder or file in it that cannot be read
        intact, state = False, f"cannot be checked: {_unread(exc)}"
    try:
        facts = inspect_plugin(plugin.root) if intact else None
    except (PluginError, OSError) as exc:
        _err(str(exc))
        return EXIT_FAILED
    print(_row(plugin, _held()))
    print(f"  tree      {plugin.tree} ({_s(state, 600)})")
    print(f"  source    {_s(plugin.source, 300)}")
    print(f"  installed {plugin.installed_at}")
    if plugin.enabled:
        print(f"  view      {plugin.view}")
    store = ConnectionStore()
    for conn_id in plugin.connections:
        name = next((c.name for c in store.list() if c.id == conn_id), "(gone)")
        print(f"  connection {name} ({conn_id})")
    for line in facts_lines(facts)[1:] if facts else []:
        print(line)
    return EXIT_OK if intact else EXIT_FAILED


def cmd_plugin_enable(args: argparse.Namespace) -> int:
    try:
        plugin = enable(
            args.ref,
            allow_hooks=args.allow_hooks,
            without_hooks=args.without_hooks,
            add_servers=args.add_servers,
            allow_bridges=args.allow_bridges,
            without_bridges=args.without_bridges,
        )
    except (PluginError, ValueError, OSError) as exc:
        _err(str(exc))
        return EXIT_FAILED
    print(f"enabled {plugin.ref} {_s(plugin.version, 64)} ({plugin.tree[:12]})")
    if plugin.hooks_allowed:
        print("its hooks run, outside MetaBot's gate")
    for name in plugin.bridges:
        print(f'bridge {_s(name, 32)} is an engine: metabot ask -e {_s(name, 32)} "TASK"')
    store = ConnectionStore()
    for conn_id in plugin.connections:
        held_as = next((c.name for c in store.list() if c.id == conn_id), None)
        if held_as is None:  # listed by an enable that stopped part way
            print(f"connection {conn_id} is not there: add it with {enable_line(plugin)}")
        else:
            print(f"connection {held_as}: check it with metabot connect test {held_as}")
    return EXIT_OK


def cmd_plugin_disable(args: argparse.Namespace) -> int:
    try:
        plugins = disable(args.ref)
    except (PluginError, OSError) as exc:
        _err(str(exc))
        return EXIT_FAILED
    for plugin in plugins:
        print(f"off: {plugin.ref} ({plugin.tree[:12]})")
    return EXIT_OK


def cmd_plugin_remove(args: argparse.Namespace) -> int:
    if not args.yes and not sys.stdin.isatty():
        _err(ASK_FIRST.format(what="remove"))
        return EXIT_USAGE
    try:
        doomed = versions(args.ref)
    except PluginError as exc:
        _err(str(exc))
        return EXIT_FAILED
    held = _held()
    for plugin in doomed:
        print(f"  {_row(plugin, held)}")
    if not _ask(f"remove {'these' if len(doomed) > 1 else 'it'}?", args.yes):
        print("nothing was removed")
        return EXIT_FAILED
    try:
        removed = remove(args.ref)
    except (PluginError, OSError) as exc:
        _err(str(exc))
        return EXIT_FAILED
    for plugin in removed:
        print(f"removed {plugin.ref} ({plugin.tree[:12]})")
    return EXIT_OK


# ── plugin new ──────────────────────────────────────────────────────────────

BRIDGE_PROGRAM = """#!/usr/bin/env python3
\"\"\"{name}: a MetaBot bridge (metabot.bridge/1) that answers with the task it is given.

MetaBot runs this file with the path of a job file as its last word. The job
is JSON: format, prompt, model, cwd, read_only, switchboard and resume. The
answer is JSON lines on stdout, one event per line: started, text, tool_call,
tool_result, usage, final, error or notice. Each tool_call has a call_id no
other call in the run has. A usage line holds one request's token counts, or
with "total": true the run's whole so far. Put your engine where the answer
is made.
\"\"\"

import json
import sys


def say(**event):
    print(json.dumps(event), flush=True)


def main():
    with open(sys.argv[-1], encoding="utf-8") as f:
        job = json.load(f)
    say(type="started", session_id="{name}-1", model=job.get("model"))
    answer = "{name}: " + job["prompt"]  # your engine goes here
    say(type="final", text=answer, session_id="{name}-1")


if __name__ == "__main__":
    main()
"""

BRIDGE_SPEC = """# {name}: a program MetaBot runs as an engine, by metabot.bridge/1.
# command: the program and its words. A path names a file in this plugin.
command: [bin/{name}]
# models: the models it takes; the first is the one a run gets by default.
models: []
# tools: own (its own tools, which MetaBot does not gate), or switchboard (it
# is also given MetaBot's switchboard, whose tools are gated; it reports a call
# through it as metabot/ and the tool's name, which the switchboard records).
tools: own
# read_only: honored when it keeps to a job that may not write; else no.
read_only: honored
"""

SKILL = """---
name: {name}
description: 'Say what this skill does, and when an agent should use it.'
---

# {name}

Write the steps here.
"""

PLUGIN_README = """# {name}

A MetaBot plugin, and a marketplace that lists it. From this folder:

```sh
metabot plugin marketplace add {market} .
metabot plugin install {name}@{market}
metabot plugin enable {name}{enable}
```
{more}
Keep it to yourself, or share the folder: anyone can add it as a marketplace,
and MetaBot pins what they install by its hash.
"""

BRIDGE_README = """
Then run a task on it: `metabot ask -e {name} "say hi"`. The bridge is
`bin/{name}`; `metabot/bridges/{name}.yaml` says how MetaBot runs it.
"""


def _market_name(name: str, folder: Path) -> str:
    """The new folder's marketplace name: the plugin's own, unless MetaBot keeps it or it is taken.

    Adding a marketplace under a name already added replaces that one, and its
    plugins leave the list, so a name taken by another folder gets a number.
    """
    from metabot.plugins import BUILTIN, LOCAL, marketplaces

    added = marketplaces()
    market, n = name, 1
    while market in (LOCAL, BUILTIN) or (market in added and added[market] != folder):
        n += 1
        market = f"{name}-{n}"
    return market


def cmd_plugin_new(args: argparse.Namespace) -> int:
    """Write a plugin to start from: a bridge, with --bridge, or a skill."""
    from metabot.config import ENDPOINT_NAME_RE
    from metabot.engines import ENGINES
    from metabot.plugins import NAME

    name = args.name
    if not NAME.match(name) or (
        args.bridge and (not ENDPOINT_NAME_RE.match(name) or name in ENGINES)
    ):
        shape = (
            "lower-case letters, digits and '-'"
            if args.bridge
            else "lower-case letters, digits, '.', '-' or '_'"
        )
        taken = f", and not {', '.join(ENGINES)}" if args.bridge else ""
        _err(f"a plugin's name is {shape}{taken}: {name!r}")
        return EXIT_USAGE
    folder = Path(args.dir or name).expanduser().resolve()
    if folder.exists() and (not folder.is_dir() or any(folder.iterdir())):
        _err(f"{_s(folder, 300)} is not empty; give a new folder with --dir")
        return EXIT_USAGE
    about = f"the {name} bridge" if args.bridge else f"the {name} skill"
    market = _market_name(name, folder)
    files = {
        ".claude-plugin/plugin.json": json.dumps(
            {"name": name, "version": "0.1.0", "description": about}, indent=2
        )
        + "\n",
        ".claude-plugin/marketplace.json": json.dumps(
            {"name": market, "plugins": [{"name": name, "source": "./", "description": about}]},
            indent=2,
        )
        + "\n",
        "README.md": PLUGIN_README.format(
            name=name,
            market=market,
            enable=" --allow-bridges" if args.bridge else "",
            more=BRIDGE_README.format(name=name) if args.bridge else "",
        ),
    }
    if args.bridge:
        files[f"metabot/bridges/{name}.yaml"] = BRIDGE_SPEC.format(name=name)
        files[f"bin/{name}"] = BRIDGE_PROGRAM.format(name=name)
    else:
        files[f"skills/{name}/SKILL.md"] = SKILL.format(name=name)
    for rel, text in files.items():
        path = folder / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    if args.bridge:
        program = folder / "bin" / name
        program.chmod(program.stat().st_mode | stat.S_IXUSR)
    print(f"wrote the {name} plugin in {_s(folder, 300)}")
    # The marketplace is named for the plugin: one named for another plugin would be replaced.
    print(f"metabot plugin marketplace add {market} {_s(folder, 300)}")
    print(f"metabot plugin install {name}@{market}")
    print(f"metabot plugin enable {name}" + (" --allow-bridges" if args.bridge else ""))
    return EXIT_OK
