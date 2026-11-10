"""T1: the self-test runner's own mechanics, hermetic: the scratch world it builds, the
config it writes, how it records a step (an assertion is a failed step, never a crash),
how it waits on the run index, and which journeys run under which caps. The runner
against the real core is tools/selftest-linux.sh, not a unit test."""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from metabot_app.selftest import journeys, report, run as runner  # noqa: E402


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.out = Path(tempfile.mkdtemp(prefix="metachat-selftest-"))
        self.addCleanup(shutil.rmtree, self.out, True)
        self.s = runner.Session(self.out, metabot_bin="/opt/x/metabot", metabot_python="/opt/x/python",
                                orcha_bin="/opt/x/orcha", port=8479)

    def test_the_scratch_world_is_private_and_complete(self):
        self.assertTrue(self.s.root.is_dir() and self.s.root.parent == self.out)
        for d in (self.s.home, self.s.state, self.s.job, self.s.scenarios, self.s.logs, self.s.shots):
            self.assertTrue(d.is_dir(), d)
        self.assertEqual(self.s.state.stat().st_mode & 0o777, 0o700)
        env = self.s.env()
        self.assertEqual(env["METABOT_HOME"], str(self.s.home))
        self.assertEqual(env["ORCHA_KEY_PATH"], str(self.s.home / "key"))
        self.assertEqual(env["METACHAT_STATE"], str(self.s.state))
        self.assertEqual(env["METACHAT_CONFIG"], str(self.s.config))
        self.assertEqual(env["HOME"], str(self.s.userhome))
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertTrue(self.s.report.path == self.s.root / "report.jsonl")
        self.assertRegex(self.s.id, r"^st-\d{8}T\d{6}Z-[0-9a-f]{4}$")

    def test_the_app_config_points_everything_at_the_scratch_world(self):
        self.s.write_app_config()
        cfg = json.loads(self.s.config.read_text())
        self.assertEqual(cfg["port"], 8479)
        self.assertEqual(runner.Session(self.out).port, 0)      # default: the server picks, server.json tells
        self.assertEqual(cfg["bind"], ["127.0.0.1"])
        self.assertEqual(cfg["state_dir"], str(self.s.state))
        self.assertEqual(cfg["metabot_bin"], "/opt/x/metabot")
        self.assertEqual(cfg["metabot_python"], "/opt/x/python")
        self.assertEqual(cfg["orcha_bin"], "/opt/x/orcha")
        self.assertEqual(cfg["metabot_home"], str(self.s.home))
        self.assertEqual(cfg["project_roots"], [str(self.s.job)])
        self.assertEqual(cfg["projects"], [{"name": "selftest", "paths": [str(self.s.job)]}])
        self.assertEqual(cfg["max_runs"], 3)
        self.assertFalse(cfg["phone_listener"])
        self.assertEqual(cfg["tmux_socket"], f"mb-{self.s.id}")

    def test_set_model_rewrites_the_core_config(self):
        self.s.home.mkdir(exist_ok=True)
        (self.s.home / "config.json").write_text(json.dumps({"default_engine": "openai", "openai_model": "x"}))
        self.s.engine_url = "http://127.0.0.1:1/v1"
        self.s.set_model("tools_offline")
        cfg = json.loads((self.s.home / "config.json").read_text())
        self.assertEqual(cfg["openai_model"], "tools_offline")
        self.assertEqual(cfg["openai_base_url"], "http://127.0.0.1:1/v1")
        self.assertEqual(cfg["default_engine"], "openai")

    def test_a_step_records_its_outcome_and_never_raises(self):
        self.assertEqual(self.s.step("j", "adds", lambda: 2 + 2, budget_ms=50), 4)
        self.assertIsNone(self.s.step("j", "asserts", lambda: self._fail("nope")))
        self.assertIsNone(self.s.step("j", "crashes", lambda: 1 / 0))
        rows = report.read(self.s.report.path)
        self.assertEqual([r["status"] for r in rows], ["pass", "fail", "fail"])
        self.assertEqual(rows[0]["budget_ms"], 50)
        self.assertEqual(rows[1]["detail"], "nope")
        self.assertIn("ZeroDivisionError", rows[2]["detail"])
        self.assertTrue(self.s.failed)

    def _fail(self, why):
        raise AssertionError(why)

    def test_wait_run_polls_the_index_until_the_desk_is_done(self):
        answers = [{"runs": []},
                   {"runs": [{"run_id": "mb-1", "state": "live", "desk": "run-a"}]},
                   {"runs": [{"run_id": "mb-1", "state": "done", "desk": "run-a", "receipt_state": "sealed"}]}]
        self.s.api = lambda method, path, body=None: (200, answers.pop(0))
        row = self.s.wait_run("run-a", deadline_s=5, every_s=0.001)
        self.assertEqual(row["run_id"], "mb-1")
        self.assertEqual(answers, [])
        self.s.api = lambda method, path, body=None: (200, {"runs": []})
        with self.assertRaises(AssertionError):
            self.s.wait_run("run-b", deadline_s=0.01, every_s=0.001)

    def test_journeys_are_selected_and_skipped_by_caps(self):
        names = journeys.names()
        for n in ("cold_start", "setup_first_run", "ask_receipt", "tools_offline", "recovery_engine"):
            self.assertIn(n, names)
        self.assertEqual(journeys.pick(["ask_receipt", "cold_start"]), ["cold_start", "ask_receipt"])  # plan order
        with self.assertRaises(KeyError):
            journeys.pick(["no_such"])
        mod = journeys.load("ask_receipt")
        self.assertEqual(mod.AXIS, "clarity")
        self.assertIsInstance(mod.SCENARIOS, dict)
        self.assertTrue(callable(mod.run))
        # a journey whose needs are not met is skipped with the reason in the report, never silent
        mod2 = journeys.load("tools_offline")
        self.assertIn("desks", mod2.NEEDS)
        self.s.caps = {"desks": False, "engines": {"openai": "http"}}
        self.s.run_journey("tools_offline")
        rows = report.read(self.s.report.path)
        self.assertEqual(rows[-1]["status"], "skip")
        self.assertIn("desks", rows[-1]["detail"])

    def test_scenarios_are_installed_from_every_journey(self):
        installed = self.s.install_scenarios(["ask_receipt", "tools_offline", "recovery_engine"])
        self.assertIn("ask", installed)
        self.assertIn("tools_offline", installed)
        self.assertIn("error_500", installed)
        for n in installed:
            self.assertTrue((self.s.scenarios / f"{n}.jsonl").is_file(), n)


if __name__ == "__main__":
    unittest.main()
