"""B5: doctor. One report, ten items with ids the page keys on, each with what it
found and the fix in plain words; a blocker makes the report not ok. No raw
exception text ever reaches an item. Served as GET /api/doctor and as the CLI's
``doctor [--json]``."""
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))
from harness import Env  # noqa: E402
from metabot_app import doctor, paths, server, tmuxcmd  # noqa: E402

IDS = ["python", "core", "key", "tmux", "engine.claude", "engine.codex", "engine.cursor",
       "engine.local", "sandbox", "folder"]


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.env = Env().start()
        self.addCleanup(self.env.stop)
        self.key = self.env.root / "key"
        self.key.write_text("k\n")
        self.key.chmod(0o600)
        os.environ["ORCHA_KEY_PATH"] = str(self.key)
        self.addCleanup(os.environ.pop, "ORCHA_KEY_PATH", None)
        # hermetic: the fixture bin and the system dirs only, never the user's own bins
        # (Env.stop restores PATH); the fake core needs python3 from the system
        os.environ["PATH"] = f"{self.env.bin}:/usr/bin:/bin"
        lp = mock.patch.object(paths, "login_path", return_value="")
        lp.start()
        self.addCleanup(lp.stop)
        self.engine("claude")          # one sign-in exists unless a test removes it

    def engine(self, name):
        p = self.env.bin / name
        p.write_text("#!/bin/sh\nexit 0\n")
        p.chmod(0o755)

    def items(self, **patch):
        with mock.patch.object(tmuxcmd, "version", return_value=patch.pop("tmux", (3, 4))):
            rep = doctor.report(server.MB)
        self.assertEqual([i["id"] for i in rep["items"]], IDS)
        for i in rep["items"]:
            self.assertEqual(set(i), {"id", "ok", "what", "fix", "blocker", "configured"})
            self.assertIsInstance(i["what"], str)
            self.assertIsInstance(i["fix"], str)
            self.assertNotIn("Traceback", i["what"] + i["fix"])
            self.assertNotIn("Error", i["what"] + i["fix"])
        return rep, {i["id"]: i for i in rep["items"]}

    def test_all_well(self):
        rep, by = self.items()
        self.assertTrue(rep["ok"], [i for i in rep["items"] if not i["ok"]])
        self.assertTrue(by["python"]["ok"] and by["core"]["ok"] and by["key"]["ok"] and by["tmux"]["ok"])
        self.assertIn("3.4", by["tmux"]["what"])
        self.assertIn("0.3.0", by["core"]["what"])   # the fake core's version line
        self.assertTrue(by["folder"]["ok"])
        self.assertEqual(by["key"]["fix"], "")

    def test_no_core_is_a_blocker_with_the_install_line(self):
        server.CFG["metabot_bin"] = str(self.env.root / "no-such-metabot")
        server.MB._versions = None
        rep, by = self.items()
        self.assertFalse(rep["ok"])
        self.assertFalse(by["core"]["ok"])
        self.assertTrue(by["core"]["blocker"])
        self.assertIn("uv tool install metaorcha", by["core"]["fix"])

    def test_old_tmux_fails_with_the_floor(self):
        rep, by = self.items(tmux=(3, 1))
        self.assertFalse(by["tmux"]["ok"])
        self.assertIn("3.2 or newer", by["tmux"]["fix"])
        self.assertIn("3.1", by["tmux"]["what"])

    def test_no_tmux_at_all(self):
        with mock.patch.object(tmuxcmd, "found", return_value=None):
            rep, by = self.items(tmux=None)
        self.assertFalse(by["tmux"]["ok"])
        self.assertTrue(by["tmux"]["blocker"])
        self.assertIn("tmux", by["tmux"]["fix"])

    def test_missing_key_says_init(self):
        self.key.unlink()
        rep, by = self.items()
        self.assertFalse(by["key"]["ok"])
        self.assertTrue(by["key"]["blocker"])
        self.assertIn("metabot init", by["key"]["fix"])
        self.assertNotIn(str(self.key), by["key"]["what"])     # never the path of a secret

    def test_key_mode_is_checked(self):
        self.key.chmod(0o644)
        rep, by = self.items()
        self.assertFalse(by["key"]["ok"])
        self.assertIn("0600", by["key"]["fix"])

    def test_engines_found_on_path(self):
        self.engine("cursor-agent")
        rep, by = self.items()
        self.assertTrue(by["engine.claude"]["ok"])
        self.assertTrue(by["engine.cursor"]["ok"])
        self.assertFalse(by["engine.codex"]["ok"])
        self.assertFalse(by["engine.codex"]["blocker"])       # an absent sign-in is a choice, not a fault
        self.assertIn("codex", by["engine.codex"]["fix"])

    def test_local_model_server_is_probed_on_loopback_only(self):
        hits = []

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                hits.append(self.path)
                body = b'{"data":[{"id":"m1"},{"id":"m2"}]}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass
        srv = HTTPServer(("127.0.0.1", 0), H)
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        (self.env.mbhome / "config.json").write_text(json.dumps(
            {"openai_base_url": f"http://127.0.0.1:{srv.server_address[1]}/v1"}))
        rep, by = self.items()
        self.assertTrue(by["engine.local"]["ok"])
        self.assertIn("2 model", by["engine.local"]["what"])
        self.assertFalse(by["engine.local"]["configured"])   # a probed server needs no second flag
        self.assertEqual(hits, ["/v1/models"])
        # a hosted address is never probed by doctor
        (self.env.mbhome / "config.json").write_text(json.dumps({"openai_base_url": "https://api.example.com/v1"}))
        rep, by = self.items()
        self.assertFalse(by["engine.local"]["ok"])
        self.assertIn("not probed", by["engine.local"]["what"])
        self.assertFalse(by["engine.local"]["blocker"])
        self.assertIs(by["engine.local"]["configured"], True)   # Bugbot 11-12: the page counts a hosted engine as set up

    def test_no_local_model_server(self):
        (self.env.mbhome / "config.json").write_text(json.dumps({"openai_base_url": "http://127.0.0.1:1/v1"}))
        rep, by = self.items()
        self.assertFalse(by["engine.local"]["ok"])
        self.assertFalse(by["engine.local"]["blocker"])
        self.assertNotIn("refused", by["engine.local"]["what"].lower())   # plain words, no errno text
        self.assertIn("model server", by["engine.local"]["fix"])

    def test_no_engine_at_all_blocks_the_first_run(self):
        (self.env.mbhome / "config.json").write_text(json.dumps({"openai_base_url": "http://127.0.0.1:1/v1"}))
        with mock.patch.object(paths, "find_bin", side_effect=lambda n: None if n in ("claude", "codex", "cursor-agent") else shutil.which(n)):
            rep, by = self.items()
        self.assertFalse(rep["ok"])
        self.assertTrue(by["engine.local"]["blocker"])
        self.assertIn("sign in", by["engine.local"]["fix"])

    def test_folder_blocks_when_no_root_exists(self):
        server.CFG["project_roots"] = [str(self.env.root / "gone")]
        server.CFG["projects"] = []
        rep, by = self.items()
        self.assertFalse(by["folder"]["ok"])
        self.assertTrue(by["folder"]["blocker"])
        self.assertIn("folder", by["folder"]["fix"])

    def test_sandbox_is_a_note_not_a_blocker(self):
        with mock.patch.object(paths, "find_bin", side_effect=lambda n: None if n == "bwrap" else shutil.which(n)):
            rep, by = self.items()
        self.assertFalse(by["sandbox"]["ok"])
        self.assertFalse(by["sandbox"]["blocker"])
        self.assertIn("cursor", by["sandbox"]["fix"])


class RouteTests(unittest.TestCase):
    def test_get_api_doctor(self):
        env = Env().start()
        self.addCleanup(env.stop)
        with mock.patch.object(tmuxcmd, "version", return_value=(3, 4)):
            status, body = env.get("/api/doctor")
        self.assertEqual(status, 200)
        self.assertIn("ok", body)
        self.assertEqual([i["id"] for i in body["items"]], IDS)

    def test_the_route_needs_the_token(self):
        env = Env().start()
        self.addCleanup(env.stop)
        status, _ = env.get("/api/doctor?t=nope", token=False)
        self.assertEqual(status, 403)


if __name__ == "__main__":
    unittest.main()
