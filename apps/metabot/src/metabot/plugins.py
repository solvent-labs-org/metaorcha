"""Plugins: skills, commands and agents from a marketplace, pinned by hash.

A plugin is a folder in the plugin layout the ``claude`` CLI reads: ``.claude-plugin/
plugin.json`` (or ``.cursor-plugin/plugin.json``), ``skills/*/SKILL.md``,
``commands/``, ``agents/``, ``hooks/``, ``.mcp.json`` and ``.lsp.json``, plus
MetaBot's ``metabot/routines/*.yaml`` and ``metabot/bridges/*.yaml``. A
marketplace is a folder whose ``.claude-plugin/marketplace.json`` lists
plugins by path. This version installs from folders on this machine; an
entry that must be fetched needs the network and is refused.

Install copies the plugin first, and everything shown and hashed is that
copy, kept at ``<METABOT_HOME>/plugins/<name>@<tree hash>``. Records are
keyed by the hash, never by the name, so nothing can be swapped in under a
name. The person sees what a plugin carries before it is installed; an
update shows what changed since the version they have.

Enabling builds the copy the engines get, the view. What would start a
process outside MetaBot's gate stays out of it: MCP servers, language
servers and settings always, hooks unless the person allows them. That
holds for hooks and servers in a skill's or agent's front matter too. A
plugin's MCP servers run only as connections, behind the gate, and only when
the person asks. The view carries a ``.claude-plugin`` and a
``.cursor-plugin`` manifest, so either engine loads it.

A plugin's bridges, programs that run as engines, are engines only once the
person allows them. A path in a bridge's command names a file the plugin
holds, so what a bridge runs is the installed copy, and its hash is checked
again before each run.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml
from emerge.journal import utc_now

from metabot.approvals import printable
from metabot.config import bridge_ok
from metabot.connections import NAME_RE as CONNECTION_NAME
from metabot.connections import RESERVED as RESERVED_NAMES
from metabot.connections import ConnectionStore, check_spec, new_connection_id
from metabot.engines import ENGINES
from metabot.paths import metabot_home, private_dir, write_private

CLAUDE_MANIFEST = Path(".claude-plugin") / "plugin.json"
CURSOR_MANIFEST = Path(".cursor-plugin") / "plugin.json"
MARKET_FILE = Path(".claude-plugin") / "marketplace.json"
MARKETS = "marketplaces.json"
RECORDS = "plugins.json"
LOCAL = "local"  # the marketplace of skills imported from this machine
BUILTIN = "metabot"  # the marketplace that ships inside this package
NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
TREE = re.compile(r"^[0-9a-f]{64}$")
HEX = re.compile(r"^[0-9a-f]+$")
MIN_PREFIX = 8
MAX_FILES = 5000
MAX_BYTES = 50 * 1024 * 1024
MAX_ENTRIES = 200  # hooks, servers or language servers in one plugin
PLUGIN_ROOT_VAR = "${CLAUDE_PLUGIN_ROOT}"
PATH_KEYS = ("commands", "agents", "skills", "hooks", "mcpServers", "lspServers", "outputStyles")
MCP_FILES = (".mcp.json", "mcp.json")
LSP_FILES = (".lsp.json",)
NEVER_IN_A_VIEW = ("mcpServers", "lspServers")  # manifest and front-matter keys
LEFT_OUT = frozenset({*MCP_FILES, *LSP_FILES, "settings.json"})
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_FRONT = re.compile(r"\A\ufeff?\s*---[ \t]*\r?\n(.*?)^---[ \t]*\r?$", re.S | re.M)
_RISKY = re.compile(r"(?m)^\s*['\"]?(hooks|mcpServers|lspServers)['\"]?\s*:")

Confirm = Callable[["PluginFacts", "list[str] | None"], bool]


class PluginError(ValueError):
    pass


@dataclass(frozen=True)
class PluginFacts:
    name: str
    version: str
    description: str
    skills: dict[str, str]  # name -> description
    commands: tuple[str, ...]
    agents: tuple[str, ...]
    hooks: dict[str, tuple[str, ...]]  # event -> commands
    servers: dict[str, str]  # name -> command line or url
    routines: tuple[str, ...]
    lsp_servers: dict[str, str] = field(default_factory=dict)  # never run by MetaBot
    bridges: dict[str, str] = field(default_factory=dict)  # name -> its command line


@dataclass
class InstalledPlugin:
    tree: str
    name: str
    market: str
    version: str
    source: str
    installed_at: str
    enabled: bool = False
    hooks_allowed: bool = False
    connections: list[str] = field(default_factory=list)  # ids of connections it added
    seq: int = 0  # install order: a clock can tie, this cannot
    view_tree: str = ""  # hash of the view enable built; empty while the plugin is off
    # name -> spec, its paths in the installed copy; empty unless the person allowed them
    bridges: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def ref(self) -> str:
        return f"{self.name}@{self.market}"

    @property
    def pin(self) -> str:
        """What a run records: the plugin and the exact tree it ran with."""
        return f"{self.ref}#{self.tree}"

    @property
    def root(self) -> Path:
        return metabot_home() / "plugins" / f"{self.name}@{self.tree}"

    @property
    def view(self) -> Path:
        return metabot_home() / "plugin-views" / f"{self.name}@{self.tree}"


# ── the tree ────────────────────────────────────────────────────────────────


def _utf_8(text: str) -> bool:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _files(root: Path) -> list[tuple[str, Path]]:
    """Every file of a plugin, sorted by path; a link or special file is refused, and so is a
    folder in it that cannot be listed, which would be installed as nothing."""
    if root.is_symlink() or not root.is_dir():
        raise PluginError(f"{root} is not a plugin folder")
    found: list[tuple[str, Path]] = []

    def unread(exc: OSError) -> None:
        folder = os.path.abspath(exc.filename) if exc.filename else root.absolute()
        raise PluginError(f"{folder} cannot be read: {exc.strerror or exc}")

    for dirpath, dirnames, filenames in os.walk(root, onerror=unread, followlinks=False):
        here = Path(dirpath)
        for name in list(dirnames):
            if (here / name).is_symlink():
                rel = (here / name).relative_to(root).as_posix()
                raise PluginError(f"{rel!r} is a link; a plugin holds plain files")
            if name == ".git" and here == root:
                dirnames.remove(name)  # a checkout's history is not the plugin
        for name in filenames:
            path = here / name
            rel = path.relative_to(root).as_posix()
            if _CONTROL.search(rel):
                raise PluginError(f"{rel!r}: a control character in a file name")
            if not _utf_8(rel):
                raise PluginError(f"{rel!r}: a file name that is not UTF-8")
            if path.is_symlink():
                raise PluginError(f"{rel!r} is a link; a plugin holds plain files")
            if not stat.S_ISREG(path.lstat().st_mode):
                raise PluginError(f"{rel!r} is not a regular file")
            found.append((rel, path))
            if len(found) > MAX_FILES:
                raise PluginError(f"more than {MAX_FILES} files")
    return sorted(found)


def tree_sha256(root: Path) -> str:
    """One hash over every file's path, executable bit and content."""
    digest = hashlib.sha256()
    total = 0
    for rel, path in _files(root):
        data = path.read_bytes()
        total += len(data)
        if total > MAX_BYTES:
            raise PluginError(f"larger than {MAX_BYTES} bytes")
        mode = "x" if path.stat().st_mode & 0o111 else "-"
        digest.update(f"{rel}\0{mode}\0{hashlib.sha256(data).hexdigest()}\n".encode())
    return digest.hexdigest()


