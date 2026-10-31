"""A desk's kit: Code Desks' skills, found where it looks, given to the desk's run as a plugin."""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

import pytest

from conftest import private_mode
from metabot import kits
from metabot.cli import main
from metabot.events import Final, Started
from metabot.office import OfficeError, load_office
from metabot.office_run import office_journal_path
from metabot.plugins import plugin_skills
from metabot.switchboard import SKILLS_LOAD
from test_office_run import DESKS, _relay, _repo

SKILL = "---\nname: {name}\ndescription: {said}\n---\nDo the {said} thing.\n"


@pytest.fixture
def person(tmp_path, monkeypatch):
    """The person's home, where Code Desks looks last."""
    folder = tmp_path / "person"
    folder.mkdir()
    monkeypatch.setenv("HOME", str(folder))
    return folder


def _skill(base: Path, name: str, said: str = "plan", **files: str) -> Path:
    folder = base / name
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(SKILL.format(name=name, said=said), encoding="utf-8")
    for rel, text in files.items():
        (folder / rel).write_text(text, encoding="utf-8")
    return folder


def _office(tmp_path: Path, kit=("plan",)) -> Path:
    """The test office, with a kit on its build desk."""
    return _repo(tmp_path, {**DESKS, "build": {**DESKS["build"], "kit": list(kit)}})


def _build_kit(root: Path, dest: Path) -> list[str]:
    office = load_office(root)
    return kits.build_kit(office, office.desks["build"], dest)


# ── finding a skill ─────────────────────────────────────────────────────────


def test_a_skill_is_found_where_code_desks_looks_in_its_order(tmp_path, person):
    root = _office(tmp_path)
    places = [
        root / ".office" / "skills",
        root / ".claude" / "skills",
        root / ".agents" / "skills",
        person / ".claude" / "skills",
        person / ".agents" / "skills",
    ]
    for n, base in enumerate(places):
        _skill(base, "plan", said=f"place {n}")
    office = load_office(root)
    for base in places:
        assert kits.find_skill(office, "plan") == (base / "plan").resolve()
        shutil.rmtree(base / "plan")
    with pytest.raises(kits.KitError) as err:
        kits.find_skill(office, "plan")
    assert "'plan' is not in" in str(err.value)
    assert all(str(base) in str(err.value) for base in places)


def test_office_check_names_a_kit_skill_it_cannot_find(tmp_path, person, capsys):
    root = _office(tmp_path, kit=("plan", "nope"))
    _skill(root / ".claude" / "skills", "plan")
    assert main(["office", "check", "--dir", str(root)]) == 0
    out = capsys.readouterr().out
    assert "cannot run here" in out
    [line] = [x for x in out.splitlines() if x.strip().startswith("! build:")]
    assert "kit: skill 'nope' is not in" in line
    _skill(person / ".agents" / "skills", "nope")
    assert main(["office", "check", "--dir", str(root)]) == 0
    assert "cannot run here" not in capsys.readouterr().out


def test_office_check_names_a_kit_place_it_cannot_enter(tmp_path, person, capsys, path_checks):
    root = _office(tmp_path)
    _skill(root / ".office" / "skills", "plan")
    shut = root / ".office"
    shut.chmod(0o600)
    try:
        if os.access(shut, os.X_OK):
            pytest.skip("this user enters any folder")
        code = main(["office", "check", "--dir", str(root)])
    finally:
        shut.chmod(0o755)
    out = capsys.readouterr().out
    [line] = [x for x in out.splitlines() if x.strip().startswith("! build:")]
    assert code == 0
    assert f"kit: skill 'plan' in {shut / 'skills'} cannot be read: Permission denied" in line


@pytest.mark.parametrize("name", ["../plan", "a/b", ".plan", "..", "", "p" * 65, "plan\n"])
def test_a_kit_entry_is_a_plain_skill_name_never_a_path(tmp_path, person, name):
    _skill(person / ".claude" / "skills", "plan")
    with pytest.raises(kits.KitError, match="is not a skill name"):
        kits.find_skill(load_office(_office(tmp_path)), name)


def test_a_project_skill_that_leads_outside_the_project_is_refused(tmp_path, person):
    root = _office(tmp_path)
    private = _skill(person / "private", "plan")  # a folder in the person's home
    (root / ".office" / "skills").mkdir(parents=True)
    (root / ".office" / "skills" / "plan").symlink_to(private)  # as a cloned repo may hold
    with pytest.raises(kits.KitError, match="outside the project"):
        kits.find_skill(load_office(root), "plan")


