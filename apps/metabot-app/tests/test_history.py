"""Section 9: append-only history."""
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from metabot_app import history  # noqa: E402

SRC = (Path(__file__).resolve().parents[1] / "src/metabot_app/history.py").read_text()


class HistoryTests(unittest.TestCase):
    def test_no_update_or_delete_in_module(self):
        self.assertIsNone(re.search(r"\b(UPDATE|DELETE|DROP|REPLACE|TRUNCATE|ALTER)\b", SRC))
        stmts = re.findall(r'"((?:CREATE|INSERT|SELECT)[^"]*)', SRC)
        self.assertTrue(stmts)

    def test_append_and_recent(self):
        with tempfile.TemporaryDirectory() as d:
            st = history.Store(Path(d) / "h.sqlite")
            self.assertEqual(st.recent(), [])
            a = st.append("spawn", desk="d1", data={"kind": "shell"})
            b = st.append("verify", run_id="r1", data={"stdout": "x" * 5000})
            self.assertEqual((a, b), (1, 2))
            rows = st.recent(10)
            self.assertEqual([r["id"] for r in rows], [2, 1])
            self.assertEqual(rows[1], {"id": 1, "ts": rows[1]["ts"], "kind": "spawn", "desk": "d1",
                                       "run_id": None, "data": {"kind": "shell"}})
            self.assertEqual(len(rows[0]["data"]["stdout"]), 2048)
            self.assertEqual(len(st.recent(1)), 1)
            with self.assertRaises(ValueError):
                st.append("bogus")
            big = st.append("cli_read", data={"blob": "y" * 9000})
            self.assertEqual(st.recent(1)[0]["data"], {"truncated": True, "keys": ["blob"]})
            self.assertEqual(big, 3)

    def test_kinds(self):
        self.assertEqual(set(history.KINDS), {"spawn", "run", "kill", "verify", "account", "cli_read",
                                              "cli_desk", "modules_reload", "device_paired", "device_revoked",
                                              "folder_added", "folder_removed"})


if __name__ == "__main__":
    unittest.main()