def _copy_files(src: Path, dst: Path, skip: Callable[[str], bool] = lambda rel: False) -> None:
    total = 0
    for rel, path in _files(src):
        if skip(rel):
            continue
        data = path.read_bytes()
        total += len(data)
        if total > MAX_BYTES:
            raise PluginError(f"larger than {MAX_BYTES} bytes")
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        if path.stat().st_mode & 0o111:  # a script stays runnable; the umask sets the rest
            target.chmod(target.stat().st_mode | stat.S_IXUSR)


# ── what a plugin carries ───────────────────────────────────────────────────


def _json(path: Path, what: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError) as exc:
        raise PluginError(f"{what}: cannot read {path.name}: {type(exc).__name__}") from None


def _inside(root: Path, rel: Any, what: str, within: str = "the plugin") -> Path:
    if not isinstance(rel, str) or not rel:
        raise PluginError(f"{what}: expected a path")
    path = (root / rel).resolve()
    if not path.is_relative_to(root.resolve()):
        raise PluginError(f"{what} points outside {within}: {rel}")
    return path


def _paths(value: Any, root: Path, what: str) -> list[Path]:
    items = [value] if isinstance(value, str) else value if isinstance(value, list) else []
    return [_inside(root, item, what) for item in items]


def _manifest(root: Path) -> dict[str, Any]:
    path = next(
        (root / m for m in (CLAUDE_MANIFEST, CURSOR_MANIFEST) if (root / m).is_file()), None
    )
    if path is None:
        raise PluginError(f"no {CLAUDE_MANIFEST.as_posix()} in {root.name}")
    data = _json(path, "plugin.json")
    if not isinstance(data, dict):
        raise PluginError("plugin.json: expected an object")
    name = data.get("name")
    if not isinstance(name, str) or not NAME.match(name):
        raise PluginError("plugin.json: a name is lower-case letters, digits, '.', '-' or '_'")
    for key in PATH_KEYS:  # every path an engine would follow stays inside
        _paths(data.get(key), root, key)
    return data


def _text(path: Path) -> str:
    return path.read_bytes().decode("utf-8", errors="surrogateescape")


def _front(text: str) -> tuple[dict[str, Any] | None, re.Match[str] | None]:
    """A markdown file's front matter, parsed, and the match that holds it."""
    match = _FRONT.match(text)
    if match is None:
        return None, None
    try:
        meta = yaml.safe_load(match.group(1))
    except (yaml.YAMLError, ValueError, RecursionError):
        return None, match
    return (meta if isinstance(meta, dict) else None), match


def _one_line(value: Any) -> str:
    return " ".join(str(value if value is not None else "").split())


def _skill_files(root: Path, manifest: dict[str, Any]) -> list[Path]:
    found: list[Path] = []
    for base in [root / "skills", *_paths(manifest.get("skills"), root, "skills")]:
        if (base / "SKILL.md").is_file():
            found.append(base / "SKILL.md")  # the manifest named one skill's folder
        elif base.is_dir():
            found += [d / "SKILL.md" for d in sorted(base.iterdir()) if (d / "SKILL.md").is_file()]
    return found


def _skills(root: Path, manifest: dict[str, Any]) -> dict[str, str]:
    found: dict[str, str] = {}
    for md in _skill_files(root, manifest):
        meta = _front(_text(md))[0] or {}
        found[_one_line(meta.get("name") or md.parent.name)] = _one_line(meta.get("description"))
    return dict(sorted(found.items()))


def _markdown_names(root: Path, folder: str, manifest: dict[str, Any]) -> tuple[str, ...]:
    names: set[str] = set()
    for base in [root / folder, *_paths(manifest.get(folder), root, folder)]:
        if base.is_file() and base.suffix == ".md":
            names.add(base.stem)
        elif base.is_dir():
            names.update(p.stem for p in base.glob("*.md") if p.is_file())
    return tuple(sorted(names))


def _add(found: dict[str, list[str]], key: str, text: str) -> None:
    if sum(map(len, found.values())) >= MAX_ENTRIES:
        raise PluginError(f"more than {MAX_ENTRIES} hooks")
    found.setdefault(key, []).append(text)


def _collect_hooks(events: Any, found: dict[str, list[str]], where: str = "") -> None:
    """Each hook of ``{event: [{"hooks": [{...}]}]}``; one that cannot be read still counts."""

    def key(event: Any) -> str:
        return f"{_one_line(event)} in {where}" if where else _one_line(event)

    if not isinstance(events, dict):
        _add(found, key("hooks"), "(hooks MetaBot cannot read)")
        return
    for event, groups in events.items():
        for group in groups if isinstance(groups, list) else [None]:
            hooks = group.get("hooks") if isinstance(group, dict) else None
            for hook in hooks if isinstance(hooks, list) else [None]:
                if isinstance(hook, dict) and hook.get("command"):
                    _add(found, key(event), _one_line(hook["command"]))
                elif isinstance(hook, dict) and hook.get("type"):
                    _add(found, key(event), f"({_one_line(hook['type'])} hook)")
                else:
                    _add(found, key(event), "(a hook MetaBot cannot read)")


