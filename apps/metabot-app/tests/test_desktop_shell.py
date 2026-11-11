"""D2: the desktop shell's crate is shaped so CI's bundle job can build and prove it.

The Tauri build itself needs the webkit libraries and minutes, so it runs in CI alone;
here the crate, its config, its pages, its icons, the Makefile target, the CI job and the
smoke script are checked for the things a wrong edit would break silently.
"""

import json
import re
import struct
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
DESKTOP = APP / "desktop"
TAURI = DESKTOP / "src-tauri"
WORKFLOW = APP.parents[1] / ".github" / "workflows" / "metabot-app.yml"


def png_size(path):
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", path
    w, h = struct.unpack(">II", data[16:24])
    return w, h


class DesktopShellTests(unittest.TestCase):
    def test_the_tauri_crate_depends_on_the_shell_core_by_path_and_builds_one_binary(self):
        cargo = (TAURI / "Cargo.toml").read_text()
        self.assertIn('metabot-shell-core = { path = "../shell-core" }', cargo)
        self.assertIn('tauri = { version = "2"', cargo)
        self.assertIn('tauri-build = { version = "2"', cargo)
        self.assertIn('name = "metabot-desktop"', cargo)
        self.assertIn("publish = false", cargo)
        self.assertEqual((TAURI / "build.rs").read_text().strip(), "fn main() {\n    tauri_build::build()\n}")

    def test_the_config_declares_no_static_window_and_serves_the_local_pages(self):
        conf = json.loads((TAURI / "tauri.conf.json").read_text())
        self.assertEqual(conf["app"]["windows"], [])   # the window is made in setup, on the page URL
        self.assertEqual(conf["build"]["frontendDist"], "../ui")
        self.assertEqual(conf["identifier"], "ai.metaorcha.metabot.desktop")
        self.assertEqual(conf["bundle"]["targets"], ["deb", "appimage"])
        for name in ("index.html", "missing.html"):
            self.assertTrue((DESKTOP / "ui" / name).is_file(), name)
        self.assertIn("uv tool install metaorcha-app", (DESKTOP / "ui" / "missing.html").read_text())
        cap = json.loads((TAURI / "capabilities" / "default.json").read_text())
        self.assertEqual(cap["windows"], ["main"])
        self.assertEqual(cap["permissions"], ["core:default"])

    def test_every_icon_the_config_names_is_a_png_of_the_size_its_name_says(self):
        conf = json.loads((TAURI / "tauri.conf.json").read_text())
        sizes = {"32x32.png": 32, "128x128.png": 128, "128x128@2x.png": 256, "icon.png": 512}
        self.assertEqual(sorted(Path(i).name for i in conf["bundle"]["icon"]), sorted(sizes))
        for rel in conf["bundle"]["icon"]:
            self.assertEqual(png_size(TAURI / rel), (sizes[Path(rel).name],) * 2, rel)

    def test_the_window_opens_the_page_url_or_the_missing_page_and_detaches_a_started_server(self):
        main = (TAURI / "src" / "main.rs").read_text()
        self.assertIn("use metabot_shell_core::{config_path, page_url, plan, state_dir, wait_for_server, Plan};", main)
        self.assertIn('tauri::WebviewUrl::External(u.parse()?)', main)
        self.assertIn('tauri::WebviewUrl::App("missing.html".into())', main)
        self.assertIn('WebviewWindowBuilder::new(app, "main", target)', main)
        self.assertIn(".process_group(0)", main)   # the server outlives the window
        self.assertIn('.arg("serve")', main)

    def test_the_smoke_mode_prints_the_port_and_never_the_token(self):
        main = (TAURI / "src" / "main.rs").read_text()
        smoke = main[main.index('if env("METABOT_SHELL_SMOKE")'):]
        smoke = smoke[: smoke.index("std::process::exit")]
        self.assertIn('println!("{how} port={port}");', smoke)
        self.assertNotIn("token", smoke.replace("the token stays off every terminal and log", ""))
        script = (APP / "tools" / "desktop-smoke.sh").read_text()
        self.assertIn('[ "$out" = "start port=8499" ]', script)
        self.assertIn('[ "$out" = "attach port=8499" ]', script)
        self.assertIn('[ "$out" = "missing port=-" ] && [ "$code" -eq 3 ]', script)
        self.assertNotIn("pkill -f", script)

    def test_the_make_target_and_the_ci_job_agree_with_the_guide(self):
        makefile = (APP / "Makefile").read_text()
        self.assertRegex(makefile, r"(?m)^desktop-test:\n\tcargo test --manifest-path desktop/shell-core/Cargo.toml\n")
        wf = WORKFLOW.read_text()
        job = wf[wf.index("  bundle-linux:"):wf.index("  wheelhouse-linux:")]
        self.assertIn("make -C apps/metabot-app desktop-test", job)
        self.assertIn("contains(github.event.pull_request.labels.*.name, 'bundle-ci')", job)
        self.assertIn("github.event_name == 'workflow_dispatch'", job)
        self.assertIn("cargo tauri build --bundles deb,appimage", job)
        self.assertIn("xvfb-run -a apps/metabot-app/tools/desktop-smoke.sh", job)
        for action in re.findall(r"uses: (\S+)", job):
            self.assertIn(action, wf[:wf.index("  bundle-linux:")], f"{action} is not a pin the other jobs use")
        guide = (APP / "CONTRIBUTING-linux.md").read_text()
        self.assertIn("CI's `bundle-linux` job runs exactly `make -C apps/metabot-app desktop-test`", guide)


if __name__ == "__main__":
    unittest.main()