def test_a_project_skills_folder_that_leads_outside_the_project_is_refused(tmp_path, person):
    root = _office(tmp_path)
    _skill(person / "private", "plan")
    (root / ".office").mkdir()
    (root / ".office" / "skills").symlink_to(person / "private")
    with pytest.raises(kits.KitError, match="outside the project"):
        kits.find_skill(load_office(root), "plan")


def test_a_skill_in_the_persons_home_may_be_a_link(tmp_path, person):
    target = _skill(tmp_path / "elsewhere", "plan")
    (person / ".claude" / "skills").mkdir(parents=True)
    (person / ".claude" / "skills" / "plan").symlink_to(target)
    assert kits.find_skill(load_office(_office(tmp_path)), "plan") == target.resolve()


def test_a_folder_with_no_skill_md_is_not_a_skill(tmp_path, person):
    root = _office(tmp_path)
    (root / ".office" / "skills" / "plan").mkdir(parents=True)
    with pytest.raises(kits.KitError, match="no SKILL.md"):
        kits.find_skill(load_office(root), "plan")


# ── the copy ────────────────────────────────────────────────────────────────


def test_a_kit_becomes_a_plugin_the_engines_load(tmp_path, person):
    root = _office(tmp_path, kit=("plan", "check", "plan"))
    _skill(root / ".office" / "skills", "plan", **{"notes.md": "more\n"})
    _skill(person / ".agents" / "skills", "check", said="check")
    dest = tmp_path / "kit"
    assert _build_kit(root, dest) == ["plan", "check"]  # each skill once, in kit order
    for manifest in (".claude-plugin", ".cursor-plugin"):
        assert json.loads((dest / manifest / "plugin.json").read_text())["name"] == "kit"
    assert plugin_skills([dest]) == {
        "kit:check": dest / "skills" / "check" / "SKILL.md",
        "kit:plan": dest / "skills" / "plan" / "SKILL.md",
    }
    assert (dest / "skills" / "plan" / "notes.md").read_text() == "more\n"


def test_the_kits_folders_are_private_whatever_the_umask(tmp_path, person, home):
    root = _office(tmp_path)
    _skill(root / ".office" / "skills", "plan")
    old = os.umask(0o002)  # a umask that lets the group in
    try:
        _build_kit(root, kits.kits_root("mb-20261012T000000Z-abcdef") / "build")
    finally:
        os.umask(old)
    folder = kits.kits_root("mb-20261012T000000Z-abcdef")
    assert private_mode(folder) == 0o700 and private_mode(folder.parent) == 0o700


def test_a_kit_skill_never_brings_hooks_or_servers(tmp_path, person):
    root = _office(tmp_path)
    folder = _skill(root / ".office" / "skills", "plan")
    (folder / "SKILL.md").write_text(
        "---\nname: plan\ndescription: plans\nhooks:\n  PreToolUse: []\n"
        "mcpServers:\n  x: {command: evil}\n---\nBody.\n",
        encoding="utf-8",
    )
    dest = tmp_path / "kit"
    _build_kit(root, dest)
    text = (dest / "skills" / "plan" / "SKILL.md").read_text(encoding="utf-8")
    assert "hooks" not in text and "mcpServers" not in text and "evil" not in text
    assert "description: plans" in text and text.endswith("---\nBody.\n")


def test_a_skill_holding_a_link_is_refused_and_nothing_is_left(tmp_path, person):
    root = _office(tmp_path)
    folder = _skill(root / ".office" / "skills", "plan")
    (person / "secret.txt").write_text("the person's\n", encoding="utf-8")
    (folder / "key").symlink_to(person / "secret.txt")
    with pytest.raises(kits.KitError, match="holds a link, 'key'"):
        _build_kit(root, tmp_path / "kit")
    assert not (tmp_path / "kit").exists()


@pytest.mark.parametrize("shut", [".", "scripts"])
def test_a_skill_folder_that_cannot_be_listed_is_refused_not_copied_as_nothing(
    tmp_path, person, shut
):
    root = _office(tmp_path)
    folder = _skill(root / ".office" / "skills", "plan")
    (folder / "scripts").mkdir()
    (folder / "scripts" / "run.sh").write_text("echo plan\n", encoding="utf-8")
    door = folder / shut
    door.chmod(0o300)  # it can be entered, and not listed
    try:
        if os.access(door, os.R_OK):
            pytest.skip("this user lists any folder")
        with pytest.raises(kits.KitError, match=rf"skill 'plan': {re.escape(shut)} cannot be read"):
            _build_kit(root, tmp_path / "kit")
    finally:
        door.chmod(0o755)
    assert not (tmp_path / "kit").exists()


