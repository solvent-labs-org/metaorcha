"""Section 2: config loading never exits; defaults; literals moved to config."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))
from harness import Env, server  # noqa: E402


class ConfigTests(unittest.TestCase):
    def test_missing_file_is_defaults(self):
        cfg = server.load_config("/nonexistent/metachat.json")
        self.assertEqual(cfg, server.DEFAULTS)
        self.assertEqual(cfg["default_desk"], "")
        self.assertEqual(cfg["claude_name_prefixes"], ["claude"])

    def test_malformed_file_is_defaults_no_exit(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write("{not json")
        try:
            cfg = server.load_config(f.name)
        finally:
            os.unlink(f.name)
        self.assertEqual(cfg["port"], 8460)

    def test_wrong_type_key_ignored(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"port": "8460", "max_runs": 3, "unknown": 1}, f)
        try:
            cfg = server.load_config(f.name)
        finally:
            os.unlink(f.name)
        self.assertEqual(cfg["port"], 8460)
        self.assertEqual(cfg["max_runs"], 3)
        self.assertNotIn("unknown", cfg)

    def test_desk_names_come_from_config_not_literals(self):
        root = Path(__file__).resolve().parents[1] / "src" / "metabot_app"
        text = (root / "server.py").read_text()
        # the old code guessed Claude desks from a tuple of name literals and
        # routed to one hard-coded default desk; both now read config
        self.assertNotIn('startswith(("claude"', text)
        self.assertIn('"claude" if CLAUDE_PREFIXES and d["name"].startswith(CLAUDE_PREFIXES)', text)
        self.assertIn("DEFAULT_DESK", text)
        self.assertIn("CLAUDE_PREFIXES", text)
        self.assertEqual(server.DEFAULTS["default_desk"], "")
        self.assertEqual(server.DEFAULTS["inbox_module"], "")   # no chat inbox unless one is configured
        self.assertNotIn("healthchecks.env", text)

    def test_state_dir_env_override_and_token_kept(self):
        with Env() as env:
            self.assertEqual(server.STATE, env.state)
            tok = (env.state / "token").read_text().strip()
            self.assertEqual(tok, env.token)
            self.assertEqual(oct((env.state / "token").stat().st_mode & 0o777), "0o600")
            server.configure()
            self.assertEqual(server.TOKEN, tok)   # a second load keeps the token


if __name__ == "__main__":
    unittest.main()
