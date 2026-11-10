"""B6: folders. GET /api/dirs lists the subfolders of a folder (dot folders only when
asked); POST /api/folders adds or removes a project root, writing config.json
atomically with every other key kept, and the running server accepts the folder at
once. The home folder itself, ~/.ssh, ~/.orcha, ~/.metabot and any ~/.claude* are
never accepted."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))
from harness import Env, server  # noqa: E402
from metabot_app import catalog  # noqa: E402


class FoldersTests(unittest.TestCase):
    def setUp(self):
        self.env = Env(config={"max_runs": 4}).start()
        self.addCleanup(self.env.stop)
        self.home = self.env.home
        self.work = self.home / "work"
        (self.work / "proj" / "src").mkdir(parents=True)
        (self.work / ".hidden").mkdir()
        (self.work / "notes.txt").write_text("x")
        (self.home / ".ssh").mkdir()
        (self.home / ".claude-work").mkdir()

    def test_refused_folders(self):
        for raw in (str(self.home), str(self.home / ".ssh"), str(self.home / ".claude-work"),
                    str(self.home / ".claude"), "work/proj", str(self.work / "nope"), str(self.work / "notes.txt"), ""):
            status, body = self.env.post("/api/folders", {"add": raw})
            self.assertEqual(status, 400, raw)
            self.assertIn("error", body)
            self.assertNotIn("Traceback", body["error"])

    def test_add_takes_effect_at_once_and_keeps_the_config(self):
        target = str(self.work / "proj")
        self.assertIsNone(catalog.path_ok(target, server.MB.catalog_ctx()))
        status, body = self.env.post("/api/folders", {"add": target})
        self.assertEqual(status, 200, body)
        self.assertIn(str(Path(target).resolve()), body["project_roots"])
        self.assertEqual(catalog.path_ok(target, server.MB.catalog_ctx()), str(Path(target).resolve()))
        raw = json.loads(self.env.config.read_text())
        self.assertEqual(raw["max_runs"], 4)                      # the keys it had before
        self.assertEqual(raw["metabot_bin"], str(Path(__file__).resolve().parent / "fixtures" / "fake-metabot"))
        self.assertIn(str(Path(target).resolve()), raw["project_roots"])
        self.assertFalse(list(self.env.config.parent.glob("*.tmp")))   # written atomically, nothing left
        status, body = self.env.post("/api/folders", {"add": target})
        self.assertEqual(status, 200)
        self.assertEqual(body["project_roots"].count(str(Path(target).resolve())), 1)   # no duplicate
        kinds = [e["kind"] for e in self.env.get("/api/events")[1]["events"]]
        self.assertIn("folder_added", kinds)

    def test_remove(self):
        target = str(self.work / "proj")
        self.env.post("/api/folders", {"add": target})
        status, body = self.env.post("/api/folders", {"remove": target})
        self.assertEqual(status, 200)
        self.assertNotIn(str(Path(target).resolve()), body["project_roots"])
        self.assertIsNone(catalog.path_ok(target, server.MB.catalog_ctx()))
        self.assertNotIn(str(Path(target).resolve()), json.loads(self.env.config.read_text())["project_roots"])
        status, body = self.env.post("/api/folders", {"remove": target})
        self.assertEqual(status, 404)
        status, body = self.env.post("/api/folders", {})
        self.assertEqual(status, 400)

    def test_dirs_lists_subfolders(self):
        status, body = self.env.get(f"/api/dirs?under={self.work}")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["under"], str(self.work.resolve()))
        self.assertEqual([d["name"] for d in body["dirs"]], ["proj"])
        self.assertEqual(body["dirs"][0]["path"], str((self.work / "proj").resolve()))
        self.assertEqual(body["parent"], str(self.home.resolve()))
        status, body = self.env.get(f"/api/dirs?under={self.work}&all=1")
        self.assertEqual([d["name"] for d in body["dirs"]], [".hidden", "proj"])

    def test_dirs_default_and_refusals(self):
        status, body = self.env.get("/api/dirs")
        self.assertEqual(status, 200)
        self.assertEqual(body["under"], str(self.home.resolve()))       # browsing starts at home
        self.assertIn("work", [d["name"] for d in body["dirs"]])
        self.assertNotIn(".ssh", [d["name"] for d in body["dirs"]])
        self.assertIsNone(body["parent"]) if body["under"] == "/" else None
        for raw in (str(self.home / ".ssh"), str(self.work / "notes.txt"), "relative", str(self.work / "gone")):
            status, body = self.env.get(f"/api/dirs?under={raw}")
            self.assertEqual(status, 400, raw)

    def test_the_tablet_may_not_browse_a_claude_folder(self):
        status, body = self.env.get(f"/api/dirs?under={self.home / '.claude-work'}")
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
