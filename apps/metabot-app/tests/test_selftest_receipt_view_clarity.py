"""The receipt_view_clarity journey: the parts a unit test can hold without the core."""
import json
import unittest

from metabot_app.selftest import journeys
from metabot_app.selftest.journeys import receipt_view_clarity as rc


class ReceiptViewClarityTests(unittest.TestCase):
    def test_it_is_last_and_needs_no_desks(self):
        self.assertEqual(journeys.ORDER[-1], "receipt_view_clarity")
        self.assertEqual(rc.NEEDS, ())
        self.assertEqual([line.get("tool_call", {}).get("name") for line in rc.SCENARIOS["clarity"][:2]],
                         ["list_dir", "read_file"])

    def test_tampered_changes_one_step_output_and_nothing_else(self):
        env = {"steps": [{"call_id": "c0", "tool": "t", "output": "ok"}, {"call_id": "c1", "output": "x"}],
               "signature": "s", "verdicts": []}
        out = json.loads(rc.tampered(json.dumps(env)))
        self.assertEqual(out["steps"][0]["output"], "ok (edited)")
        self.assertEqual(out["steps"][1], env["steps"][1])
        self.assertEqual((out["signature"], out["verdicts"]), ("s", []))
        with self.assertRaises(AssertionError):
            rc.tampered(json.dumps({"steps": []}))


if __name__ == "__main__":
    unittest.main()
