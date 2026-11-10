"""The concurrency journey: three runs at once, a fourth refused at the cap, overlap read
from the receipts; the parts a unit test can hold without the core."""
import unittest

from metabot_app.selftest import journeys
from metabot_app.selftest.journeys import concurrency as cc


class ConcurrencyTests(unittest.TestCase):
    def test_it_is_in_the_plan_order_before_recovery(self):
        order = journeys.ORDER
        self.assertLess(order.index("workflow_chain"), order.index("concurrency"))
        self.assertLess(order.index("concurrency"), order.index("recovery_engine"))

    def test_it_needs_no_desks_and_uses_a_slow_answer(self):
        self.assertEqual(cc.NEEDS, ())
        self.assertEqual(cc.AT_ONCE, 3)
        self.assertEqual(cc.SCENARIOS["slow3"][0]["delay_ms"], 3000)

    def test_overlap_is_read_from_start_and_finish_times(self):
        three = [("2026-01-01T00:00:00Z", "2026-01-01T00:00:04Z"),
                 ("2026-01-01T00:00:01Z", "2026-01-01T00:00:05Z"),
                 ("2026-01-01T00:00:02Z", "2026-01-01T00:00:06Z")]
        self.assertTrue(cc.overlapped(three))
        serial = [("2026-01-01T00:00:00Z", "2026-01-01T00:00:01Z"),
                  ("2026-01-01T00:00:01Z", "2026-01-01T00:00:02Z"),
                  ("2026-01-01T00:00:02Z", "2026-01-01T00:00:03Z")]
        self.assertFalse(cc.overlapped(serial))
        self.assertEqual(cc.when("2026-01-01T00:00:00+00:00"), cc.when("2026-01-01T00:00:00Z"))