def _hook_docs(root: Path, manifest: dict[str, Any]) -> list[Any]:
    """Every hooks object: ``hooks/hooks.json`` and what the manifest adds."""
    docs: list[Any] = []
    paths: list[Path] = []
    default = root / "hooks" / "hooks.json"
    if default.is_file():
        paths.append(default.resolve())
    spec = manifest.get("hooks")
    if isinstance(spec, dict):
        docs.append(spec)
    else:
        paths += [p for p in _paths(spec, root, "hooks") if p not in paths]
    return docs + [_json(path, "hooks") for path in paths]


def _events(doc: Any) -> Any:
    if isinstance(doc, dict) and "hooks" in doc:
        return doc["hooks"]
    if isinstance(doc, dict):
        return {k: v for k, v in doc.items() if k != "description"}
    return doc


def _target(config: Any) -> str:
    if not isinstance(config, dict):
        return "?"
    if config.get("command"):
        args = config.get("args")
        if not isinstance(args, list):
            args = []
        return _one_line(" ".join([str(config["command"]), *map(str, args)]))
    return _one_line(config.get("url") or "?")


def _configs(
    root: Path, manifest: dict[str, Any], files: tuple[str, ...], key: str
) -> dict[str, Any]:
    """Servers of one kind: from their files at the root and from the manifest."""
    docs = [_json(root / name, name) for name in files if (root / name).is_file()]
    spec = manifest.get(key)
    if isinstance(spec, dict):
        docs.append(spec)
    else:
        docs += [_json(path, key) for path in _paths(spec, root, key)]
    found: dict[str, Any] = {}
    for doc in docs:
        inner = doc.get(key, doc) if isinstance(doc, dict) else None
        if not isinstance(inner, dict):
            raise PluginError(f"{key}: expected an object of servers")
        for name, config in inner.items():
            if len(found) >= MAX_ENTRIES:
                raise PluginError(f"more than {MAX_ENTRIES} {key}")
            found[_one_line(name)] = config
    return found


def _named(value: Any, where: str, found: dict[str, str]) -> None:
    """Servers a front matter names: an object of them, or a list."""
    for item in value if isinstance(value, list) else [value]:
        pairs = item.items() if isinstance(item, dict) else [(item, None)]
        for name, config in pairs:
            if len(found) >= MAX_ENTRIES:
                raise PluginError(f"more than {MAX_ENTRIES} servers")
            found[f"{_one_line(name)} in {where}"] = _target(config) if config else "(named)"


def _front_matter_risks(root: Path) -> tuple[dict[str, list[str]], dict[str, str]]:
    """Hooks and servers declared in the front matter of any markdown file."""
    hooks: dict[str, list[str]] = {}
    servers: dict[str, str] = {}
    for rel, path in _files(root):
        if not rel.lower().endswith(".md"):
            continue
        meta, match = _front(_text(path))
        if match is None:
            continue
        if meta is None:
            if _RISKY.search(match.group(1)):
                raise PluginError(
                    f"{rel}: its front matter names hooks or servers and cannot be read"
                )
            continue
        if "hooks" in meta:
            _collect_hooks(meta["hooks"], hooks, where=rel)
        for key in NEVER_IN_A_VIEW:
            if key in meta:
                _named(meta[key], rel, servers)
    return hooks, servers


BRIDGES = Path("metabot") / "bridges"
BRIDGE_SHAPE = (
    "a bridge has a command (a list of words), and may have models, tools (own or "
    "switchboard) and read_only (honored or no); its file's name is lower-case letters, "
    "digits and '-'"
)


def _a_path(word: str) -> bool:
    """Whether a command word names a file: it has a slash, and is no flag or address."""
    return ("/" in word or "\\" in word) and not word.startswith("-") and "://" not in word


def _bridge_specs(root: Path) -> dict[str, dict[str, Any]]:
    """The bridges a plugin carries, by name, as ``metabot/bridges/<name>.yaml`` says them.

    A path in a command names a file the plugin holds. One outside it, or one
    it does not hold, is refused, so the install hash covers what a bridge runs.
    """
    folder = root / BRIDGES
    if not folder.is_dir():
        return {}
    found: dict[str, dict[str, Any]] = {}
    for path in sorted(p for p in folder.glob("*.y*ml") if p.is_file()):
        rel, name = (BRIDGES / path.name).as_posix(), path.stem
        if name in ENGINES:
            raise PluginError(f"{rel}: {name} is a built-in engine's name")
        try:
            spec = yaml.safe_load(_text(path))
        except (yaml.YAMLError, RecursionError):
            raise PluginError(f"{rel}: it does not read as YAML") from None
        if not bridge_ok(name, spec):
            raise PluginError(f"{rel}: {BRIDGE_SHAPE}")
        for word in filter(_a_path, spec["command"]):
            if not _inside(root, word, rel).is_file():  # an absolute path is outside too
                raise PluginError(f"{rel}: its command names {word}, which the plugin lacks")
        if name in found:
            raise PluginError(f"{rel}: two files name the bridge {name}")
        found[name] = spec
    return found


def bridge_refused(name: str, command: list[str]) -> str | None:
    """Why a plugin's bridge must not run ``command``, as a server must not: it names the copy
    other than by a whole path, or under a home whose path holds what a program can split or
    expand, as a shell behind env reads it again."""
    from metabot.upstream import refused_copy

    refusal = refused_copy(command)
    if refusal is None:
        return None
    home = metabot_home()
    if refusal.odd:
        return (
            f"bridge {name} runs its plugin's copy in {home}/plugins, and the home's path holds "
            f"{refusal.odd}, which a program can split or expand: move the home to a path "
            "without them, then enable it again, or enable it --without-bridges"
        )
    return (
        f"bridge {name} names a plugin's copy where MetaBot cannot tell it is the one in "
        f"{home}/plugins: enable it again, or enable it --without-bridges"
    )


def _resolved(root: Path, spec: dict[str, Any]) -> dict[str, Any]:
    """A bridge's spec with each path in its command made a path in ``root``."""
    words = [str(root / w) if _a_path(w) else w for w in spec["command"]]
    return {**spec, "command": words}


