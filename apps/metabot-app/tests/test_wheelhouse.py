"""D3: ``packaging/wheelhouse.py`` builds the sdk, core and app wheels and downloads their
pinned dependencies as wheels only, for one platform, and writes a ``requirements.lock``
with a sha256 per file. These tests hold what needs no network: the lock, the sdist
refusal, the commands it would run. CI runs the real thing."""
import hashlib
import io
import contextlib
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packaging"))
import wheelhouse  # noqa: E402


def fake_wheel(folder: Path, name: str, body: bytes) -> Path:
    p = folder / name
    p.write_bytes(body)
    return p


class LockTests(unittest.TestCase):
    def setUp(self):
        self.out = Path(tempfile.mkdtemp(prefix="mb-wheels-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.out, ignore_errors=True))

    def test_the_lock_lists_every_wheel_with_its_hash_sorted_by_name(self):
        fake_wheel(self.out, "orcha_metabot-0.3.0-py3-none-any.whl", b"core")
        fake_wheel(self.out, "anyio-4.14.2-py3-none-any.whl", b"anyio")
        fake_wheel(self.out, "cryptography-50.0.0-cp311-abi3-manylinux_2_28_x86_64.whl", b"crypto")
        text = wheelhouse.lock_text(self.out)
        self.assertEqual(text, (
            f"anyio==4.14.2 \\\n    --hash=sha256:{hashlib.sha256(b'anyio').hexdigest()}\n"
            f"cryptography==50.0.0 \\\n    --hash=sha256:{hashlib.sha256(b'crypto').hexdigest()}\n"
            f"orcha-metabot==0.3.0 \\\n    --hash=sha256:{hashlib.sha256(b'core').hexdigest()}\n"))

    def test_anything_that_is_not_a_wheel_is_refused(self):
        fake_wheel(self.out, "anyio-4.14.2-py3-none-any.whl", b"anyio")
        fake_wheel(self.out, "pyyaml-6.0.3.tar.gz", b"sdist")
        with self.assertRaises(wheelhouse.WheelhouseError) as cm:
            wheelhouse.refuse_sdists(self.out)
        self.assertIn("pyyaml-6.0.3.tar.gz", str(cm.exception))
        with self.assertRaises(wheelhouse.WheelhouseError):
            wheelhouse.lock_text(self.out)

    def test_write_lock_puts_the_file_beside_the_wheels(self):
        fake_wheel(self.out, "anyio-4.14.2-py3-none-any.whl", b"anyio")
        path = wheelhouse.write_lock(self.out)
        self.assertEqual(path, self.out / "requirements.lock")
        self.assertTrue(path.read_text().startswith("anyio==4.14.2"))

    def test_a_wheel_name_is_parsed_and_normalised(self):
        self.assertEqual(wheelhouse.name_version("Orcha_SDK-0.2.2-py3-none-any.whl"), ("orcha-sdk", "0.2.2"))
        with self.assertRaises(wheelhouse.WheelhouseError):
            wheelhouse.name_version("not-a-wheel.whl")


class CommandTests(unittest.TestCase):
    def test_the_download_is_wheels_only_for_the_named_platform(self):
        argv = wheelhouse.download_argv("linux-x86_64", "3.12", Path("/w"), Path("/r.txt"))
        self.assertIn("--only-binary=:all:", argv)
        self.assertIn("manylinux_2_28_x86_64", argv)
        self.assertIn("3.12", argv)
        self.assertIn("/w", argv)
        self.assertIn("/r.txt", argv)
        argv = wheelhouse.download_argv("macos-arm64", "3.12", Path("/w"), Path("/r.txt"))
        self.assertIn("macosx_11_0_arm64", argv)
        with self.assertRaises(wheelhouse.WheelhouseError):
            wheelhouse.download_argv("windows-x64", "3.12", Path("/w"), Path("/r.txt"))

    def test_the_three_packages_are_built_as_wheels(self):
        root = Path("/repo")
        argvs = wheelhouse.build_argvs(root, Path("/w"))
        self.assertEqual(len(argvs), 3)
        for argv in argvs:
            self.assertEqual(argv[:3], ["uv", "build", "--wheel"])
            self.assertIn("/w", argv)
        self.assertTrue(any(a[-1].endswith("/sdk") for a in argvs))
        self.assertTrue(any(a[-1].endswith("/apps/metabot") for a in argvs))
        self.assertTrue(any(a[-1].endswith("/apps/metabot-app") for a in argvs))

    def test_dry_run_prints_the_commands_and_touches_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = wheelhouse.main(["--platform", "linux-x86_64", "--out", d + "/w", "--dry-run"])
            self.assertEqual(rc, 0, err.getvalue())
            self.assertIn("uv build --wheel", out.getvalue())
            self.assertIn("--only-binary=:all:", out.getvalue())
            self.assertFalse(Path(d, "w").exists())
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                rc = wheelhouse.main(["--platform", "windows-x64", "--out", d + "/w", "--dry-run"])
            self.assertEqual(rc, 2)
            self.assertIn("platform", err.getvalue())


if __name__ == "__main__":
    unittest.main()
