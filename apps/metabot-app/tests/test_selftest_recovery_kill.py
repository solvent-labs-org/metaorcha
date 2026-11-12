"""The recovery_kill journey: the app (and the run with it) is killed mid-run, comes back,
lists the run as interrupted and runs again; the parts a unit test can hold without the core."""
import json
import tempfile
import unittest
from pathlib import Path

from metabot_app.selftest import journeys
from metabot_app.selftest.journeys import recovery_kill as rk


class RecoveryKillTests(unittest.TestCase):
    def test_it_runs_after_concurrency_and_before_recovery_engine(self):
        order = journeys.ORDER
        self.assertLess(order.index("concurrency"), order.index("recovery_kill"))
        self.assertLess(order.index("recovery_kill"), order.index("recovery_engine"))

    def test_it_needs_no_desks_and_uses_a_slow_answer(self):
        self.assertEqual(rk.NEEDS, ())
        self.assertGreaterEqual(rk.SCENARIOS["slow_kill"][0]["delay_ms"], 4000)

    def test_live_pid_reads_the_core_live_file_of_that_run_only(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            (home / "live").mkdir()
            (home / "live" / "mb-1.json").write_text(json.dumps(
                {"format": "metabot.live/1", "kind": "run", "id": "mb-1", "pid": 4242}))
            (home / "live" / "mb-2.json").write_text(json.dumps(
                {"format": "metabot.live/1", "kind": "run", "id": "mb-2", "pid": 4343}))
            (home / "live" / ".mb-3.json").write_text("{")   # a write cut short
            self.assertEqual(rk.live_pid(home, "mb-1"), 4242)
            self.assertEqual(rk.live_pid(home, "mb-2"), 4343)
            self.assertIsNone(rk.live_pid(home, "mb-3"))
            self.assertIsNone(rk.live_pid(home / "nowhere", "mb-1"))


if __name__ == "__main__":
    unittest.main()