def inspect_plugin(root: Path) -> PluginFacts:
    manifest = _manifest(root)
    hooks: dict[str, list[str]] = {}
    for doc in _hook_docs(root, manifest):
        _collect_hooks(_events(doc), hooks)
    front_hooks, front_servers = _front_matter_risks(root)
    for key, commands in front_hooks.items():
        for command in commands:
            _add(hooks, key, command)
    servers = {n: _target(c) for n, c in _configs(root, manifest, MCP_FILES, "mcpServers").items()}
    servers.update(front_servers)
    lsp = {n: _target(c) for n, c in _configs(root, manifest, LSP_FILES, "lspServers").items()}
    routines = root / "metabot" / "routines"
    return PluginFacts(
        name=manifest["name"],
        version=_one_line(manifest.get("version") or "0"),
        description=_one_line(manifest.get("description")),
        skills=_skills(root, manifest),
        commands=_markdown_names(root, "commands", manifest),
        agents=_markdown_names(root, "agents", manifest),
        hooks={event: tuple(cmds) for event, cmds in sorted(hooks.items())},
        servers=dict(sorted(servers.items())),
        routines=tuple(
            sorted(p.stem for p in routines.glob("*.y*ml") if p.is_file())
            if routines.is_dir()
            else []
        ),
        lsp_servers=dict(sorted(lsp.items())),
        bridges={n: " ".join(spec["command"]) for n, spec in _bridge_specs(root).items()},
    )


def _diff_named(kind: str, old: dict[str, str], new: dict[str, str]) -> list[str]:
    lines = [f"+ {kind} {n}: {new[n]}" for n in sorted(set(new) - set(old))]
    lines += [f"- {kind} {n}" for n in sorted(set(old) - set(new))]
    lines += [
        f"~ {kind} {n}: {old[n]} -> {new[n]}"
        for n in sorted(set(old) & set(new))
        if old[n] != new[n]
    ]
    return lines


def permission_diff(old: PluginFacts, new: PluginFacts) -> list[str]:
    """What an update adds, removes or changes, one line each."""
    lines: list[str] = []
    if old.version != new.version:
        lines.append(f"~ version {old.version} -> {new.version}")
    for kind, before, after in (
        ("skill", set(old.skills), set(new.skills)),
        ("command", set(old.commands), set(new.commands)),
        ("agent", set(old.agents), set(new.agents)),
        ("routine", set(old.routines), set(new.routines)),
    ):
        lines += [f"+ {kind} {x}" for x in sorted(after - before)]
        lines += [f"- {kind} {x}" for x in sorted(before - after)]
    before_hooks = {(e, c) for e, cmds in old.hooks.items() for c in cmds}
    after_hooks = {(e, c) for e, cmds in new.hooks.items() for c in cmds}
    lines += [f"+ hook {e}: {c}" for e, c in sorted(after_hooks - before_hooks)]
    lines += [f"- hook {e}: {c}" for e, c in sorted(before_hooks - after_hooks)]
    lines += _diff_named("server", old.servers, new.servers)
    lines += _diff_named("lsp", old.lsp_servers, new.lsp_servers)
    lines += _diff_named("bridge", old.bridges, new.bridges)
    return lines


# ── state ───────────────────────────────────────────────────────────────────


def _state(name: str) -> dict[str, Any]:
    """A state file's object; PluginError naming the file when it does not read as one.

    Every run reads the plugins it enables from here, so a file that does not
    read stops them, saying which, rather than running with plugins left out.
    """
    path = metabot_home() / name
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, RecursionError) as exc:  # a UnicodeDecodeError is a ValueError
        raise PluginError(f"cannot read {path}: {exc}") from None
    if not isinstance(data, dict):
        raise PluginError(f"cannot read {path}: it holds no object")
    return data


def _store_state(name: str, data: dict[str, Any]) -> None:
    write_private(metabot_home() / name, json.dumps(data, indent=2, sort_keys=True) + "\n")


def installed() -> list[InstalledPlugin]:
    known = {f.name for f in fields(InstalledPlugin)}
    try:
        rows = [
            InstalledPlugin(**{k: v for k, v in raw.items() if k in known})
            for tree, raw in _state(RECORDS).items()
            if TREE.match(tree) and isinstance(raw, dict) and raw.get("tree") == tree
        ]
        return sorted(rows, key=lambda p: (p.seq, p.installed_at, p.tree))
    except TypeError:  # a record without a field it needs, or with one of another type
        path = metabot_home() / RECORDS
        raise PluginError(f"cannot read {path}: a plugin's record is not whole") from None


def _save(plugin: InstalledPlugin) -> None:
    data = _state(RECORDS)
    data[plugin.tree] = asdict(plugin)
    _store_state(RECORDS, data)


def versions(ref: str) -> list[InstalledPlugin]:
    """The versions a ref means: one for a hash prefix, else every version of one plugin."""
    plugins = installed()
    if HEX.match(ref or "") and len(ref) >= MIN_PREFIX:
        matches = [p for p in plugins if p.tree.startswith(ref)]
        if len(matches) == 1:
            return matches
        if len(matches) > 1:
            raise PluginError(f"{ref} starts more than one installed hash; give more of it")
    name, sep, market = (ref or "").partition("@")
    matches = [p for p in plugins if p.name == name and (not sep or p.market == market)]
    if not matches:
        raise PluginError(f"no installed plugin {ref!r}; see metabot plugin list")
    refs = sorted({p.ref for p in matches})
    if len(refs) > 1:
        raise PluginError(f"{ref!r} is more than one plugin: {', '.join(refs)}; say which")
    return matches


def resolve(ref: str) -> InstalledPlugin:
    """One installed plugin: by hash prefix, else the newest version of a name."""
    return versions(ref)[-1]


def _check_name(name: str, what: str) -> str:
    if not isinstance(name, str) or not NAME.match(name):
        raise PluginError(f"a {what} name is lower-case letters, digits, '.', '-' or '_'")
    return name


# ── marketplaces and install ────────────────────────────────────────────────


def _market_doc(root: Path) -> dict[str, Any]:
    path = root / MARKET_FILE
    if not path.is_file():
        raise PluginError(f"{root} has no {MARKET_FILE.as_posix()}")
    data = _json(path, "marketplace.json")
    if not isinstance(data, dict) or not isinstance(data.get("plugins"), list):
        raise PluginError("marketplace.json: expected an object with a plugins list")
    return data


def builtin_marketplace() -> Path:
    return Path(__file__).resolve().parent / "marketplace"


