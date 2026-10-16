"""Plugins and marketplaces built in a test's own folder."""

from __future__ import annotations

import json
from pathlib import Path


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def make_plugin(
    root: Path,
    name: str = "tools",
    version: str = "1.0.0",
    skills: tuple[str, ...] = ("hello",),
    hooks: dict[str, str] | None = None,
    servers: dict | None = None,
    commands: tuple[str, ...] = (),
    routines: tuple[str, ...] = (),
) -> Path:
    manifest = {"name": name, "version": version, "description": f"the {name} plugin"}
    write(root / ".claude-plugin" / "plugin.json", json.dumps(manifest))
    for skill in skills:
        write(
            root / "skills" / skill / "SKILL.md",
            f"---\nname: {skill}\ndescription: 'Says {skill}.'\n---\n\n# {skill}\n",
        )
    if hooks:
        doc = {
            "hooks": {
                event: [{"hooks": [{"type": "command", "command": cmd}]}]
                for event, cmd in hooks.items()
            }
        }
        write(root / "hooks" / "hooks.json", json.dumps(doc))
    if servers:
        write(root / ".mcp.json", json.dumps({"mcpServers": servers}))
    for command in commands:
        write(root / "commands" / f"{command}.md", f"Do {command}.\n")
    for routine in routines:
        write(root / "metabot" / "routines" / f"{routine}.yaml", "name: r\n")
    return root


def make_market(tmp_path: Path, *plugins: str, name: str = "m") -> Path:
    market = tmp_path / f"market-{name}"
    entries = [{"name": p, "source": f"./plugins/{p}", "description": p} for p in plugins]
    write(
        market / ".claude-plugin" / "marketplace.json",
        json.dumps({"name": name, "owner": {"name": "test"}, "plugins": entries}),
    )
    return market


def yes(*_args) -> bool:
    return True
