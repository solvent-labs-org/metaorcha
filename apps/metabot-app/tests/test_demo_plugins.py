"""The demo marketplace under plugins/ and the scripts under tools/demo/.

Each plugin is proven the way MetaBot runs it: the bridges on a job file, the
MCP server over stdio, with fakes on PATH for everything that would touch the
screen or a model. No display, no network beyond loopback, no vendor CLI.
"""

import http.server
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
PLUGINS = APP / "plugins"
DEMO = APP / "tools" / "demo"
TOKEN = "T0K3N-never-printed"
NAMES = ("watchdog", "monitor", "local-harness", "browser")


def _run(argv, env, stdin=None, timeout=60):
    return subprocess.run(argv, input=stdin, capture_output=True, text=True, timeout=timeout,
                          env=env, check=False)


def _events(stdout):
    out = []
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            out.append(json.loads(line))
    return out


def _env(**extra):
    env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
    env.update(extra)
    return env


class _Fake(http.server.ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


class _AppHandler(http.server.BaseHTTPRequestHandler):
    """A stand-in for the app server: the routes the watchdog reads, token-gated."""

    routes = {}
    seen = []

    def log_message(self, *_a):
        pass

    def do_GET(self):
        path, _, query = self.path.partition("?")
        self.seen.append((path, query, self.headers.get("X-MetaChat")))
        if f"t={TOKEN}" not in query or self.headers.get("X-MetaChat") != "1":
            self.send_response(401)
            self.end_headers()
            return
        body = self.routes.get(path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def healthy_routes():
    return {
        "/api/meta": {"build": "t1", "caps": {"desks": True, "sandbox": True, "phone_listener": True,
                                              "engines": {"claude": "cli", "openai": "http"}}},
        "/api/devices": {"devices": [{"name": "Tablet", "scope": "phone", "created": "1", "seen": "9999999999"}]},
        "/api/desks": {"desks": [{"name": "run-a", "pane": True, "cli": "shell", "needs_you": False},
                                 {"name": "old", "pane": False, "cli": "", "needs_you": False}]},
        "/api/mb/runs": {"runs": [{"run_id": "mb-1", "state": "done", "receipt_state": "valid"},
                                  {"run_id": "mb-2", "state": "live", "receipt_state": None}]},
        "/api/accounts": {"claude": [{"name": "a", "ready": True}], "cursor": [{"name": "t", "ready": False}],
                          "defaults": {"claude": {"ready": False}, "cursor": {"ready": True}}},
        "/api/doctor": {"ok": True, "items": [{"id": "py", "ok": True, "blocker": False, "what": "Python"}]},
    }


class _WithFakeApp(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="demo-plugins-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        _AppHandler.routes = healthy_routes()
        _AppHandler.seen = []
        self.srv = _Fake(("127.0.0.1", 0), _AppHandler)
        self.port = self.srv.server_address[1]
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)

    def state(self, pid=None):
        sd = self.tmp / "state"
        sd.mkdir(exist_ok=True)
        (sd / "server.json").write_text(json.dumps({"pid": pid or os.getpid(), "port": self.port, "build": "t1"}))
        (sd / "token").write_text(TOKEN + "\n")
        return sd

    def job(self, prompt="watch", **more):
        job = {"format": "metabot.bridge/1", "prompt": prompt, "model": None, "cwd": str(self.tmp),
               "read_only": True, "switchboard": None, "resume": None}
        job.update(more)
        p = self.tmp / "bridge-job.json"
        p.write_text(json.dumps(job))
        return str(p)

    def watchdog(self, sd, prompt="watch"):
        r = _run([sys.executable, str(PLUGINS / "watchdog" / "bin" / "watchdog"), self.job(prompt)],
                 _env(METACHAT_STATE=str(sd), HOME=str(self.tmp)))
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout, _events(r.stdout)


class MarketplaceTests(unittest.TestCase):
    def test_the_marketplace_lists_every_plugin_folder_and_each_has_a_manifest(self):
        market = json.loads((PLUGINS / ".claude-plugin" / "marketplace.json").read_text())
        listed = {p["name"]: p["source"] for p in market["plugins"]}
        folders = sorted(p.name for p in PLUGINS.iterdir() if p.is_dir() and not p.name.startswith("."))
        self.assertEqual(sorted(listed), folders)
        for name, source in listed.items():
            self.assertEqual(source, f"./{name}")
            manifest = json.loads((PLUGINS / name / ".claude-plugin" / "plugin.json").read_text())
            self.assertEqual(manifest["name"], name)
            self.assertTrue(manifest.get("description"))
            self.assertTrue((PLUGINS / name / "README.md").is_file())

    def test_each_bridge_spec_names_an_executable_the_plugin_holds_and_honours_read_only(self):
        specs = sorted(PLUGINS.glob("*/metabot/bridges/*.yaml"))
        self.assertEqual([s.stem for s in specs], ["local-harness", "watchdog"])
        for spec in specs:
            text = spec.read_text()
            m = re.search(r"^command:\s*\[([^\]]+)\]", text, re.M)
            self.assertIsNotNone(m, spec)
            program = PLUGINS / spec.parents[2].name / m.group(1).split(",")[0].strip()
            self.assertTrue(program.is_file(), program)
            self.assertTrue(program.stat().st_mode & stat.S_IXUSR, program)
            self.assertIn("read_only: honored", text)
            self.assertIn("tools: own", text)

    def test_the_monitor_server_is_addressed_by_the_plugin_root_so_the_installed_copy_runs(self):
        servers = json.loads((PLUGINS / "monitor" / ".mcp.json").read_text())["mcpServers"]
        self.assertEqual(list(servers), ["monitor"])
        self.assertEqual(servers["monitor"]["args"], ["${CLAUDE_PLUGIN_ROOT}/bin/monitor-mcp"])


class WatchdogTests(_WithFakeApp):
    def test_a_healthy_app_reads_ok_with_one_step_per_probe_and_the_token_never_printed(self):
        stdout, ev = self.watchdog(self.state())
        self.assertNotIn(TOKEN, stdout)
        self.assertEqual(ev[0]["type"], "started")
        calls = [e for e in ev if e["type"] == "tool_call"]
        results = [e for e in ev if e["type"] == "tool_result"]
        self.assertEqual([c["tool"] for c in calls],
                         ["watchdog/meta", "watchdog/devices", "watchdog/desks", "watchdog/runs",
                          "watchdog/accounts", "watchdog/doctor", "watchdog/machine"])
        self.assertEqual([c["call_id"] for c in calls], [r["call_id"] for r in results])
        self.assertTrue(all(r["ok"] for r in results), results)
        self.assertNotIn("?t=", json.dumps(calls))
        self.assertNotIn("&t=", json.dumps(calls))
        final = ev[-1]
        self.assertEqual(final["type"], "final")
        self.assertTrue(final["text"].startswith("WATCHDOG OK\n"), final["text"])
        self.assertIn("devices: 1 paired; Tablet seen", final["text"])
        self.assertIn("desks: 1 live of 2 (shell 1)", final["text"])
        self.assertIn("runs (last 2): done 1, live 1", final["text"])
        self.assertIn("accounts: claude 1/1 ready, cursor 0/1 ready; defaults ready: cursor", final["text"])
        self.assertIn("server pid alive", final["text"])
        self.assertTrue(all(h == "1" for _p, _q, h in _AppHandler.seen))

    def test_an_interrupted_run_a_desk_that_needs_you_and_a_blocker_are_findings(self):
        _AppHandler.routes["/api/mb/runs"]["runs"].append({"run_id": "mb-9", "state": "interrupted"})
        _AppHandler.routes["/api/desks"]["desks"][0]["needs_you"] = True
        _AppHandler.routes["/api/doctor"]["items"].append({"id": "k", "ok": False, "blocker": True, "what": "no key"})
        _stdout, ev = self.watchdog(self.state())
        text = ev[-1]["text"]
        self.assertTrue(text.startswith("WATCHDOG ATTENTION (3)\n"), text)
        self.assertIn("- run mb-9 was interrupted", text)
        self.assertIn("- desk run-a needs you", text)
        self.assertIn("- doctor: no key", text)

    def test_a_server_that_does_not_answer_is_one_finding_and_the_other_probes_are_skipped(self):
        sd = self.state()
        self.srv.shutdown()
        self.srv.server_close()
        _stdout, ev = self.watchdog(sd)
        calls = [e["tool"] for e in ev if e["type"] == "tool_call"]
        self.assertEqual(calls, ["watchdog/meta", "watchdog/machine"])
        self.assertFalse([e for e in ev if e["type"] == "tool_result"][0]["ok"])
        self.assertIn("WATCHDOG ATTENTION (1)", ev[-1]["text"])

    def test_json_in_the_prompt_gets_the_report_as_json(self):
        _stdout, ev = self.watchdog(self.state(), prompt="watch, as json")
        rep = json.loads(ev[-1]["text"])
        self.assertEqual(rep["verdict"], "WATCHDOG OK")
        self.assertEqual(rep["findings"], [])
        self.assertEqual(len(rep["probes"]), 7)

    def test_no_recorded_server_is_said_plainly(self):
        sd = self.tmp / "empty-state"
        sd.mkdir()
        _stdout, ev = self.watchdog(sd)
        self.assertEqual([e["type"] for e in ev], ["started", "final"])
        self.assertIn("no app server is recorded", ev[-1]["text"])


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="demo-monitor-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.fake = self.tmp / "bin"
        self.fake.mkdir()
        self.log = self.tmp / "calls.log"
        for name, body in {
            "wmctrl": 'echo "0x01 0 host Editor - notes.txt"; echo "0x02 1 host Browser"',
            "scrot": 'for a in "$@"; do f="$a"; done; printf PNG > "$f"',
            "google-chrome": "exit 0",
            "notify-send": "exit 0",
        }.items():
            p = self.fake / name
            p.write_text(f'#!/bin/sh\necho "{name} $*" >> "{self.log}"\n{body}\n')
            p.chmod(0o755)
        (self.tmp / "tmp").mkdir()

    def talk(self, messages, with_xdotool=False):
        if with_xdotool:
            p = self.fake / "xdotool"
            p.write_text(f'#!/bin/sh\necho "xdotool $*" >> "{self.log}"\n')
            p.chmod(0o755)
        lines = [json.dumps({"jsonrpc": "2.0", "id": 0, "method": "initialize",
                             "params": {"protocolVersion": "2025-03-26", "capabilities": {}}}),
                 json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})]
        for i, (method, params) in enumerate(messages, 1):
            lines.append(json.dumps({"jsonrpc": "2.0", "id": i, "method": method, "params": params}))
        r = _run([sys.executable, str(PLUGINS / "monitor" / "bin" / "monitor-mcp")],
                 {"PATH": str(self.fake), "HOME": str(self.tmp), "TMPDIR": str(self.tmp / "tmp"), "LANG": "C.UTF-8"},
                 stdin="\n".join(lines) + "\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        replies = {m["id"]: m for m in (json.loads(x) for x in r.stdout.splitlines() if x.strip())}
        return replies

    def test_it_initializes_with_the_clients_protocol_and_lists_ten_classed_tools(self):
        rep = self.talk([("tools/list", {})])
        self.assertEqual(rep[0]["result"]["protocolVersion"], "2025-03-26")
        self.assertEqual(rep[0]["result"]["serverInfo"]["name"], "monitor")
        tools = {t["name"]: t for t in rep[1]["result"]["tools"]}
        self.assertEqual(sorted(tools), sorted(["screen_look", "windows", "show_url", "show_text", "focus_window",
                                                "close_window", "type_text", "press_key", "click", "mouse_move"]))
        for name, t in tools.items():
            self.assertIn("readOnlyHint", t["annotations"], name)
            self.assertIn("destructiveHint", t["annotations"], name)
        self.assertTrue(tools["screen_look"]["annotations"]["readOnlyHint"])
        self.assertTrue(tools["close_window"]["annotations"]["destructiveHint"])
        self.assertFalse(tools["show_text"]["annotations"]["destructiveHint"])

    def test_look_and_windows_read_the_screen_through_scrot_and_wmctrl_with_a_display(self):
        rep = self.talk([("tools/call", {"name": "screen_look", "arguments": {}}),
                         ("tools/call", {"name": "windows", "arguments": {}})])
        look = rep[1]["result"]
        self.assertFalse(look["isError"])
        self.assertIn("2 windows:", look["content"][0]["text"])
        self.assertIn("- Editor - notes.txt", look["content"][0]["text"])
        shot = re.search(r"screenshot (\S+) \((\d+) bytes\)", look["content"][0]["text"])
        self.assertIsNotNone(shot)
        self.assertTrue(shot.group(1).startswith(str(self.tmp / "tmp")), shot.group(1))
        self.assertEqual(shot.group(2), "3")
        wins = json.loads(rep[2]["result"]["content"][0]["text"])
        self.assertEqual([w["title"] for w in wins], ["Editor - notes.txt", "Browser"])

    def test_show_url_opens_only_pages_and_files_in_a_new_browser_window(self):
        page = self.tmp / "card.html"
        page.write_text("<p>hi</p>")
        rep = self.talk([("tools/call", {"name": "show_url", "arguments": {"url": "javascript:alert(1)"}}),
                         ("tools/call", {"name": "show_url", "arguments": {"url": "https://example.com/x"}}),
                         ("tools/call", {"name": "show_url", "arguments": {"url": str(page)}}),
                         ("tools/call", {"name": "show_url", "arguments": {"url": str(self.tmp / "missing.html")}})])
        self.assertTrue(rep[1]["result"]["isError"])
        self.assertIn("only http(s) URLs and local files", rep[1]["result"]["content"][0]["text"])
        self.assertFalse(rep[2]["result"]["isError"])
        self.assertFalse(rep[3]["result"]["isError"])
        self.assertTrue(rep[4]["result"]["isError"])
        log = self.log.read_text()
        self.assertIn("google-chrome --new-window https://example.com/x", log)
        self.assertIn(f"google-chrome --new-window {page}", log)
        self.assertNotIn("javascript:", log)

    def test_show_text_writes_an_escaped_card_notifies_and_shows_it(self):
        rep = self.talk([("tools/call", {"name": "show_text", "arguments": {"title": "Hi <b>", "text": "a & b\nline 2"}})])
        text = rep[1]["result"]["content"][0]["text"]
        self.assertFalse(rep[1]["result"]["isError"])
        self.assertIn("notified, card shown: ", text)
        card = Path(text.split(": ", 1)[1])
        html = card.read_text()
        self.assertIn("Hi &lt;b&gt;", html)
        self.assertIn("a &amp; b\nline 2", html)
        self.assertIn("notify-send Hi <b> a & b", self.log.read_text())

    def test_typing_and_clicking_need_xdotool_and_say_so_without_it(self):
        rep = self.talk([("tools/call", {"name": "type_text", "arguments": {"text": "hello"}}),
                         ("tools/call", {"name": "click", "arguments": {"x": 10, "y": 20}})])
        for i in (1, 2):
            self.assertTrue(rep[i]["result"]["isError"])
            self.assertIn("xdotool is not installed", rep[i]["result"]["content"][0]["text"])
        rep = self.talk([("tools/call", {"name": "type_text", "arguments": {"text": "hello"}}),
                         ("tools/call", {"name": "click", "arguments": {"x": 10, "y": 20, "button": 3}}),
                         ("tools/call", {"name": "press_key", "arguments": {"key": "ctrl+l"}}),
                         ("tools/call", {"name": "press_key", "arguments": {"key": "rm -rf"}}),
                         ("tools/call", {"name": "click", "arguments": {"x": "10", "y": 20}})],
                        with_xdotool=True)
        self.assertEqual(rep[1]["result"]["content"][0]["text"], "typed 5 characters")
        self.assertEqual(rep[2]["result"]["content"][0]["text"], "clicked button 3 at 10,20")
        self.assertEqual(rep[3]["result"]["content"][0]["text"], "pressed ctrl+l")
        self.assertTrue(rep[4]["result"]["isError"])
        self.assertTrue(rep[5]["result"]["isError"])
        log = self.log.read_text()
        self.assertIn("xdotool type --delay 20 -- hello", log)
        self.assertIn("xdotool mousemove 10 20 click 3", log)

    def test_focus_and_close_go_through_wmctrl_and_unknown_tools_or_methods_are_errors(self):
        rep = self.talk([("tools/call", {"name": "focus_window", "arguments": {"title": "Editor"}}),
                         ("tools/call", {"name": "close_window", "arguments": {"title": "Browser"}}),
                         ("tools/call", {"name": "nope", "arguments": {}}),
                         ("ping", {}),
                         ("resources/list", {})])
        self.assertFalse(rep[1]["result"]["isError"])
        self.assertFalse(rep[2]["result"]["isError"])
        self.assertIn("wmctrl -a Editor", self.log.read_text())
        self.assertIn("wmctrl -c Browser", self.log.read_text())
        self.assertEqual(rep[3]["error"]["code"], -32602)
        self.assertEqual(rep[4]["result"], {})
        self.assertEqual(rep[5]["error"]["code"], -32601)


class _ModelHandler(http.server.BaseHTTPRequestHandler):
    """A stand-in for an OpenAI-compatible server: a scripted list of turns."""

    turns = []
    requests = []

    def log_message(self, *_a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        self.requests.append(body)
        turn = self.turns.pop(0) if self.turns else {"content": "no more turns"}
        msg = {"role": "assistant", "content": turn.get("content", "")}
        if turn.get("calls"):
            msg["tool_calls"] = [{"id": f"c{i}", "type": "function",
                                  "function": {"name": c[0], "arguments": json.dumps(c[1])}}
                                 for i, c in enumerate(turn["calls"], 1)]
        data = json.dumps({"choices": [{"message": msg, "finish_reason": "stop"}],
                           "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class LocalHarnessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="demo-harness-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.tmp / "README.md").write_text("A folder with one file.\n")
        _ModelHandler.turns = []
        _ModelHandler.requests = []
        self.srv = _Fake(("127.0.0.1", 0), _ModelHandler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/v1"

    def run_job(self, read_only=True, prompt="what is here?", model=None):
        job = {"format": "metabot.bridge/1", "prompt": prompt, "model": model, "cwd": str(self.tmp),
               "read_only": read_only, "switchboard": None, "resume": None}
        p = self.tmp / "job.json"
        p.write_text(json.dumps(job))
        r = _run([sys.executable, str(PLUGINS / "local-harness" / "bin" / "local-harness"), str(p)],
                 _env(LOCAL_HARNESS_URL=self.url, HOME=str(self.tmp)))
        self.assertEqual(r.returncode, 0, r.stderr)
        return _events(r.stdout)

    def test_it_runs_the_models_tool_calls_reports_each_and_ends_with_the_answer(self):
        _ModelHandler.turns = [{"calls": [("list_dir", {"path": "."}), ("read_file", {"path": "README.md"})]},
                               {"content": "One README that says it is a folder with one file."}]
        ev = self.run_job(model="m-test")
        self.assertEqual(ev[0], {"type": "started", "session_id": "local-harness-1", "model": "m-test"})
        kinds = [e["type"] for e in ev]
        self.assertEqual(kinds, ["started", "usage", "tool_call", "tool_result", "tool_call", "tool_result", "usage", "final"])
        calls = [e for e in ev if e["type"] == "tool_call"]
        self.assertEqual([(c["tool"], c["call_id"]) for c in calls], [("list_dir", "lh1"), ("read_file", "lh2")])
        results = [e for e in ev if e["type"] == "tool_result"]
        self.assertEqual(results[0]["output"], "README.md\njob.json")
        self.assertIn("one file", results[1]["output"])
        self.assertEqual(ev[-1]["text"], "One README that says it is a folder with one file.")
        self.assertEqual(ev[1]["data"]["total_tokens"], 15)
        first = _ModelHandler.requests[0]
        self.assertEqual(first["model"], "m-test")
        self.assertEqual([t["function"]["name"] for t in first["tools"]], ["list_dir", "read_file"])
        self.assertEqual(_ModelHandler.requests[1]["messages"][-1]["role"], "tool")

    def test_a_read_only_job_offers_no_write_tool_and_refuses_one_the_model_calls_anyway(self):
        _ModelHandler.turns = [{"calls": [("write_file", {"path": "x.txt", "content": "hi"})]}, {"content": "done"}]
        ev = self.run_job(read_only=True)
        res = [e for e in ev if e["type"] == "tool_result"][0]
        self.assertFalse(res["ok"])
        self.assertIn("read-only", res["output"])
        self.assertFalse((self.tmp / "x.txt").exists())

    def test_a_writing_job_writes_inside_the_folder_only(self):
        _ModelHandler.turns = [{"calls": [("write_file", {"path": "out/x.txt", "content": "hi"}),
                                          ("write_file", {"path": "../escape.txt", "content": "no"}),
                                          ("read_file", {"path": "/etc/hostname"})]},
                               {"content": "done"}]
        ev = self.run_job(read_only=False)
        results = [e for e in ev if e["type"] == "tool_result"]
        self.assertEqual([r["ok"] for r in results], [True, False, False])
        self.assertEqual((self.tmp / "out" / "x.txt").read_text(), "hi")
        self.assertFalse((self.tmp.parent / "escape.txt").exists())
        self.assertIn("write_file", [t["function"]["name"] for t in _ModelHandler.requests[0]["tools"]])

    def test_a_model_that_does_not_answer_is_an_error_event_not_a_crash(self):
        self.srv.shutdown()
        self.srv.server_close()
        ev = self.run_job()
        self.assertEqual([e["type"] for e in ev], ["started", "error"])
        self.assertIn("did not answer", ev[1]["message"])


class DemoScriptTests(unittest.TestCase):
    """The scripts call `metabot`; a fake on METABOT records the argv they build."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="demo-scripts-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.log = self.tmp / "metabot.log"
        self.fake = self.tmp / "metabot"
        self.fake.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{self.log}"\n'
                             'case "$1 $2" in\n'
                             '  "connect list") echo "monitor-monitor  active   stdio did:x python3 x";;\n'
                             f'  "workflow add") cp "$3" "{self.tmp}/added-$(basename "$3")";;\n'
                             'esac\n')
        self.fake.chmod(0o755)
        self.folder = self.tmp / "work"
        self.folder.mkdir()

    def sh(self, script, *args, **env):
        e = _env(METABOT=str(self.fake), DEMO_FOLDER=str(self.folder), HOME=str(self.tmp), **env)
        r = _run(["/bin/sh", str(DEMO / script), *args], e, timeout=60)
        return r, (self.log.read_text().splitlines() if self.log.exists() else [])

    def test_run_maps_a_cursor_tier_to_its_model_and_defaults_to_read_only(self):
        r, lines = self.sh("run.sh", "high", "describe", "this folder")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(lines, [f"ask -e cursor -m grok-4.7-high --cwd {self.folder} --read-only describe this folder"])

    def test_run_takes_another_engine_a_write_mode_and_refuses_an_unknown_tier(self):
        r, lines = self.sh("run.sh", "--engine", "local", "--write", "medium", "make a file")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(lines[-1], f"ask -e local-harness -m qwen2.5:3b --cwd {self.folder} make a file")
        r, _ = self.sh("run.sh", "turbo", "x")
        self.assertEqual(r.returncode, 2)
        self.assertIn("tier is one of low, medium, high, xhigh", r.stderr)

    def test_setup_installs_the_four_plugins_saves_the_loop_and_adds_the_workflows_unapproved(self):
        r, lines = self.sh("setup.sh")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        joined = "\n".join(lines)
        self.assertIn(f"plugin marketplace add metabot-demos {PLUGINS}", joined)
        for name in NAMES:
            self.assertIn(f"plugin install {name}@metabot-demos --yes", joined)
        self.assertIn("plugin enable watchdog --allow-bridges", joined)
        self.assertIn("plugin enable monitor --add-servers", joined)
        self.assertIn("plugin enable local-harness --allow-bridges", joined)
        self.assertIn("plugin enable browser", joined)
        self.assertIn(f"routine save watchdog --cron */5 * * * * -e watchdog --cwd {self.folder} watch", joined)
        adds = [x for x in lines if x.startswith("workflow add ")]
        self.assertEqual(len(adds), 2)
        for name in ("inception", "tiers"):
            doc = json.loads((self.tmp / f"added-{name}.json").read_text())
            self.assertEqual(doc["format"], "metabot.workflow/1")
            self.assertEqual(doc["cwd"], str(self.folder))
            self.assertLessEqual(len(doc["steps"]), 16)
        self.assertNotIn("workflow approve", joined)   # an approval is given at a terminal, never by a script
        self.assertIn("metabot workflow approve inception", r.stdout)
        self.assertIn("metabot workflow approve tiers", r.stdout)
        self.assertNotIn("connect add browser", joined)

    def test_the_workflow_templates_are_valid_and_name_the_monitor_connection_by_placeholder(self):
        inception = json.loads((DEMO / "workflows" / "inception.json").read_text())
        tiers = json.loads((DEMO / "workflows" / "tiers.json").read_text())
        self.assertEqual([s["engine"] for s in inception["steps"]], ["watchdog", "cursor"])
        self.assertEqual(inception["steps"][1]["allow"], ["__MONITOR____show_text"])
        self.assertIs(inception["steps"][1]["connections"], True)
        self.assertEqual([s["model"] for s in tiers["steps"]], ["grok-4.7-low", "grok-4.7-medium", "grok-4.7-high"])
        self.assertTrue(all(s.get("read_only") for s in tiers["steps"]))
        r, lines = self.sh("setup.sh")
        self.assertEqual(r.returncode, 0, r.stderr)
        doc = json.loads((self.tmp / "added-inception.json").read_text())
        self.assertIs(doc["steps"][1]["connections"], True)
        self.assertEqual(doc["steps"][1]["allow"], ["monitor-monitor__show_text"])

    def test_browser_connect_builds_the_connection_command_from_a_local_copy_or_npx(self):
        env = _env(METABOT=str(self.fake), HOME=str(self.tmp))
        r = _run(["/bin/sh", str(PLUGINS / "browser" / "bin" / "browser-connect"), "--mcp", "/x/cli.js",
                  "--origin", "https://example.com"], env)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = _run(["/bin/sh", str(PLUGINS / "browser" / "bin" / "browser-connect"), "--headed"], env)
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = self.log.read_text().splitlines()
        self.assertEqual(lines[0], "connect add browser --browser --no-token --origin https://example.com "
                                   "--command node /x/cli.js --browser chromium --isolated --headless")
        self.assertEqual(lines[1], "connect add browser --browser --no-token --command npx --yes "
                                   "@playwright/mcp@latest --browser chromium --isolated")


if __name__ == "__main__":
    unittest.main()
