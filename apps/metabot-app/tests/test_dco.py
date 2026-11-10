"""The DCO check (.github/scripts/check_dco.py) against a throwaway git repository: a
commit signed off by its author passes; an unsigned one, or one signed off by someone
else, fails the range. The repository lives in a temporary folder and never sees the
developer's git config."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / ".github" / "scripts" / "check_dco.py"
ME = ("A Contributor", "contributor@example.com")


class DcoTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="dco-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.root),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": ME[0], "GIT_AUTHOR_EMAIL": ME[1],
            "GIT_COMMITTER_NAME": ME[0], "GIT_COMMITTER_EMAIL": ME[1],
            "LC_ALL": "C",
        }
        self.git("init", "-q", "-b", "main")
        self.git("config", "commit.gpgsign", "false")
        self.base = self.commit("base", signoff=True)

    def git(self, *args, env=None):
        p = subprocess.run(["git", *args], cwd=self.repo, env=env or self.env,
                           capture_output=True, text=True, check=True)
        return p.stdout.strip()

    def commit(self, msg, signoff=False, author=None, trailer=None):
        env = dict(self.env)
        if author:
            env["GIT_AUTHOR_NAME"], env["GIT_AUTHOR_EMAIL"] = author
        self.n = getattr(self, "n", 0) + 1    # one file per commit, so branches merge cleanly
        name = f"f{self.n}.txt"
        (self.repo / name).write_text(msg + "\n")
        self.git("add", name, env=env)
        args = ["commit", "-q", "-m", msg]
        if signoff:
            args.append("-s")      # signs off as the committer: ME
        if trailer:
            args += ["-m", trailer]
        self.git(*args, env=env)
        return self.git("rev-parse", "HEAD")

    def check(self, rng):
        return subprocess.run([sys.executable, "-B", str(SCRIPT), rng], cwd=self.repo, env=self.env,
                              capture_output=True, text=True)

    def test_a_signed_off_commit_passes(self):
        head = self.commit("feat: signed", signoff=True)
        r = self.check(f"{self.base}..{head}")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(head[:12], r.stdout)

    def test_a_commit_without_sign_off_fails(self):
        good = self.commit("feat: signed", signoff=True)
        bad = self.commit("feat: not signed")
        r = self.check(f"{self.base}..{bad}")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(bad[:12], r.stdout)
        self.assertIn("FAIL", r.stdout)
        self.assertIn(f"ok   {good[:12]}", r.stdout)

    def test_a_sign_off_by_someone_else_fails(self):
        head = self.commit("feat: other author", signoff=True,
                           author=("Someone Else", "else@example.com"))
        r = self.check(f"{self.base}..{head}")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("else@example.com", r.stdout)

    def test_the_author_email_matches_without_case(self):
        head = self.commit("feat: case", trailer="Signed-off-by: A Contributor <Contributor@Example.COM>")
        r = self.check(f"{self.base}..{head}")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_sign_off_in_the_middle_of_the_body_is_not_a_trailer(self):
        head = self.commit("feat: body", trailer="Signed-off-by: A Contributor <contributor@example.com>\n"
                                                 "\nmore text after it")
        r = self.check(f"{self.base}..{head}")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)

    def test_merge_commits_are_skipped(self):
        self.git("checkout", "-q", "-b", "side")
        side = self.commit("feat: side", signoff=True)
        self.git("checkout", "-q", "main")
        self.commit("feat: main", signoff=True)
        self.git("merge", "-q", "--no-ff", "--no-edit", "side")
        head = self.git("rev-parse", "HEAD")
        r = self.check(f"{self.base}..{head}")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(f"skip {head[:12]}", r.stdout)
        self.assertIn(side[:12], r.stdout)

    def test_an_empty_range_passes(self):
        r = self.check(f"{self.base}..{self.base}")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("no commits", r.stdout)

    def test_a_bad_range_is_an_error_not_a_pass(self):
        r = self.check("nosuchref..HEAD")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)

    def test_no_argument_is_a_usage_error(self):
        r = subprocess.run([sys.executable, "-B", str(SCRIPT)], cwd=self.repo, env=self.env,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)


if __name__ == "__main__":
    unittest.main()
