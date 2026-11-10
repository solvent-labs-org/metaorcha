"""The contributing guides are rooted in real tests: every command they give exists, the
CI jobs run exactly what the guides say they run, and the versions they ask for are the
versions the code requires. Standard library only, so it runs on a bare CI Python."""
import re
import tomllib
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
REPO = APP.parents[1]
GUIDES = ["CONTRIBUTING.md", "CONTRIBUTING-linux.md", "CONTRIBUTING-macos.md", "TESTING.md"]
OS_GUIDES = {"linux": "CONTRIBUTING-linux.md", "macos": "CONTRIBUTING-macos.md"}
WORKFLOW = REPO / ".github" / "workflows" / "metabot-app.yml"
WORKFLOWS = REPO / ".github" / "workflows"

FENCE = re.compile(r"^```(\w*)[^\n]*\n(.*?)^```", re.M | re.S)
SHELL_LANGS = {"sh", "bash", "shell", "console", "zsh"}
MAKE = re.compile(r"(?:^\s*|[;&|(]\s*)make((?:\s+-[A-Za-z]\s+\S+|\s+-[A-Za-z]+)*)((?:\s+[A-Za-z][\w-]*(?=\s|$|[;&|)]))*)")
TARGET = re.compile(r"^([A-Za-z][\w-]*(?:\s+[A-Za-z][\w-]*)*)\s*:(?!=)", re.M)
CI_SAYS = re.compile(r"CI's `([\w-]+)` job runs exactly `([^`]+)`")
USES = re.compile(r"^\s*(?:-\s+)?uses:\s*(\S+)", re.M)
PINNED = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")


def read(name: str) -> str:
    return (APP / name).read_text(encoding="utf-8")


def shell_blocks(text: str) -> list[str]:
    return [body for lang, body in FENCE.findall(text) if lang.lower() in SHELL_LANGS]


def make_targets_in(text: str) -> list[str]:
    out = []
    for block in shell_blocks(text):
        for line in block.splitlines():
            line = line.split("#", 1)[0]
            for _flags, targets in MAKE.findall(line):
                out.extend(targets.split())
    return out


def makefile_targets() -> set[str]:
    targets: set[str] = set()
    for names in TARGET.findall((APP / "Makefile").read_text(encoding="utf-8")):
        targets.update(names.split())
    return targets - {"PHONY"}


def workflow_jobs(text: str) -> dict[str, str]:
    """Job id -> its text, from the top-level ``jobs:`` mapping (two-space job keys)."""
    body = text.split("\njobs:\n", 1)[1]
    parts = re.split(r"^  ([\w-]+):[ \t]*\n", body, flags=re.M)
    return {parts[i]: parts[i + 1] for i in range(1, len(parts) - 1, 2)}


def floors(pattern: str) -> dict[str, list[str]]:
    rx = re.compile(pattern)
    return {g: rx.findall(read(g)) for g in GUIDES}


class GuideCommandTests(unittest.TestCase):
    def test_every_make_target_in_the_guides_exists(self):
        have = makefile_targets()
        for g in GUIDES:
            named = make_targets_in(read(g))
            if g != "TESTING.md":
                self.assertTrue(named, f"{g} gives no make command in a shell block")
            missing = sorted(set(named) - have)
            self.assertEqual(missing, [], f"{g} names make targets the Makefile lacks")

    def test_the_parser_sees_a_missing_target(self):
        text = ("```sh\nmake check\nmake -C apps/metabot-app no-such-target  # x\n"
                "make unit PYTHON=python3\nsudo apt-get install -y git make tmux\n```\n```text\nmake not-a-shell-block\n```\n")
        self.assertEqual(make_targets_in(text), ["check", "no-such-target", "unit"])
        self.assertNotIn("no-such-target", makefile_targets())

    def test_the_os_targets_exist(self):
        self.assertTrue({"check", "unit", "ui", "verifier", "guard", "test-linux", "test-macos"}
                        <= makefile_targets())


class WorkflowMatchesGuidesTests(unittest.TestCase):
    def setUp(self):
        self.text = WORKFLOW.read_text(encoding="utf-8")
        self.jobs = workflow_jobs(self.text)

    def test_each_os_job_runs_its_target(self):
        for job in ("linux", "macos"):
            self.assertIn(job, self.jobs)
            self.assertIn(f"make -C apps/metabot-app test-{job}", self.jobs[job])

    def test_each_os_guide_says_what_its_job_runs(self):
        for job, guide in OS_GUIDES.items():
            text = read(guide)
            said = CI_SAYS.findall(text)
            self.assertIn((job, f"make -C apps/metabot-app test-{job}"), said, guide)
            for name, cmd in said:
                self.assertIn(name, self.jobs, f"{guide} names a CI job that does not exist")
                self.assertIn(cmd, self.jobs[name], f"{guide}: job {name} does not run {cmd}")
            # and the contributor's own command is the same target
            self.assertIn(f"test-{job}", make_targets_in(text))

    def test_every_action_is_pinned_to_a_sha(self):
        uses = USES.findall(self.text)
        self.assertTrue(uses)
        loose = [u for u in uses if not PINNED.match(u)]
        self.assertEqual(loose, [], "actions not pinned to a 40-hex SHA")

    def test_pins_are_copied_from_the_other_workflows(self):
        others = "\n".join(p.read_text(encoding="utf-8") for p in sorted(WORKFLOWS.glob("*.yml"))
                           if p != WORKFLOW)
        for u in USES.findall(self.text):
            self.assertIn(u, others, f"{u} is pinned nowhere else in the repo")

    def test_the_node_version_matches_the_guides(self):
        nodes = set(re.findall(r'node-version:\s*"?(\d+)', self.text))
        self.assertEqual(nodes, {"20"})
        for g in OS_GUIDES.values():
            self.assertEqual(set(re.findall(r"\bNode (\d+)\b", read(g))), {"20"}, g)


class VersionFloorTests(unittest.TestCase):
    def test_python_floor_matches_pyproject(self):
        req = tomllib.loads((APP / "pyproject.toml").read_text(encoding="utf-8"))["project"]["requires-python"]
        self.assertTrue(req.startswith(">="), req)
        floor = req[2:].strip()
        found = floors(r"\bPython (\d+\.\d+) or newer")
        for g in OS_GUIDES.values():
            self.assertTrue(found[g], f"{g} does not state the Python floor")
        for g, vals in found.items():
            self.assertEqual(set(vals) - {floor}, set(), f"{g} states another Python floor")

    def test_tmux_floor_is_3_2(self):
        found = floors(r"\btmux (\d+\.\d+) or newer")
        for g in OS_GUIDES.values():
            self.assertTrue(found[g], f"{g} does not state the tmux floor")
        for g, vals in found.items():
            self.assertEqual(set(vals) - {"3.2"}, set(), f"{g} states another tmux floor")


if __name__ == "__main__":
    unittest.main()
