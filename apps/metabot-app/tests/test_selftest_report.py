"""T3: the self-test report. JSON Lines, one line per step, a summary line last, so a
run the server died under still has every step it took; report.md derived from the
lines; the verdict is an exit code (fail, or a budget miss where budgets are enforced)."""
import json
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from metabot_app.selftest import report  # noqa: E402


class WriterTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="metachat-report-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = self.dir / "report.jsonl"

    def test_steps_are_lines_written_at_once(self):
        r = report.Report(self.path, session="s1", device={"model": "pc", "os": "linux", "abi": "x86_64"},
                          mode="linux", build="dev", model_source="fake")
        r.step("ask_receipt", "start run", True, 12.5, budget_ms=None, run_id="mb-1")
        r.step("ask_receipt", "verify", False, 40, budget_ms=10_000, detail="INVALID")
        rows = [json.loads(l) for l in self.path.read_text().splitlines()]
        self.assertEqual(len(rows), 2)                      # no summary yet
        self.assertEqual(rows[0]["journey"], "ask_receipt")
        self.assertEqual(rows[0]["step"], "start run")
        self.assertEqual(rows[0]["origin"], "runner")
        self.assertEqual(rows[0]["ok"], True)
        self.assertEqual(rows[0]["ms"], 12)
        self.assertEqual(rows[0]["run_id"], "mb-1")
        self.assertIsNone(rows[0]["budget_ms"])
        self.assertEqual(rows[1]["detail"], "INVALID")
        self.assertEqual(rows[1]["session"], "s1")
        self.assertAlmostEqual(rows[0]["ts"], time.time(), delta=5)

    def test_skip_and_invalid_are_statuses_not_failures(self):
        r = report.Report(self.path, session="s1")
        r.skip("remote_pair", "no USB")
        r.step("perf", "scroll", True, 5, origin="page", status="invalid", detail="hidden")
        r.summary(caps={"desks": False})
        rows = [json.loads(l) for l in self.path.read_text().splitlines()]
        self.assertEqual(rows[0]["status"], "skip")
        self.assertEqual(rows[0]["detail"], "no USB")
        self.assertEqual(rows[1]["status"], "invalid")
        s = rows[-1]
        self.assertEqual(s["summary"], True)
        self.assertEqual((s["pass"], s["fail"], s["skipped"], s["invalid"]), (0, 0, 1, 1))
        self.assertEqual(s["caps"], {"desks": False})

    def test_summary_counts_and_budget_misses(self):
        r = report.Report(self.path, session="s1", mode="on_device")
        r.step("cold_start", "page", True, 4000, budget_ms=3000)    # over budget, but ok
        r.step("ask_receipt", "verify", True, 100, budget_ms=10_000)
        r.step("tools", "files", False, 1)
        r.summary()
        s = json.loads(self.path.read_text().splitlines()[-1])
        self.assertEqual((s["pass"], s["fail"]), (2, 1))
        self.assertEqual(s["budget_misses"], [{"journey": "cold_start", "step": "page", "ms": 4000, "budget_ms": 3000}])
        self.assertEqual(s["mode"], "on_device")


class ReadTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="metachat-report-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = self.dir / "report.jsonl"
        r = report.Report(self.path, session="s2", mode="linux", build="abc", device={"model": "pc"})
        r.step("cold_start", "page", True, 4000, budget_ms=3000)
        r.step("ask_receipt", "start", True, 20, run_id="mb-9")
        r.step("ask_receipt", "verify", True, 90, budget_ms=10_000, receipt_sha256="ab" * 32)
        r.skip("remote_pair", "no USB")
        r.summary(caps={"desks": True})

    def test_render_md_lists_every_journey(self):
        lines = report.read(self.path)
        md = report.render_md(lines)
        self.assertIn("| cold_start |", md)
        self.assertIn("4000", md)
        self.assertIn("remote_pair", md)
        self.assertIn("skip", md)
        self.assertIn("no USB", md)
        self.assertIn("s2", md)
        self.assertIn("pass 3", md)

    def test_verdict_is_an_exit_code(self):
        lines = report.read(self.path)
        self.assertEqual(report.verdict(lines, enforce_budgets=False), 0)
        self.assertEqual(report.verdict(lines, enforce_budgets=True), 2)   # cold_start missed its budget
        failing = report.Report(self.dir / "f.jsonl", session="s3")
        failing.step("x", "y", False, 1)
        failing.summary()
        self.assertEqual(report.verdict(report.read(self.dir / "f.jsonl"), enforce_budgets=False), 1)

    def test_a_report_without_a_summary_is_incomplete(self):
        cut = self.dir / "cut.jsonl"
        cut.write_text("".join(self.path.read_text().splitlines(True)[:2]) + '{"not json\n')
        lines = report.read(cut)
        self.assertEqual(len(lines), 2)                      # the bad line is dropped, not fatal
        self.assertEqual(report.verdict(lines, enforce_budgets=False), 3)
        self.assertIn("incomplete", report.render_md(lines))


if __name__ == "__main__":
    unittest.main()
