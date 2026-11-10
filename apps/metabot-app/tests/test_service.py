"""D4: ``metabot-app service install|remove|status`` keeps the server running across
logins: a systemd user unit on Linux, a launchd agent on macOS. The rendered texts are
golden; nothing in these tests calls systemctl or launchctl."""

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))
from metabot_app import cli, service  # noqa: E402

PY = "/opt/mb/venv/bin/python3"
PATH = "/opt/mb/venv/bin:/usr/local/bin:/usr/bin:/bin"


class RenderTests(unittest.TestCase):
    def test_the_systemd_unit_is_the_golden_text(self):
        text = service.unit_text(PY, PATH)
        self.assertEqual(
            text,
            (
                "[Unit]\n"
                "Description=MetaBot, the app: the local server the page, the phone and the desks use\n"
                "After=network.target\n"
                "\n"
                "[Service]\n"
                f"ExecStart={PY} -m metabot_app serve\n"
                f"Environment=PATH={PATH}\n"
                "Restart=on-failure\n"
                "RestartSec=3\n"
                "KillMode=process\n"
                "\n"
                "[Install]\n"
                "WantedBy=default.target\n"
            ),
        )

    def test_the_launchd_plist_is_the_golden_text(self):
        text = service.plist_text(
            PY, PATH, Path("/Users/me/Library/Logs/metabot-app.log")
        )
        self.assertTrue(text.startswith('<?xml version="1.0" encoding="UTF-8"?>\n'))
        self.assertIn(
            "<key>Label</key>\n    <string>ai.metaorcha.metabot-app</string>", text
        )
        self.assertIn(
            f"<string>{PY}</string>\n        <string>-m</string>\n        <string>metabot_app</string>\n        <string>serve</string>",
            text,
        )
        self.assertIn(f"<key>PATH</key>\n        <string>{PATH}</string>", text)
        self.assertIn("<key>AbandonProcessGroup</key>\n    <true/>", text)
        self.assertIn("<key>KeepAlive</key>\n    <true/>", text)
        self.assertIn("<string>/Users/me/Library/Logs/metabot-app.log</string>", text)

    def test_a_path_with_xml_or_unit_specials_is_escaped(self):
        self.assertIn("&amp;", service.plist_text(PY, "/a&b", Path("/l")))
        self.assertIn("Environment=PATH=/a%%b", service.unit_text(PY, "/a%b"))


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="mb-service-"))
        self.addCleanup(
            lambda: __import__("shutil").rmtree(self.home, ignore_errors=True)
        )
        self.calls = []

        def fake_run(argv):
            self.calls.append(list(argv))
            return 0, "active\n" if argv[-2:] == ["is-active", service.UNIT] else ""

        for p in (
            mock.patch.object(service, "_run", side_effect=fake_run),
            mock.patch.object(service.Path, "home", return_value=self.home),
            mock.patch.object(service, "_python", return_value=PY),
            mock.patch.object(service, "_path", return_value=PATH),
        ):
            p.start()
            self.addCleanup(p.stop)

    def test_install_writes_the_unit_and_enables_it(self):
        out = service.install("systemd")
        self.assertEqual(
            out, self.home / ".config" / "systemd" / "user" / "metabot-app.service"
        )
        self.assertEqual(out.read_text(), service.unit_text(PY, PATH))
        self.assertEqual(out.stat().st_mode & 0o777, 0o644)
        self.assertEqual(
            self.calls,
            [
                ["systemctl", "--user", "daemon-reload"],
                ["systemctl", "--user", "enable", "--now", "metabot-app.service"],
            ],
        )

    def test_install_writes_the_plist_and_loads_it(self):
        out = service.install("launchd")
        self.assertEqual(
            out,
            self.home / "Library" / "LaunchAgents" / "ai.metaorcha.metabot-app.plist",
        )
        self.assertIn("<string>ai.metaorcha.metabot-app</string>", out.read_text())
        self.assertEqual(
            self.calls,
            [["launchctl", "unload", str(out)], ["launchctl", "load", "-w", str(out)]],
        )

    def test_remove_disables_then_deletes_and_is_quiet_when_absent(self):
        service.install("systemd")
        self.calls.clear()
        self.assertTrue(service.remove("systemd"))
        self.assertFalse(
            (
                self.home / ".config" / "systemd" / "user" / "metabot-app.service"
            ).exists()
        )
        self.assertEqual(
            self.calls,
            [
                ["systemctl", "--user", "disable", "--now", "metabot-app.service"],
                ["systemctl", "--user", "daemon-reload"],
            ],
        )
        self.calls.clear()
        self.assertFalse(service.remove("systemd"))
        self.assertEqual(self.calls, [])

    def test_status_says_installed_and_active(self):
        self.assertEqual(
            service.status("systemd"),
            {
                "kind": "systemd",
                "installed": False,
                "active": "",
                "path": str(
                    self.home / ".config" / "systemd" / "user" / "metabot-app.service"
                ),
            },
        )
        service.install("systemd")
        st = service.status("systemd")
        self.assertTrue(st["installed"])
        self.assertEqual(st["active"], "active")

    def test_auto_picks_the_host_and_refuses_an_unknown_one(self):
        with mock.patch.object(service.sys, "platform", "linux"):
            self.assertEqual(service.kind_for("auto"), "systemd")
        with mock.patch.object(service.sys, "platform", "darwin"):
            self.assertEqual(service.kind_for("auto"), "launchd")
        with mock.patch.object(service.sys, "platform", "win32"):
            with self.assertRaises(service.ServiceError):
                service.kind_for("auto")
        with self.assertRaises(service.ServiceError):
            service.kind_for("cron")

    def test_the_cli_installs_shows_status_and_removes(self):
        def run_cli(*argv):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = cli.main(list(argv))
            return rc, out.getvalue(), err.getvalue()

        rc, out, _ = run_cli("service", "install", "--kind", "systemd")
        self.assertEqual(rc, 0)
        self.assertIn("metabot-app.service", out)
        self.assertIn("systemctl --user status metabot-app.service", out)
        rc, out, _ = run_cli("service", "status", "--kind", "systemd")
        self.assertEqual(rc, 0)
        self.assertIn("installed", out)
        self.assertIn("active", out)
        rc, out, _ = run_cli("service", "remove", "--kind", "systemd")
        self.assertEqual(rc, 0)
        self.assertIn("removed", out)
        rc, _, err = run_cli("service", "remove", "--kind", "systemd")
        self.assertEqual(rc, 1)
        self.assertIn("not installed", err)
        with mock.patch.object(service.sys, "platform", "win32"):
            rc, _, err = run_cli("service", "install")
        self.assertEqual(rc, 2)
        self.assertIn("no service manager", err)


if __name__ == "__main__":
    unittest.main()
