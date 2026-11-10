import socket
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import devguard  # noqa: E402


class DevguardTests(unittest.TestCase):
    def test_disk_floor_above_free_space_fails(self):
        ok, line = devguard.disk_ok("/", floor_gb=10**9)
        self.assertFalse(ok)
        self.assertIn("floor", line)

    def test_disk_floor_zero_passes(self):
        self.assertTrue(devguard.disk_ok("/", floor_gb=0)[0])

    def test_scratch_over_cap_fails_and_under_cap_passes(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "blob").write_bytes(b"x" * 2_000_000)
            self.assertFalse(devguard.scratch_ok(Path(d), cap_mb=1)[0])
            self.assertTrue(devguard.scratch_ok(Path(d), cap_mb=3)[0])

    def test_missing_scratch_is_fine(self):
        self.assertTrue(devguard.scratch_ok(Path("/nonexistent/metabot-app"), cap_mb=1)[0])

    def test_port_ranges_parse(self):
        self.assertEqual(devguard.parse_ports("8471-8473,8479"), [8471, 8472, 8473, 8479])

    def test_a_leftover_listener_is_reported(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            s.listen(1)
            port = s.getsockname()[1]
            ok, line = devguard.procs_ok(ports=[port], tmux_socket="mb-test-devguard-none")
            self.assertFalse(ok)
            self.assertIn(str(port), line)
        self.assertTrue(devguard.procs_ok(ports=[port], tmux_socket="mb-test-devguard-none")[0])


if __name__ == "__main__":
    unittest.main()