def add_marketplace(name: str, path: Path) -> Path:
    _check_name(name, "marketplace")
    if name == LOCAL:
        raise PluginError(f"{LOCAL} is kept for skills imported from this machine")
    if name == BUILTIN:
        raise PluginError(f"{BUILTIN} is the marketplace that ships with MetaBot")
    root = Path(path).expanduser().resolve()
    _market_doc(root)
    data = _state(MARKETS)
    data[name] = {"path": str(root), "added_at": utc_now()}
    _store_state(MARKETS, data)
    return root


def remove_marketplace(name: str) -> None:
    if name == BUILTIN:
        raise PluginError(f"{BUILTIN} ships with MetaBot and stays listed")
    data = _state(MARKETS)
    if data.pop(name, None) is None:
        raise PluginError(f"no marketplace named {name!r}")
    _store_state(MARKETS, data)


def marketplaces() -> dict[str, Path]:
    """Every marketplace: the one that ships with MetaBot, then those added here."""
    added = {
        name: Path(raw["path"])
        for name, raw in sorted(_state(MARKETS).items())
        if name != BUILTIN and isinstance(raw, dict) and isinstance(raw.get("path"), str)
    }
    return {BUILTIN: builtin_marketplace(), **added}


def market_plugins(root: Path) -> list[tuple[str, str]]:
    """``(name, description)`` of every plugin a marketplace lists."""
    return [
        (_one_line(entry["name"]), _one_line(entry.get("description")))
        for entry in _market_doc(root)["plugins"]
        if isinstance(entry, dict) and isinstance(entry.get("name"), str)
    ]


def _entry_dir(root: Path, doc: dict[str, Any], entry: dict[str, Any]) -> Path:
    name = entry.get("name")
    source = entry.get("source")
    remote = isinstance(source, str) and ("://" in source or source.startswith("git@"))
    if isinstance(source, dict) or remote:
        kind = source.get("source", "a remote source") if isinstance(source, dict) else source
        raise PluginError(
            f"{name} comes from {_one_line(kind)}; fetching it needs the network, and this "
            "version installs from folders on this machine"
        )
    if not isinstance(source, str) or not source:
        raise PluginError(f"{name} has no source")
    meta = doc.get("metadata")
    base = root.resolve()
    if isinstance(meta, dict) and isinstance(meta.get("pluginRoot"), str) and meta["pluginRoot"]:
        base = _inside(root, meta["pluginRoot"], "pluginRoot", "the marketplace")
    src = (base / source).resolve()
    if not src.is_relative_to(root.resolve()):
        raise PluginError(f"{name}'s source is outside the marketplace: {source}")
    if not any((src / m).is_file() for m in (CLAUDE_MANIFEST, CURSOR_MANIFEST)):
        raise PluginError(f"{name}: no plugin at {source}")
    return src


def install(ref: str, confirm: Confirm) -> InstalledPlugin | None:
    """Install ``name@market`` once ``confirm`` agrees; None when it does not."""
    name, sep, market = ref.partition("@")
    if not sep or not name or not market:
        raise PluginError("install takes NAME@MARKETPLACE")
    root = marketplaces().get(market)
    if root is None:
        raise PluginError(f"no marketplace named {market!r}; add it with plugin marketplace add")
    doc = _market_doc(root)
    entries = [e for e in doc["plugins"] if isinstance(e, dict) and e.get("name") == name]
    if not entries:
        raise PluginError(f"{market} lists no plugin named {name!r}")
    if len(entries) > 1:
        raise PluginError(f"{market} lists {name} more than once")
    return install_dir(_entry_dir(root, doc, entries[0]), name, market, confirm)


def install_dir(
    src: Path, name: str, market: str, confirm: Confirm, *, source: str | None = None
) -> InstalledPlugin | None:
    _check_name(name, "plugin")
    folder = private_dir(metabot_home() / "plugins")
    staging = folder / f".staging-{secrets.token_hex(6)}"
    try:
        _copy_files(Path(src), staging)
        facts = inspect_plugin(staging)
        if facts.name != name:
            raise PluginError(
                f"the marketplace lists {name}, but the plugin calls itself {facts.name}"
            )
        tree = tree_sha256(staging)
        records = installed()
        same = next((p for p in records if p.tree == tree), None)
        if same is not None:
            return same
        earlier = [p for p in records if p.ref == f"{name}@{market}" and p.root.is_dir()]
        pool = [p for p in earlier if p.enabled] or earlier
        base = pool[-1] if pool else None
        diff = permission_diff(inspect_plugin(base.root), facts) if base else None
        if not confirm(facts, diff):
            return None
        plugin = InstalledPlugin(tree, name, market, facts.version, source or str(src), utc_now())
        plugin.seq = max((p.seq for p in records), default=0) + 1
        if plugin.root.exists():  # left by an install that never finished
            shutil.rmtree(plugin.root)
        os.replace(staging, plugin.root)
        _save(plugin)
        return plugin
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def import_skills(src_dir: Path, name: str, confirm: Confirm) -> InstalledPlugin | None:
    """Make a plugin of a folder of skills, such as the BMAD skills, and install it."""
    _check_name(name, "plugin")
    src = Path(src_dir).expanduser().resolve()
    if not src.is_dir():
        raise PluginError(f"{src} is not a folder")
    skills = [d for d in sorted(src.iterdir()) if d.is_dir() and (d / "SKILL.md").is_file()]
    if not skills:
        raise PluginError(f"{src} holds no skill folders (a folder with a SKILL.md)")
    build = private_dir(metabot_home() / "local-plugins") / f".{name}-{secrets.token_hex(6)}"
    try:
        for skill in skills:
            _copy_files(skill.resolve(), build / "skills" / skill.name)
        manifest = {"name": name, "version": "0.0.0-local", "description": f"skills from {src}"}
        write_private(build / CLAUDE_MANIFEST, json.dumps(manifest, indent=2) + "\n")
        return install_dir(build, name, LOCAL, confirm, source=str(src))
    finally:
        shutil.rmtree(build, ignore_errors=True)


# ── enable, disable, remove ─────────────────────────────────────────────────


