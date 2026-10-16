"""The ``metabot plugin`` commands: marketplaces, install, enable and the rest.

Everything a plugin says about itself is shown escaped, since a name or a
hook command could otherwise write over what the person reads.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from metabot.approvals import printable
from metabot.connections import ConnectionStore
from metabot.plugins import (
    InstalledPlugin,
    PluginError,
    PluginFacts,
    add_marketplace,
    disable,
    enable,
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


def _row(plugin: InstalledPlugin) -> str:
    notes = []
    if plugin.hooks_allowed:
        notes.append("hooks on")
    if plugin.connections:
        notes.append(f"{len(plugin.connections)} connection(s)")
    state = "enabled" if plugin.enabled else "off"
    head = f"{plugin.ref:<28} {_s(plugin.version, 24):<12} {state:<8} {plugin.tree[:12]}"
    return f"{head}  {', '.join(notes)}".rstrip()


def cmd_plugin_list(_args: argparse.Namespace) -> int:
    plugins = installed()
    if not plugins:
        print("no plugins; see what you can install with: metabot plugin marketplace list")
    for plugin in plugins:
        print(_row(plugin))
    return EXIT_OK


def cmd_plugin_show(args: argparse.Namespace) -> int:
    try:
        plugin = resolve(args.ref)
        intact = plugin.root.is_dir() and tree_sha256(plugin.root) == plugin.tree
        facts = inspect_plugin(plugin.root) if intact else None
    except (PluginError, OSError) as exc:
        _err(str(exc))
        return EXIT_FAILED
    print(_row(plugin))
    print(f"  tree      {plugin.tree} ({'intact' if intact else 'CHANGED since install'})")
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
        )
    except (PluginError, ValueError, OSError) as exc:
        _err(str(exc))
        return EXIT_FAILED
    print(f"enabled {plugin.ref} {_s(plugin.version, 64)} ({plugin.tree[:12]})")
    if plugin.hooks_allowed:
        print("its hooks run, outside MetaBot's gate")
    store = ConnectionStore()
    for conn_id in plugin.connections:
        name = next((c.name for c in store.list() if c.id == conn_id), conn_id)
        print(f"connection {name}: check it with metabot connect test {name}")
    return EXIT_OK


def cmd_plugin_disable(args: argparse.Namespace) -> int:
    try:
        plugins = disable(args.ref)
    except PluginError as exc:
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
    for plugin in doomed:
        print(f"  {_row(plugin)}")
    if not _ask(f"remove {'these' if len(doomed) > 1 else 'it'}?", args.yes):
        print("nothing was removed")
        return EXIT_FAILED
    try:
        removed = remove(args.ref)
    except PluginError as exc:
        _err(str(exc))
        return EXIT_FAILED
    for plugin in removed:
        print(f"removed {plugin.ref} ({plugin.tree[:12]})")
    return EXIT_OK
