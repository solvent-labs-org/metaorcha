"""Speed: app start to the page's API answering. Two rows: the first start of a
session (cold: empty state, cold reader) and a warm restart. The budgets are
placeholders until three tablet runs set them (plan: p50 x 1.5)."""
AXIS = "speed"
NEEDS = ()
BUDGET = {"first start": 8000, "warm start": 3000}
SCENARIOS = {}


def run(s):
    s.step("cold_start", "first start", s.start_server, budget_ms=BUDGET["first start"])
    s.stop_server()
    s.step("cold_start", "warm start", s.start_server, budget_ms=BUDGET["warm start"])