def _connection_spec(root: Path, server: str, config: Any) -> dict[str, Any]:
    if not isinstance(config, dict):
        raise PluginError(f"server {server}: expected an object")
    kind = config.get("type")
    args = config.get("args", [])
    if config.get("command"):
        words = [str(w).replace(PLUGIN_ROOT_VAR, str(root)) for w in [config["command"], *args]]
        spec: dict[str, Any] = {"transport": "stdio", "command": words}
        text = " ".join(words)
    else:
        spec = {"transport": "http", "url": str(config.get("url") or "")}
        text = spec["url"]
    needs = (
        "environment values" if config.get("env") or "${" in text
        else "headers" if config.get("headers")
        else f"the {kind} transport" if kind not in (None, "stdio", "http", "streamable-http")
        else "args as a list" if not isinstance(args, list)
        else None
    )  # fmt: skip
    if needs:
        raise PluginError(
            f"server {server} needs {needs}; add it yourself with metabot connect add, "
            "which keeps any token out of every engine"
        )
    try:
        check_spec(spec["transport"], command=spec.get("command"), url=spec.get("url"))
    except ValueError as exc:
        raise PluginError(f"server {server}: {exc}") from None
    from metabot.upstream import refused_copy

    # a connection that never starts, and enabling again writes it so: each cause and its mend
    refusal = refused_copy(spec["command"]) if spec["transport"] == "stdio" else None
    if refusal is not None:
        mends = []
        if refusal.odd:
            mends.append(
                f"the home's path holds {refusal.odd}, which a program can split or expand: "
                "move the home to a path without them"
            )
        if refusal.misnamed:
            mends.append(
                "add it yourself with metabot connect add, the copy's path whole, by no blank, "
                "quote or operator: a word of its own, after the last '=', ':' or ',' in one, or "
                "a word of a line for sh -c with no '#', '<<', '(', '{' or '`'"
            )
        where = (
            "a plugin's copy where MetaBot cannot tell it is the one in"
            if refusal.misnamed
            else "its copy in"
        )
        raise PluginError(
            f"server {server} names {where} {metabot_home()}/plugins, so MetaBot would not "
            f"start it; {', then '.join(mends)}"
        )
    return spec


def _connection_name(plugin: str, server: str) -> str:
    name = re.sub(r"[^a-z0-9-]+", "-", f"{plugin}-{server}".lower()).strip("-")[:32].rstrip("-")
    if not CONNECTION_NAME.match(name):
        raise PluginError(f"server {server} has no name a connection can take")
    if name in RESERVED_NAMES:
        raise PluginError(f"server {server} would be the connection {name}, which MetaBot keeps")
    return name


def _add_connection(
    plugin: InstalledPlugin, store: ConnectionStore, name: str, spec: dict[str, Any]
) -> None:
    """One of its connections, listed in its record before it is added, so a retry, a
    disable or a remove takes it whichever write fails: one the store never held is passed
    over."""
    conn_id = new_connection_id()
    plugin.connections.append(conn_id)
    _save(plugin)
    store.add(name=name, conn_id=conn_id, **spec)


def _set_connections(
    plugin: InstalledPlugin, store: ConnectionStore, specs: dict[str, dict[str, Any]]
) -> None:
    """Its servers as connections. One it holds by a server's name is written again in place,
    keeping its id, its scope settings and its state, and so the routines that name it; one it
    holds by no server's name goes; a server it holds none for is added."""
    held = {c.name: c for c in store.list() if c.id in plugin.connections}
    for name, conn in held.items():
        if name not in specs:
            store.remove(conn.id)
    plugin.connections = [c.id for name, c in held.items() if name in specs]
    _save(plugin)
    for name, spec in specs.items():
        if name in held:
            store.respec(held[name].id, **spec)
        else:
            _add_connection(plugin, store, name, spec)


def _take_connections(
    plugin: InstalledPlugin,
    family: list[InstalledPlugin],
    store: ConnectionStore,
    servers: dict[str, str],
) -> None:
    """The connections another version holds for a server this one serves, under the same
    connection name and the same server, become this one's, listed here before they leave its
    record, so turning that version off keeps them, and each is written again in place: its
    id, its scope settings and its state stay. A connection name is cut at 32 characters, so
    another server can share it; its connection goes with that version."""
    names = {c.id: c.name for c in store.list()}
    for other in family:
        if other.tree == plugin.tree or not other.connections:
            continue
        theirs = _servers(other) or {}  # a copy that cannot be read hands nothing on
        taken = [
            conn_id
            for conn_id in other.connections
            if (name := names.get(conn_id)) in servers and theirs.get(name) == servers[name]
        ]
        if not taken:
            continue
        plugin.connections += [conn_id for conn_id in taken if conn_id not in plugin.connections]
        _save(plugin)
        other.connections = [conn_id for conn_id in other.connections if conn_id not in taken]
        _save(other)


def _servers(plugin: InstalledPlugin) -> dict[str, str] | None:
    """Each connection name ``plugin``'s servers get, with the server it is for; None when
    its copy cannot be read."""
    try:
        configs = _configs(plugin.root, _manifest(plugin.root), MCP_FILES, "mcpServers")
        return {_connection_name(plugin.name, server): server for server in configs}
    except (PluginError, OSError, ValueError, RecursionError):
        return None


def _drop_connections(plugin: InstalledPlugin, store: ConnectionStore) -> None:
    for conn_id in plugin.connections:
        with contextlib.suppress(KeyError):
            store.remove(conn_id)
    plugin.connections = []


def _strip_front(path: Path, keys: frozenset[str]) -> None:
    """Rewrite a markdown file's front matter without ``keys``; the body stays as it was."""
    text = _text(path)
    meta, match = _front(text)
    if match is None:
        return
    if meta is None:
        if _RISKY.search(match.group(1)):
            raise PluginError(
                f"{path.name}: its front matter names hooks or servers and cannot be read"
            )
        return
    if not keys & set(meta):
        return
    kept = {k: v for k, v in meta.items() if k not in keys}
    head = (
        yaml.safe_dump(kept, sort_keys=False, allow_unicode=True, width=1_000_000) if kept else ""
    )
    path.write_bytes(
        f"---\n{head}---{text[match.end() :]}".encode("utf-8", errors="surrogateescape")
    )


