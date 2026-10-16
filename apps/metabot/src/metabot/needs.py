"""The commands bundled plugins call, so doctor can say when one is missing.

``bizopsmap`` skills call ``bizopsmap``. ``code-desks`` skills call ``desk``.
A plugin that is not one of these, or that is not enabled, is left out.
``cli.py`` is what prints the lines; this module does not.
"""

from __future__ import annotations

import shutil

from metabot.plugins import enabled_plugins

# plugin name -> (command, where it comes from when it is not on PATH)
_NEEDS = {
    "bizopsmap": ("bizopsmap", "with bizopsmap"),
    "code-desks": ("desk", "with Code Desks"),
}


def doctor_lines() -> list[str]:
    """One line per command an enabled bundled plugin calls."""
    lines: list[str] = []
    for plugin in enabled_plugins():
        spec = _NEEDS.get(plugin.name)
        if spec is None or not plugin.enabled:
            continue
        cli, comes = spec
        found = shutil.which(cli)
        where = found if found else f"missing: it comes {comes}"
        lines.append(f"plugin   {plugin.name}  needs {cli}: {where}")
    return lines
