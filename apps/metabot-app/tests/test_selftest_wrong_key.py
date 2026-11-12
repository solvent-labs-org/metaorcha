"""The wrong_key journey: a refused key fails fast and the app says where to fix it."""
import unittest

from metabot_app.selftest import journeys
from metabot_app.selftest.journeys import wrong_key as wk


class WrongKeyTests(unittest.TestCase):
    def test_it_runs_after_recovery_engine(self):
        order = journeys.ORDER
        self.assertLess(order.index("recovery_engine"), order.index("wrong_key"))

    def test_it_needs_no_desks_and_the_engine_answers_401(self):
        self.assertEqual(wk.NEEDS, ())
        self.assertEqual(wk.SCENARIOS["wrong_key"], [{"error": 401}])
        self.assertEqual(wk.BUDGET["the run fails fast"], 5_000)

    def test_the_hint_is_the_one_the_server_writes(self):
        from metabot_app import server
        self.assertEqual(wk.HINT, "Check this engine's key in Settings → Accounts.")
        self.assertTrue(server._run_message({"status": "error", "error": "x answered 401: no"}).endswith(wk.HINT))
        self.assertFalse(server._run_message({"status": "error", "error": "x answered 500: no"}).endswith(wk.HINT))


if __name__ == "__main__":
    unittest.main()