def _build_view(plugin: InstalledPlugin, keep_hooks: bool) -> Path:
    """The copy the engines get, built aside and then swapped in."""
    root = plugin.root
    manifest = _manifest(root)
    keys = frozenset({*NEVER_IN_A_VIEW, *(() if keep_hooks else ("hooks",))})
    named = {p for key in keys for p in _paths(manifest.get(key), root, key)}

    def skip(rel: str) -> bool:
        top = rel.split("/", 1)[0]
        if rel in LEFT_OUT or top == ".cursor-plugin" or (top == "hooks" and not keep_hooks):
            return True
        return (root / rel).resolve() in named

    parent = private_dir(metabot_home() / "plugin-views")
    building = parent / f".building-{secrets.token_hex(6)}"
    old = parent / f".old-{secrets.token_hex(6)}"
    try:
        _copy_files(root, building, skip)
        for rel, path in _files(building):
            if rel.lower().endswith(".md"):
                _strip_front(path, keys)
        text = json.dumps({k: v for k, v in manifest.items() if k not in keys}, indent=2) + "\n"
        for target in (building / CLAUDE_MANIFEST, building / CURSOR_MANIFEST):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        if plugin.view.exists():
            os.replace(plugin.view, old)
        os.replace(building, plugin.view)
    finally:
        for leftover in (building, old):
            _delete(leftover)
    return plugin.view


def verify_installed(plugin: InstalledPlugin) -> None:
    """The installed copy must still be the one its hash names; a bridge runs from it. Its
    view must still be the one enable built: a plugin a disable left without one is not on."""
    _verify(plugin)
    _verify_view(plugin)


def _verify(plugin: InstalledPlugin) -> None:
    try:
        same = plugin.root.is_dir() and tree_sha256(plugin.root) == plugin.tree
    except (OSError, PluginError) as exc:  # a file or folder in it that cannot be read
        why = (exc.strerror or type(exc).__name__) if isinstance(exc, OSError) else str(exc)
        raise PluginError(
            f"cannot read {plugin.ref}'s installed copy ({why}); remove it and install it again"
        ) from None
    if not same:
        raise PluginError(
            f"{plugin.ref}'s installed copy changed since it was installed; "
            "remove it and install it again"
        )


def _verify_view(plugin: InstalledPlugin) -> None:
    """The copy a run is given must still be the one enable built."""
    changed = f"{plugin.ref}'s view changed since it was enabled; disable it and enable it again"
    try:
        digest = tree_sha256(plugin.view)
    except (PluginError, OSError):
        raise PluginError(changed) from None
    if digest != plugin.view_tree:
        raise PluginError(changed)


def _bridges_taken(family: list[InstalledPlugin]) -> dict[str, str]:
    """Bridge names in use outside this plugin's versions, each with where it comes from."""
    from metabot.config import ConfigError, load_config

    try:
        table = load_config().bridges
    except ConfigError:
        table = {}
    taken = {name: "config.json" for name in table} if isinstance(table, dict) else {}
    own = {p.tree for p in family}
    for other in installed():
        if other.enabled and other.tree not in own:
            taken.update({name: other.ref for name in other.bridges})
    return taken


def enabled_bridges() -> dict[str, tuple[InstalledPlugin, dict[str, Any]]]:
    """The bridges of enabled plugins that the person allowed, by name, from the records.

    No file is hashed here; a bridge's plugin is checked when the bridge runs.
    """
    found: dict[str, tuple[InstalledPlugin, dict[str, Any]]] = {}
    for plugin in installed():
        if not plugin.enabled or not isinstance(plugin.bridges, dict):
            continue
        for name, spec in plugin.bridges.items():
            if bridge_ok(name, spec):
                found.setdefault(name, (plugin, spec))
    return found


def enable(
    ref: str,
    *,
    allow_hooks: bool = False,
    without_hooks: bool = False,
    add_servers: bool = False,
    allow_bridges: bool = False,
    without_bridges: bool = False,
) -> InstalledPlugin:
    """Turn a plugin on: its newest version, unless a hash says which. Others go off.

    ``add_servers`` makes its MCP servers connections, behind the gate. One it
    already holds, in this version or another, is written again in place, so its
    id, its scope settings and its state stay; they go when the plugin goes off.
    ``allow_bridges`` makes its bridges engines, run from the installed copy.
    """
    if allow_hooks and without_hooks:
        raise PluginError("choose one of --allow-hooks and --without-hooks")
    if allow_bridges and without_bridges:
        raise PluginError("choose one of --allow-bridges and --without-bridges")
    plugin = resolve(ref)
    _verify(plugin)
    facts = inspect_plugin(plugin.root)
    if facts.hooks and not (allow_hooks or without_hooks):
        listed = "; ".join(f"{event}: {cmd}" for event, cmds in facts.hooks.items() for cmd in cmds)
        raise PluginError(
            f"{plugin.ref} has hooks, which run outside MetaBot's gate: {listed}. "
            "Enable it with --allow-hooks to run them, or --without-hooks to leave them out"
        )
    if facts.bridges and not (allow_bridges or without_bridges):
        listed = "; ".join(f"{name}: {cmd}" for name, cmd in facts.bridges.items())
        raise PluginError(
            f"{plugin.ref} has bridges, programs that run as engines, outside MetaBot's gate: "
            f"{listed}. Enable it with --allow-bridges to make them engines, or "
            "--without-bridges to leave them out"
        )
    everyone = installed()
    family = [p for p in everyone if p.ref == plugin.ref]
    bridges: dict[str, dict[str, Any]] = {}
    if allow_bridges and facts.bridges:
        taken = _bridges_taken(family)
        for name in facts.bridges:
            if name in taken:
                raise PluginError(
                    f"a bridge named {name} is already an engine here, from {taken[name]}; "
                    "remove that one first, or enable this plugin --without-bridges"
                )
        bridges = {n: _resolved(plugin.root, s) for n, s in _bridge_specs(plugin.root).items()}
        for name, spec in bridges.items():
            said = bridge_refused(name, spec["command"])
            if said is not None:
                raise PluginError(said)
    namesake = next(
        (p for p in everyone if p.name == plugin.name and p.ref != plugin.ref and p.enabled), None
    )
    if namesake is not None:
        raise PluginError(
            f"{namesake.ref} is enabled, and two enabled plugins cannot share the name "
            f"{plugin.name}; disable it first"
        )
    store = ConnectionStore()
    specs: dict[str, dict[str, Any]] = {}
    servers: dict[str, str] = {}
    if add_servers:
        configs = _configs(plugin.root, _manifest(plugin.root), MCP_FILES, "mcpServers")
        servers = {_connection_name(plugin.name, server): server for server in configs}
        specs = {
            name: _connection_spec(plugin.root, server, configs[server])
            for name, server in servers.items()
        }
        if len(specs) != len(configs):
            raise PluginError("two of its servers would get one connection name")
        conns = store.list()  # one that does not read refuses here, while all is as it was
        own = {conn_id for p in family for conn_id in p.connections}
        for name in specs:
            if any(c.name == name and c.id not in own for c in conns):
                raise PluginError(
                    f"a connection named {name} already exists, and keeps its name when "
                    "revoked, so none of its servers is added: enable it without "
                    "--add-servers, then add its servers with metabot connect add, this one "
                    f"under another name (metabot plugin show {plugin.tree[:12]} lists them)"
                )
    keep_hooks = allow_hooks and bool(facts.hooks)
    _build_view(plugin, keep_hooks)
    view_tree = tree_sha256(plugin.view)  # in the record only with the rest enable decides
    again = f"{_ref_and_version(plugin)} is not on: enable it again"
    try:
        if add_servers:  # before another version goes off, which drops the connections it holds
            _take_connections(plugin, family, store, servers)
        for other in family:
            if other.tree != plugin.tree and (other.enabled or other.connections):
                _turn_off(other, store, again)
        if add_servers:
            _set_connections(plugin, store, specs)
        plugin.enabled = True
        plugin.hooks_allowed = keep_hooks
        plugin.bridges = bridges
        plugin.view_tree = view_tree
        _save(plugin)
    except OSError as exc:
        raise PluginError(
            f"{_ref_and_version(plugin)}: the enable did not finish "
            f"({exc.strerror or type(exc).__name__}); enable it again"
        ) from None
    return plugin


