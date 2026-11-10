"""Traversal ids are refused with 400 on every id route."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))
from harness import Env  # noqa: E402

BAD = ["../x", "a/b", ".hidden", "a" * 129, "a..b", ""]


class IdTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = Env().start()

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()

    def test_run_and_receipt_routes(self):
        from urllib.parse import quote
        for bad in BAD:
            q = quote(bad, safe="")
            for route in ("/api/mb/run/", "/api/mb/receipt/"):
                s, j = self.env.get(route + q)
                self.assertEqual((s, j), (400, {"ok": False, "error": "bad id"}), (route, bad))

    def test_verify_route(self):
        for bad in BAD + [None, 5, ["x"]]:
            s, j = self.env.post("/api/mb/verify", {"id": bad})
            self.assertEqual((s, j), (400, {"ok": False, "error": "bad id"}), bad)

    def test_catalog_receipt_param(self):
        for bad in BAD:
            s, j = self.env.post("/api/cli/read", {"id": "receipts.show", "params": {"run": bad}})
            self.assertEqual(s, 400, bad)
            self.assertTrue(j["error"].startswith("bad run") or j["error"].startswith("missing"), j)


if __name__ == "__main__":
    unittest.main()