def test_a_kit_larger_than_the_cap_is_refused_and_nothing_is_left(tmp_path, person, monkeypatch):
    monkeypatch.setattr(kits, "KIT_BYTES", 200)
    root = _office(tmp_path)
    _skill(root / ".office" / "skills", "plan", **{"big.md": "x" * 300})
    with pytest.raises(kits.KitError, match="larger than 200 bytes"):
        _build_kit(root, tmp_path / "kit")
    assert not (tmp_path / "kit").exists()


# ── an office run ───────────────────────────────────────────────────────────


class Looker:
    """Reads the kit its run was given, the way its kind of engine gets one."""

    tool_prefix = "fake"
    skip_tools = ()

    def __init__(self, name: str, loads_plugins: bool) -> None:
        self.name = name
        self.loads_plugins = loads_plugins
        self.jobs: list = []
        self.seen: list[str] = []

    async def run(self, job, *, board=None, approve):
        self.jobs.append(job)
        yield Started(f"s-{self.name}", job.model)
        for folder in job.plugin_dirs if self.loads_plugins else ():  # it loads plugin folders
            for skill in sorted((Path(folder) / "skills").iterdir()):
                self.seen.append((skill / "SKILL.md").read_text(encoding="utf-8"))
        if board is not None and board.has(SKILLS_LOAD):  # any other: the switchboard's tools
            loaded = await board.call(SKILLS_LOAD, {"name": "kit:plan"})
            self.seen.append("\n".join(c.text for c in loaded.content))
        yield Final(f"{self.name} done", f"s-{self.name}")


@pytest.mark.parametrize("loads_plugins", [True, False])
async def test_a_desk_runs_with_its_kit_and_the_copy_goes_when_the_office_run_ends(
    tmp_path, person, home, loads_plugins
):
    root = _office(tmp_path)
    _skill(root / ".office" / "skills", "plan", said="planning")
    engines = {name: Looker(name, loads_plugins) for name in ("claude", "codex", "cursor")}
    result = await _relay(root, engines)
    assert result.ok, [(r.desk, r.error) for r in result.rows]
    [seen] = engines["codex"].seen  # the build desk, on codex here
    assert "Do the planning thing." in seen
    assert engines["claude"].seen == [] and engines["cursor"].seen == []  # no kit, no skills
    assert "Your kit is loaded for this run: plan." in engines["codex"].jobs[0].prompt
    assert "kit" not in engines["claude"].jobs[0].prompt
    record = json.loads(office_journal_path(result.office_run_id).read_text(encoding="utf-8"))
    assert record["kits"] == {"build": ["plan"]}
    assert not (home / "kits" / result.office_run_id).exists()


async def test_a_kit_that_cannot_load_refuses_the_office_run_before_any_desk(
    tmp_path, person, home
):
    root = _office(tmp_path, kit=("plan", "ghost"))
    _skill(root / ".office" / "skills", "plan")
    engines = {name: Looker(name, True) for name in ("claude", "codex", "cursor")}
    with pytest.raises(OfficeError) as err:
        await _relay(root, engines)
    assert "build" in str(err.value) and "'ghost' is not in" in str(err.value)
    assert all(engine.jobs == [] for engine in engines.values())
    assert not (home / "desks").exists()
    for folder in ("offices", "kits"):  # no record and no kit is left; an empty folder may be
        assert not (home / folder).exists() or not any((home / folder).iterdir())


def test_a_kit_file_swapped_for_a_link_after_the_walk_is_refused(tmp_path, monkeypatch):
    """The copy reads through no link, even one put there after every file was checked."""
    person = tmp_path / "person"
    person.mkdir()
    monkeypatch.setenv("HOME", str(person))
    (person / "secret.txt").write_text("the person's secret\n", encoding="utf-8")
    root = _office(tmp_path)
    folder = _skill(root / ".office" / "skills", "plan", **{"notes.md": "plain\n"})
    real = kits._plain_files

    def swap_after_listing(name: str, src: Path):
        listed = real(name, src)  # every file is checked here: plain, no links
        (folder / "notes.md").unlink()
        (folder / "notes.md").symlink_to(person / "secret.txt")
        return listed

    monkeypatch.setattr(kits, "_plain_files", swap_after_listing)
    office = load_office(root)
    dest = tmp_path / "kit"
    with pytest.raises(kits.KitError, match="plain files only"):
        kits.build_kit(office, office.desks["build"], dest)
    assert not dest.exists()