def enable_line(plugin: InstalledPlugin) -> str:
    """The command that enables this version again with its servers, its hooks and its bridges
    run or left out as they are: enable takes no plugin that has them without saying which."""
    line = f"metabot plugin enable {plugin.tree[:12]} --add-servers"
    try:
        facts = inspect_plugin(plugin.root)
    except (PluginError, OSError, ValueError, RecursionError):
        return line  # which then says what is wrong
    if facts.hooks:
        line += " --allow-hooks" if plugin.hooks_allowed else " --without-hooks"
    if facts.bridges:
        line += " --allow-bridges" if plugin.bridges else " --without-bridges"
    return line


def _delete(tree: Path) -> None:
    """Delete a copy MetaBot keeps, also one with a folder that lost the bits it is emptied by.

    Each folder in it gets its owner's bits back, and the copy is deleted again. A copy that
    still cannot be deleted is a refusal that names it.
    """
    if not tree.exists():
        return
    try:
        shutil.rmtree(tree)
    except OSError:
        try:
            _open_up(tree)
            shutil.rmtree(tree)
        except OSError as exc:
            raise PluginError(
                f"cannot delete {tree} ({exc.strerror or type(exc).__name__}); "
                "delete it yourself, then try again"
            ) from None


def _open_up(tree: Path) -> None:
    """Give the owner of each folder in ``tree`` the bits to list and empty it; links stay."""
    folders = [str(tree)]
    while folders:
        folder = folders.pop()
        if os.path.islink(folder) or not os.path.isdir(folder):
            continue
        os.chmod(folder, stat.S_IMODE(os.lstat(folder).st_mode) | stat.S_IRWXU)
        with os.scandir(folder) as entries:
            folders += [e.path for e in entries if e.is_dir(follow_symlinks=False)]


def _ref_and_version(plugin: InstalledPlugin) -> str:
    """The plugin and its version, in a message: a long version never hides the rest."""
    return f"{plugin.ref} {printable(plugin.version, 64)}"


def _turn_off(plugin: InstalledPlugin, store: ConnectionStore, again: str) -> None:
    """One version off. ``again``: what finishes it, said when a step of it fails."""
    if plugin.connections:  # a store that does not read refuses while the plugin is whole
        store.list()
    _delete(plugin.view)  # a view that stays keeps the plugin on, with its connections
    plugin.enabled = False
    plugin.hooks_allowed = False
    plugin.bridges = {}
    plugin.view_tree = ""
    named = _ref_and_version(plugin)
    try:  # off before its connections go: those that cannot stay listed, for a retry
        _save(plugin)
    except OSError as exc:  # its view is gone: a run refuses it until it is off
        raise PluginError(
            f"{named}'s view is deleted, but it could not be recorded as off "
            f"({exc.strerror or type(exc).__name__}); {again}"
        ) from None
    try:
        _drop_connections(plugin, store)
    except OSError as exc:
        raise PluginError(
            f"{named} is off, and its connections could not be removed "
            f"({exc.strerror or type(exc).__name__}); {again}"
        ) from None
    _save(plugin)


def disable(ref: str, again: str = "disable it again") -> list[InstalledPlugin]:
    """Turn off what a ref means: every version of a name, or the one a hash names."""
    store = ConnectionStore()
    plugins = versions(ref)
    for plugin in plugins:
        _turn_off(plugin, store, again)
    return plugins


def remove(ref: str) -> list[InstalledPlugin]:
    """Delete what a ref means, with its views and the connections it added."""
    plugins = disable(ref, "no copy was removed: remove it again")
    for plugin in plugins:
        _delete(plugin.root)
        data = _state(RECORDS)  # its record goes with its copy, so a refusal keeps the rest true
        data.pop(plugin.tree, None)
        _store_state(RECORDS, data)
    return plugins


def plugin_skills(dirs: list[Path]) -> dict[str, Path]:
    """``plugin:skill`` to its SKILL.md, for the engines that cannot load a plugin."""
    found: dict[str, Path] = {}
    for root in dirs:
        manifest = _manifest(root)
        for md in _skill_files(root, manifest):
            meta = _front(_text(md))[0] or {}
            found[f"{manifest['name']}:{_one_line(meta.get('name') or md.parent.name)}"] = md
    return dict(sorted(found.items()))


def skill_description(path: Path) -> str:
    return _one_line((_front(_text(path))[0] or {}).get("description"))


def enabled_plugins() -> list[InstalledPlugin]:
    """Enabled plugins whose install and view still match the hashes they were pinned with."""
    chosen = [p for p in installed() if p.enabled]  # one with no view refuses: it is not off
    for plugin in chosen:
        _verify(plugin)
        _verify_view(plugin)
    return chosen


def enabled_plugin_dirs() -> list[Path]:
    """The views the engines get, one per enabled plugin."""
    return [p.view for p in enabled_plugins()]
