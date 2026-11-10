"""Section 4: happy paths of the read routes and the smaller writes."""
import hashlib
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))
from harness import Env, FIX, server  # noqa: E402

GOLDEN = json.loads((FIX / "golden.json").read_text())


def seed_receipt(env, run_id, body, cwd=None, digest=True):
    rec = env.mbhome / "receipts"
    rec.mkdir(exist_ok=True)
    data = (json.dumps(body, indent=2) + "\n").encode()
    (rec / f"{run_id}.json").write_bytes(data)
    row = {"run_id": run_id, "started_at": "2026-01-01T00:00:00Z", "finished_at": "2026-01-01T00:00:01Z",
           "steps": len(body.get("steps", [])), "signer": "did:orcha:agent:x",
           "receipt": str(rec / f"{run_id}.json"), "engine": "claude"}
    if cwd:
        row["cwd"] = cwd
    if digest:
        row["receipt_sha256"] = hashlib.sha256(data).hexdigest()
    with open(env.mbhome / "runs.jsonl", "a") as f:
        f.write(json.dumps(row) + "\n")
    return data


class RouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = Env().start()
        cls.valid_bytes = seed_receipt(cls.env, "run-valid", GOLDEN["valid"],
                                       cwd=str(cls.env.projects / "alpha"))
        seed_receipt(cls.env, "run-tampered", GOLDEN["tampered"])
        seed_receipt(cls.env, "run-nodigest", GOLDEN["valid"], digest=False)
        j = cls.env.mbhome / "runs" / "run-valid"
        j.mkdir(parents=True)
        (j / "journal.json").write_text(json.dumps({"meta": {"cwd": "/x", "prompt": "hi"},
                                                    "steps": [], "verdicts": [], "signer": "did:orcha:agent:x"}))

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()

    def test_meta(self):
        s, j = self.env.get("/api/meta")
        self.assertEqual(s, 200)
        self.assertEqual(j["app"], "MetaBot")        # section 23: the app is MetaBot
        self.assertEqual(j["version"], "0.1.0")
        self.assertRegex(j["build"], r"^[0-9a-f]{7}$|^dev$")   # 21.4: the short SHA of this tree
        self.assertEqual(j["metabot"], {"bin": str(FIX / "fake-metabot"), "version": "0.3.0", "ok": True})
        self.assertEqual(j["orcha"]["version"], "0.2.2")
        self.assertIsInstance(j["display"]["vnc"], bool)
        self.assertIsNone(j["display"]["unit"])        # no display unit active in the fake
        self.assertEqual(j["default_desk"], "desk-a")
        self.assertEqual(j["limits"], {"max_desks": 24, "max_runs": 6})
        self.assertEqual(j["modules"], 10)
        self.assertEqual(j["catalog"], 74)                # 58 + 25.3 B2/B7
        self.assertEqual(j["default_folder"], str((self.env.projects / "alpha").resolve()))   # 25.3 B4

    def test_sessions_and_history_shapes(self):
        s, j = self.env.get("/api/sessions")
        self.assertEqual(s, 200)
        self.assertIsInstance(j["sessions"], list)
        s, j = self.env.get("/api/history")
        self.assertEqual(s, 200)
        # only local events (pane sends made by other tests) can show
        self.assertIsInstance(j["messages"], list)
        for ev in j["messages"]:
            self.assertEqual(set(ev), {"via", "time", "title", "message", "target", "from", "typed"})
            self.assertEqual(ev["title"], "metachat")

    def test_desks_shape_and_scope(self):
        self.env.add_session("fc-legacy", cwd="/")
        (server.NAMES_DIR).mkdir(parents=True, exist_ok=True)
        (server.NAMES_DIR / "legacy").write_text("legacy\n")
        (server.NAMES_DIR / "desk-a").write_text("desk-a\n")
        server._sess_cache[0] = 0.0
        s, j = self.env.get("/api/desks")
        self.assertEqual(s, 200)
        names = [d["name"] for d in j["desks"]]
        self.assertEqual(names[0], "desk-a")    # the default desk sorts first
        by = {d["name"]: d for d in j["desks"]}
        self.assertEqual(by["legacy"]["meta"], {})
        self.assertEqual(by["legacy"]["project"], "other")
        self.assertEqual(by["legacy"]["reach"], "idle")
        self.assertEqual(by["legacy"]["cli"], "shell")
        self.assertFalse(by["legacy"]["needs_you"])
        self.assertEqual(by["desk-a"]["cli"], "claude")   # prefix from config, no pane
        self.assertIn("streams", j)
        s, j = self.env.get("/api/desks?scope=metachat")
        self.assertEqual(j["desks"], [])

    def test_claude_prefix_on_idle_pane(self):
        self.env.add_session("fc-desk-b", cwd=str(self.env.projects / "alpha"))
        server.NAMES_DIR.mkdir(parents=True, exist_ok=True)
        (server.NAMES_DIR / "desk-b").write_text("desk-b\n")
        server._sess_cache[0] = 0.0
        s, j = self.env.get("/api/desks")
        d = [x for x in j["desks"] if x["name"] == "desk-b"][0]
        self.assertEqual(d["cli"], "claude")       # pane says shell, name says claude
        self.assertEqual(d["reach"], "idle")
        self.assertEqual(d["cwd"], str(self.env.projects / "alpha"))
        self.assertEqual(d["project"], "alpha")

    def test_projects(self):
        s, j = self.env.get("/api/projects")
        self.assertEqual(s, 200)
        names = [p["name"] for p in j["projects"]]
        self.assertEqual(names, ["alpha", "other"])
        alpha = j["projects"][0]
        self.assertEqual(alpha["paths"], [str(self.env.projects / "alpha")])
        self.assertEqual(alpha["runs"], 1)
        self.assertEqual(j["projects"][1]["runs"], 2)

    def test_accounts_empty(self):
        s, j = self.env.get("/api/accounts")
        self.assertEqual((s, j), (200, {"claude": [], "cursor": [],
                                        "defaults": {"claude": {"ready": False}, "cursor": {"ready": False}}}))

    def test_catalog(self):
        s, j = self.env.get("/api/catalog")
        self.assertEqual(s, 200)
        by = {e["id"]: e for e in j["entries"]}
        self.assertEqual(len(by), 74)
        self.assertEqual(by["plugin.list"], {"id": "plugin.list", "label": "List plugins", "module": "plugins",
                                             "kind": "read", "argv": ["{metabot}", "plugin", "list"],
                                             "params": []})
        self.assertEqual(by["plugin.new"]["kind"], "desk")
        self.assertEqual(by["plugin.new"]["params"][0]["type"], "id")
        self.assertEqual(by["sdk.verify"]["argv"][0], "{orcha}")

    def test_modules(self):
        s, j = self.env.get("/api/modules")
        self.assertEqual(s, 200)
        self.assertEqual(j["errors"], [])
        ids = [m["id"] for m in j["modules"]]
        self.assertEqual(ids, ["runs", "accounts", "plugins", "workflows", "connections", "office",
                               "sdk", "desktop", "tools", "a2a"])
        self.assertEqual(j["modules"][-1]["phase"], 2)

    def test_modules_reload_reports_errors(self):
        user = self.env.state / "modules"
        user.mkdir(exist_ok=True)
        (user / "broken.json").write_text(json.dumps({"id": "broken", "label": "B", "icon": "x", "order": 1,
                                                       "kind": "catalog", "phase": 1,
                                                       "sections": [{"title": "t", "items": [{"catalog": "x.y", "label": "l"}]}]}))
        (user / "runs.json").write_text(json.dumps({"id": "runs", "label": "My runs", "icon": "r", "order": 5,
                                                     "kind": "page", "page": "runs", "phase": 1}))
        s, j = self.env.post("/api/modules/reload", {})
        self.assertEqual(s, 200)
        self.assertTrue(j["ok"])
        self.assertEqual(j["errors"], [{"file": "user/broken.json", "error": "unknown catalog id x.y"}])
        runs = [m for m in j["modules"] if m["id"] == "runs"][0]
        self.assertEqual(runs["label"], "My runs")   # a user module replaces a built-in
        s, j = self.env.get("/api/events?limit=5")
        self.assertEqual(j["events"][0]["kind"], "modules_reload")
        for f in user.iterdir():
            f.unlink()
        self.env.post("/api/modules/reload", {})

    def test_mb_runs(self):
        s, j = self.env.get("/api/mb/runs?limit=2")
        self.assertEqual(s, 200)
        self.assertEqual(j["total"], 3)
        self.assertIsNone(j["error"])
        self.assertEqual(len(j["runs"]), 2)
        r = j["runs"][0]
        self.assertEqual(r["state"], "done")
        self.assertEqual(r["receipt_state"], "valid")
        self.assertIsNone(r["desk"])
        self.assertIn("row", r)
        s, j = self.env.get("/api/mb/runs?project=alpha")
        self.assertEqual([r["run_id"] for r in j["runs"]], ["run-valid"])
        self.assertEqual(j["runs"][0]["project"], "alpha")

    def test_mb_run_view(self):
        s, j = self.env.get("/api/mb/run/run-valid")
        self.assertEqual(s, 200)
        self.assertTrue(j["ok"])
        self.assertEqual(j["run"]["run_id"], "run-valid")
        self.assertEqual(j["receipt_state"], "valid")
        s, j = self.env.get("/api/mb/run/run-missing")
        self.assertEqual((s, j), (404, {"ok": False, "error": "no such run"}))

    def test_mb_receipt_bytes(self):
        s, j, raw, headers = self.env.request("GET", "/api/mb/receipt/run-valid")
        self.assertEqual(s, 200)
        self.assertEqual(raw, self.valid_bytes)
        h = dict((k.lower(), v) for k, v in headers)
        self.assertEqual(h["content-type"], "application/json")
        self.assertEqual(h["cache-control"], "no-store")
        s, j = self.env.get("/api/mb/receipt/run-missing")
        self.assertEqual(s, 404)

    def test_mb_verify_valid_tampered_nodigest(self):
        s, j = self.env.post("/api/mb/verify", {"id": "run-valid"})
        self.assertEqual(s, 200)
        self.assertEqual(j["pc_exit"], 0)
        self.assertTrue(j["pc"]["valid"])
        self.assertEqual(j["pc"]["run_id"], "run-9c2e-example")
        self.assertEqual(j["metabot"], {"receipt_state": "valid"})
        self.assertIs(j["index_sha_ok"], True)
        self.assertEqual(j["sha256"], hashlib.sha256(self.valid_bytes).hexdigest())
        s, j = self.env.post("/api/mb/verify", {"id": "run-tampered"})
        self.assertEqual(j["pc_exit"], 1)
        self.assertFalse(j["pc"]["valid"])
        self.assertFalse(j["pc"]["checks"]["steps_root"])
        s, j = self.env.post("/api/mb/verify", {"id": "run-nodigest"})
        self.assertIsNone(j["index_sha_ok"])
        s, j = self.env.post("/api/mb/verify", {"id": "run-missing"})
        self.assertEqual(s, 404)
        s, j = self.env.get("/api/events?limit=50")
        self.assertIn("verify", [e["kind"] for e in j["events"]])
        ev = [e for e in j["events"] if e["kind"] == "verify" and e["run_id"] == "run-valid"][0]
        self.assertTrue(ev["data"]["valid"])

    def test_index_sha_mismatch_is_false(self):
        p = self.env.mbhome / "receipts" / "run-valid.json"
        data = p.read_bytes()
        p.write_bytes(data + b"\n")
        try:
            s, j = self.env.post("/api/mb/verify", {"id": "run-valid"})
            self.assertIs(j["index_sha_ok"], False)
        finally:
            p.write_bytes(data)

    def test_sdk_verify(self):
        s, j = self.env.post("/api/sdk/verify", {"raw": json.dumps(GOLDEN["valid"])})
        self.assertEqual(s, 200)
        self.assertEqual(j["pc_exit"], 0)
        self.assertTrue(j["pc"]["valid"])
        s, j = self.env.post("/api/sdk/verify", {"raw": json.dumps(GOLDEN["tampered"])})
        self.assertEqual(j["pc_exit"], 1)
        s, j = self.env.post("/api/sdk/verify", {"raw": "not json at all"})
        self.assertEqual((j["pc_exit"], j["pc"]), (2, None))
        self.assertTrue(j["stderr"])
        s, j = self.env.post("/api/sdk/verify", {"raw": "x" * 60001})
        self.assertEqual((s, j), (400, {"ok": False, "error": "bad raw"}))
        calls = [c for c in self.env.calls("orcha") if c[:2] == ["verify", "--json"]]
        self.assertEqual(calls[-1], ["verify", "--json", "-"])

    def stdin_digests(self):
        out = []
        for line in self.env.log.read_text().splitlines():
            rec = json.loads(line)
            if rec["bin"] == "orcha" and "stdin_sha256" in rec:
                out.append(rec["stdin_sha256"])
        return out

    def test_sdk_verify_hands_orcha_the_exact_bytes(self):
        # VP-1: the PC must verify the bytes the tablet verified, never a str round-trip
        import base64
        raw = '{"format":"orcha.run-attestation/v1","run_id":"ré→\U0001f600"}'
        s, j = self.env.post("/api/sdk/verify", {"raw": raw})
        self.assertEqual(s, 200)
        self.assertEqual(self.stdin_digests()[-1], hashlib.sha256(raw.encode("utf-8")).hexdigest())
        # a lone surrogate cannot be UTF-8: 400, never '?' or U+FFFD handed to orcha
        before = len(self.stdin_digests())
        s, j = self.env.post("/api/sdk/verify", {"raw": '{"run_id":"\udcff"}'})
        self.assertEqual((s, j), (400, {"ok": False, "error": "bad raw"}))
        self.assertEqual(len(self.stdin_digests()), before)
        # raw_b64 carries arbitrary bytes and wins over raw
        data = b'{"run_id":"\xff\xfe"}'   # invalid UTF-8 on purpose: no str could carry it
        s, j = self.env.post("/api/sdk/verify", {"raw": "ignored", "raw_b64": base64.b64encode(data).decode()})
        self.assertEqual(s, 200, j)
        self.assertEqual(j["pc_exit"], 2)
        self.assertEqual(self.stdin_digests()[-1], hashlib.sha256(data).hexdigest())
        bom = "﻿".encode() + json.dumps(GOLDEN["valid"]).encode()
        s, j = self.env.post("/api/sdk/verify", {"raw_b64": base64.b64encode(bom).decode()})
        self.assertEqual(self.stdin_digests()[-1], hashlib.sha256(bom).hexdigest())
        s, j = self.env.post("/api/sdk/verify", {"raw_b64": "@@not base64@@"})
        self.assertEqual((s, j), (400, {"ok": False, "error": "bad raw"}))
        s, j = self.env.post("/api/sdk/verify", {"raw_b64": base64.b64encode(b"x" * 60001).decode()})
        self.assertEqual((s, j), (413, {"ok": False, "error": "too large"}))   # the body rule fires first
        s, j = self.env.post("/api/sdk/verify", {"raw_b64": "x" * (4 * 60000 + 4)})
        self.assertIn((s, j), [(400, {"ok": False, "error": "bad raw"}), (413, {"ok": False, "error": "too large"})])
        s, j = self.env.post("/api/sdk/verify", {})
        self.assertEqual((s, j), (400, {"ok": False, "error": "bad raw"}))

    def test_sdk_verify_catalog_entry_gets_the_receipt_path(self):
        # SEC-6: orcha verify wants the file, not the run id
        s, j = self.env.post("/api/cli/read", {"id": "sdk.verify", "params": {"run": "run-valid"}})
        self.assertEqual(s, 200, j)
        self.assertEqual(j["argv"], [str(FIX / "fake-orcha"), "verify", "--json",
                                     str((self.env.mbhome / "receipts" / "run-valid.json").resolve())])
        self.assertEqual(j["exit"], 0)
        self.assertTrue(json.loads(j["stdout"])["valid"])
        s, j = self.env.post("/api/cli/read", {"id": "sdk.verify", "params": {"run": "run-tampered"}})
        self.assertEqual(j["exit"], 1)
        s, j = self.env.post("/api/cli/read", {"id": "sdk.verify", "params": {"run": "run-missing"}})
        self.assertEqual((s, j), (400, {"ok": False, "error": "bad run: no such receipt"}))
        # the metabot subcommands keep the id
        s, j = self.env.post("/api/cli/read", {"id": "receipts.show", "params": {"run": "run-valid"}})
        self.assertEqual(j["argv"][1:], ["receipts", "show", "run-valid"])
        s, j = self.env.get("/api/catalog")
        by = {e["id"]: e for e in j["entries"]}
        self.assertEqual(by["sdk.verify"]["params"][0]["type"], "receipt_path")
        self.assertEqual(by["receipts.show"]["params"][0]["type"], "receipt")

    def test_pane_sniff_matches_the_executable_only(self):
        # SEC-9: a bare word "claude" on a command line is not the Claude CLI
        for args in ("tail -f claude", "ssh claude", "less claude", "node /x/claude", "bash --rcfile claude",
                     "python3 /x/metachat/server.py claude", "claudette", "/x/claude-helper", ""):
            self.assertIsNone(server.cli_of_args(args), args)
        for args, cli in (("claude", "claude"), ("claude --resume", "claude"),
                          ("/opt/x/.local/bin/claude --resume abc", "claude"), ("./claude", "claude"),
                          ("cursor-agent", "cursor"), ("/opt/x/.local/bin/cursor-agent --use-system-ca", "cursor"),
                          ("/opt/x/.local/bin/agent --use-system-ca", "cursor"), ("  claude  ", "claude")):
            self.assertEqual(server.cli_of_args(args), cli, args)

    def test_mb_run_mbq_failure_is_502(self):
        # F10: a broken venv is not "no such run"
        import os
        os.environ["FAKE_MBQ_FAIL"] = "1"
        try:
            s, j = self.env.get("/api/mb/run/run-valid")
        finally:
            os.environ.pop("FAKE_MBQ_FAIL")
        self.assertEqual((s, j), (502, {"ok": False, "error": "RuntimeError: boom"}))
        s, j = self.env.get("/api/mb/run/run-missing")
        self.assertEqual((s, j), (404, {"ok": False, "error": "no such run"}))

    def test_events(self):
        s, j = self.env.get("/api/events?limit=1")
        self.assertEqual(s, 200)
        self.assertEqual(len(j["events"]), 1)
        self.assertEqual(set(j["events"][0]), {"id", "ts", "kind", "desk", "run_id", "data"})

    def test_index_html_served(self):
        s, j, raw, headers = self.env.request("GET", "/")
        self.assertEqual(s, 200)
        self.assertEqual(raw, (Path(__file__).resolve().parents[1] / "src/metabot_app/index.html").read_bytes())
        self.assertEqual(dict((k.lower(), v) for k, v in headers)["content-type"], "text/html; charset=utf-8")

    def test_stream_connects(self):
        import socket
        s = socket.create_connection(("127.0.0.1", self.env.port), timeout=5)
        s.sendall(f"GET /api/stream?t={self.env.token} HTTP/1.1\r\nHost: {self.env.host}\r\n\r\n".encode())
        data = b""
        while b": connected" not in data:
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
        s.close()
        self.assertIn(b"200 OK", data)
        self.assertIn(b"text/event-stream", data)
        self.assertIn(b": connected\n\n", data)

    def test_chat_text_reaches_tmux_byte_for_byte(self):
        """Follow-up 3: chat text goes on load-buffer's stdin, so tmux's argument
        parser never sees it (set-buffer dropped a trailing ";", and turned a
        trailing backslash-semicolon into a plain ";": tmux 3.4)."""
        import subprocess
        import time
        proc = subprocess.Popen(["bash", "-c", "exec -a cursor-agent sleep 30"])
        try:
            time.sleep(0.2)
            self.env.add_session("fc-pane-one", pid=proc.pid)
            server.NAMES_DIR.mkdir(parents=True, exist_ok=True)
            (server.NAMES_DIR / "pane-one").write_text("pane-one\n")
            server._sess_cache[0] = 0.0
            for text in ("run the tests;", "-x starts with a dash", "keep a\\;b inside", "ends with \\;",
                         "two\nlines;"):
                self.env.clear_calls()
                s, j = self.env.post("/api/send", {"text": text, "target": "pane-one"})
                self.assertEqual((s, j["via"]), (200, "pane"), text)
                buf, data = self.env.tmux_loaded()[-1]
                self.assertEqual(data, text.encode("utf-8"), text)
                self.assertEqual(self.env.tmux_pasted()[-1], (buf, "=fc-pane-one:", True, text.encode("utf-8")))
                self.assertNotIn("set-buffer", [c[0] for c in self.env.calls("tmux")])
        finally:
            proc.kill()
            proc.wait()

    def test_send_via_pane_happy_path(self):
        import subprocess
        import time
        # a pane whose process tree carries a cursor-agent command line (a renamed sleep)
        proc = subprocess.Popen(["bash", "-c", "exec -a cursor-agent sleep 30"])
        try:
            time.sleep(0.2)
            self.env.add_session("fc-pane-one", pid=proc.pid)
            server.NAMES_DIR.mkdir(parents=True, exist_ok=True)
            (server.NAMES_DIR / "pane-one").write_text("pane-one\n")
            server._sess_cache[0] = 0.0
            s, j = self.env.get("/api/desks")
            d = [x for x in j["desks"] if x["name"] == "pane-one"][0]
            self.assertEqual((d["reach"], d["cli"], d["pane"]), ("pane", "cursor", True))
            self.env.clear_calls()
            s, j = self.env.post("/api/send", {"text": "hello\x1b[201~ there", "target": "pane-one"})
            self.assertEqual((s, j), (200, {"ok": True, "error": "", "via": "pane", "reach": "pane"}))
            calls = self.env.calls("tmux")
            buf, data = self.env.tmux_loaded()[-1]
            self.assertRegex(buf, r"^metachat-[0-9a-f]{8}$")   # a buffer of its own per paste
            self.assertIn(["load-buffer", "-b", buf, "-"], calls)   # the text on stdin, never an argument
            self.assertEqual(data, b"hello[201~ there")
            self.assertNotIn("set-buffer", [c[0] for c in calls])
            self.assertIn(["paste-buffer", "-d", "-p", "-b", buf, "-t", "=fc-pane-one:"], calls)
            self.assertEqual(self.env.tmux_pasted()[-1], (buf, "=fc-pane-one:", True, b"hello[201~ there"))
            self.assertIn(["send-keys", "-t", "=fc-pane-one:", "Enter"], calls)
            s, j = self.env.post("/api/pane", {"target": "pane-one", "action": "start-claude"})
            self.assertEqual(s, 409)
            self.assertEqual(j["error"], "cursor already runs here")
        finally:
            proc.kill()
            proc.wait()

    def test_send_409_paths(self):
        s, j = self.env.post("/api/send", {"text": "hi", "target": "ghost"})
        self.assertEqual(s, 409)
        self.assertEqual(j["reach"], "none")
        self.env.add_session("fc-idle-one")
        server.NAMES_DIR.mkdir(parents=True, exist_ok=True)
        (server.NAMES_DIR / "idle-one").write_text("idle-one\n")
        server._sess_cache[0] = 0.0
        s, j = self.env.post("/api/send", {"text": "hi", "target": "idle-one"})
        self.assertEqual((s, j["reach"]), (409, "idle"))
        s, j = self.env.post("/api/pane", {"target": "idle-one", "action": "nope"})
        self.assertEqual((s, j), (409, {"ok": False, "error": "unknown action"}))
        s, j = self.env.post("/api/pane", {"target": "idle-one", "action": "start-claude"})
        self.assertEqual(s, 200)
        keys = [c for c in self.env.calls("tmux") if c[:1] == ["send-keys"]]
        # 11-10: the started Claude gets a session id the PC records, so the desk can be resumed
        sid = keys[-2][-1].rsplit(" ", 1)[-1]
        self.assertEqual(keys[-2], ["send-keys", "-t", "=fc-idle-one:", "-l", f"claude --session-id {sid}"])
        self.assertRegex(sid, r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
        self.assertEqual((server.NAMES_DIR / sid).read_text().strip(), "idle-one")
        s, j = self.env.post("/api/pane", {"target": "idle-one", "action": "start-cursor"})
        self.assertEqual(s, 200)
        keys = [c for c in self.env.calls("tmux") if c[:1] == ["send-keys"]]
        self.assertEqual(keys[-2], ["send-keys", "-t", "=fc-idle-one:", "-l", "cursor-agent"])

    def test_accounts_post(self):
        s, j = self.env.post("/api/accounts", {"engine": "cursor", "name": "alt"})
        self.assertEqual(s, 200)
        self.assertEqual(j["name"], "alt")
        folder = Path(j["folder"])
        self.assertTrue((folder / "config").is_dir())
        self.assertEqual(oct(folder.stat().st_mode & 0o777), "0o700")
        s, j = self.env.post("/api/accounts", {"engine": "cursor", "name": "alt"})
        self.assertEqual((s, j), (409, {"ok": False, "error": "name taken"}))
        for bad in ("default", "Work", "1x", "a" * 25, ""):
            s, j = self.env.post("/api/accounts", {"engine": "claude", "name": bad})
            self.assertEqual(s, 400, bad)
        (self.env.home / ".claude" / "settings.json").write_text("{}")
        (self.env.home / ".claude" / ".credentials.json").write_text("secret")
        (self.env.home / ".claude" / "skills").mkdir()
        s, j = self.env.post("/api/accounts", {"engine": "claude", "name": "work"})
        self.assertEqual(s, 200, j)
        folder = Path(j["folder"])
        self.assertTrue((folder / "settings.json").is_symlink())
        self.assertTrue((folder / "skills").is_symlink())
        self.assertFalse((folder / ".credentials.json").exists())
        self.assertFalse((folder / ".claude.json").exists())
        call = [c for c in self.env.calls("metabot") if c[:2] == ["login", "add"]][-1]
        self.assertEqual(call, ["login", "add", "work", "--engine", "claude", "--home", str(folder),
                                "--max-at-once", "3"])
        s, j = self.env.get("/api/accounts")
        self.assertEqual(j["claude"][0]["name"], "work")
        self.assertEqual(j["claude"][0]["max_at_once"], 3)
        # 25.3 B2: ready = signed in; a new login folder never gets the default's credentials
        self.assertFalse(j["claude"][0]["ready"])
        self.assertEqual(j["claude"][0]["source"], "metabot")
        self.assertEqual(j["cursor"], [{"name": "alt", "folder": str(self.env.state / "accounts/cursor/alt"),
                                        "proven": False, "ready": False}])
        self.assertEqual(j["defaults"], {"claude": {"ready": True}, "cursor": {"ready": False}})
        s, j = self.env.post("/api/accounts", {"engine": "claude", "name": "bad-one"})
        self.assertEqual(s, 502)
        self.assertEqual(j["error"], "login add: bad-one refused by the fake")
        self.assertFalse((self.env.state / "accounts/claude/bad-one").exists())
        s, j = self.env.get("/api/events?limit=50")
        self.assertIn("account", [e["kind"] for e in j["events"]])


if __name__ == "__main__":
    unittest.main()
