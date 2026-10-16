"""A desk's kit: Code Desks' skills for one desk, given to its run as a plugin.

A kit is a list of skill names in ``office.yaml``. MetaBot looks for each one
where Code Desks does, in the same order: the office's ``.office/skills``,
then ``.claude/skills`` and ``.agents/skills`` beside the office file, then
the person's ``~/.claude/skills`` and ``~/.agents/skills``. The first folder
of that name is the skill.

Code Desks links a kit into the desk's worktree. MetaBot copies it into one
plugin folder per desk, ``<METABOT_HOME>/kits/<office run>/<desk>``, and
gives that folder to the desk's run the way it gives an enabled plugin: an
engine that loads plugins gets the folder, and any other loads the skills
through the switchboard's two read tools. Nothing is written into the
worktree, so a kit never reaches a desk's commit. The copies go when the
office run ends.

The office file may come from a repository the person cloned, so:

- a kit entry is a plain folder name, never a path;
- a skill found in the project must resolve inside the project, so a link
  committed there cannot hand a desk a folder from the person's home. A
  skill in the person's own folders may be a link, as installers make them;
- a skill holds plain files: a link or a special file in it is refused, as
  in a plugin, and a kit is at most ``KIT_BYTES`` and ``KIT_FILES``;
- a skill's front matter loses its hooks and servers, which would run
  outside the gate. A plugin keeps its hooks only when the person allows
  them, and nobody is asked about a kit.

A kit that cannot load refuses the office run before any desk starts.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
from pathlib import Path

from metabot.office import DeskSpec, OfficeSpec
from metabot.paths import metabot_home, private_dir, safe_id

# The front matter a plugin's copy for the engines is stripped of, from the same code.
from metabot.plugins import CLAUDE_MANIFEST, CURSOR_MANIFEST, NEVER_IN_A_VIEW, PluginError
from metabot.plugins import _strip_front as strip_front

PROJECT_DIRS = (".office/skills", ".claude/skills", ".agents/skills")
HOME_DIRS = (".claude/skills", ".agents/skills")
KIT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
KIT_BYTES = 8 * 1024 * 1024
KIT_FILES = 1000
PLUGIN_NAME = "kit"
NEVER_IN_A_KIT = frozenset({*NEVER_IN_A_VIEW, "hooks"})
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class KitError(ValueError):
    """A kit that cannot load, so its desk cannot run."""


def kits_root(office_run_id: str) -> Path:
    return metabot_home() / "kits" / safe_id(office_run_id, "office run id")


def sources(office: OfficeSpec) -> list[tuple[Path, bool]]:
    """Where a skill is looked for, in order, and whether each place is the project's."""
    home = Path.home()
    return [(office.root / d, True) for d in PROJECT_DIRS] + [(home / d, False) for d in HOME_DIRS]


def find_skill(office: OfficeSpec, name: str) -> Path:
    """The resolved folder of the skill ``name``; KitError when there is none to use."""
    if not KIT_NAME.fullmatch(name):
        raise KitError(f"{name!r} is not a skill name")
    places = sources(office)
    for base, in_project in places:
        candidate = base / name
        if not candidate.is_dir():
            continue
        found = candidate.resolve()
        if in_project and not found.is_relative_to(office.top.resolve()):
            raise KitError(f"skill {name!r} in {base} leads outside the project")
        if not (found / "SKILL.md").is_file():
            raise KitError(f"skill {name!r} in {base} has no SKILL.md")
        return found
    raise KitError(f"skill {name!r} is not in " + ", ".join(str(base) for base, _ in places))


def kit_problem(office: OfficeSpec, desk: DeskSpec) -> str | None:
    """Why the desk's kit cannot load, from its first skill that is not found; None if each is.

    The files are measured and copied only when a run takes the kit.
    """
    for name in desk.kit:
        try:
            find_skill(office, name)
        except KitError as exc:
            return f"kit: {exc}"
    return None


def _plain_files(name: str, src: Path) -> list[tuple[str, Path]]:
    """Every file of a skill, by its path inside it; a link or special file is refused."""
    found: list[tuple[str, Path]] = []
    for dirpath, dirnames, filenames in os.walk(src, followlinks=False):
        here = Path(dirpath)
        if here == src and ".git" in dirnames:
            dirnames.remove(".git")  # a checkout's history is not the skill
        for entry in [*dirnames, *filenames]:
            path = here / entry
            rel = path.relative_to(src).as_posix()
            if _CONTROL.search(rel):
                raise KitError(f"skill {name!r}: a control character in a file name")
            if path.is_symlink():
                raise KitError(f"skill {name!r} holds a link, {rel!r}; a kit holds plain files")
            if entry in filenames and not stat.S_ISREG(path.lstat().st_mode):
                raise KitError(f"skill {name!r} holds {rel!r}, which is not a plain file")
        found += [((here / f).relative_to(src).as_posix(), here / f) for f in filenames]
        if len(found) > KIT_FILES:  # stop early: no kit holds more than this
            raise KitError(f"skill {name!r} holds more than {KIT_FILES} files")
    return sorted(found)


def _read_plain(path: Path) -> bytes:
    """A kit file's bytes, read through no link: one put there since the walk is refused."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise KitError(f"{path}: {exc.strerror or exc}; a kit copies plain files only") from None
    with os.fdopen(fd, "rb") as fh:
        if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
            raise KitError(f"{path} is not a plain file; a kit copies plain files only")
        data = fh.read(KIT_BYTES + 1)
    if len(data) > KIT_BYTES:
        raise KitError(f"{path} is larger than {KIT_BYTES} bytes")
    return data


def build_kit(office: OfficeSpec, desk: DeskSpec, dest: Path) -> list[str]:
    """Copy ``desk``'s kit into ``dest`` as a plugin; its skill names, each once, in kit order.

    Every skill is found and measured before anything is copied, and a kit
    that cannot load leaves no ``dest``.
    """
    names = list(dict.fromkeys(desk.kit))
    files = [
        (name, rel, path)
        for name, src in [(name, find_skill(office, name)) for name in names]
        for rel, path in _plain_files(name, src)
    ]
    if len(files) > KIT_FILES:
        raise KitError(f"the {desk.name} desk's kit holds more than {KIT_FILES} files")
    if sum(path.lstat().st_size for _, _, path in files) > KIT_BYTES:
        raise KitError(f"the {desk.name} desk's kit is larger than {KIT_BYTES} bytes")
    private_dir(dest.parent)
    manifest = {"name": PLUGIN_NAME, "description": f"The {desk.name} desk's kit"}
    try:
        for where in (CLAUDE_MANIFEST, CURSOR_MANIFEST):
            (dest / where).parent.mkdir(parents=True, exist_ok=True)
            (dest / where).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        for name, rel, path in files:
            target = dest / "skills" / name / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(_read_plain(path))
            if rel.lower().endswith(".md"):
                try:
                    strip_front(target, NEVER_IN_A_KIT)
                except PluginError as exc:
                    raise KitError(f"skill {name!r}: {exc}") from None
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    return names
