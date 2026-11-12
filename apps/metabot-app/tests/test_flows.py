"""The server-flows review findings, GET /api/tree (22.3) and the 21.4 workflow
end-to-end: new -> approve -> run -> a receipt in /api/mb/runs that
/api/mb/verify checks."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))
from harness import Env, FIX, server, ws_read_close, ws_read_frame  # noqa: E402
from metabot_app import catalog  # noqa: E402
from metabot_app import mb  # noqa: E402

ROOT = Path(__file__).resolve().parents[1] / "src" / "metabot_app"


def pane_desk(env, name, cmd="claude"):
    """A tmux session whose pane tree carries a CLI command line (a renamed sleep)."""
    proc = subprocess.Popen(["bash", "-c", f"exec -a {cmd} sleep 30"])
    time.sleep(0.2)
    env.add_session(f"fc-{name}", pid=proc.pid)
    server.invalidate()
    return proc


def desk_argv(call):
    """The argv a recorded tmux new-session hands the fixed script (after 'metachat')."""
    return call[call.index("metachat") + 1:]


class RunDeskChatTests(unittest.TestCase):
    """server-flows-1 (blocker): chat text never reaches a headless run's pty."""

    def setUp(self):
        self.env = Env().start()

    def tearDown(self):
        self.env.stop()

    def test_run_desk_takes_no_chat(self):
        proc = subprocess.Popen(["bash", "-c", "exec -a claude sleep 30"])   # the headless engine
        try:
            time.sleep(0.2)
            os.environ["FAKE_PANE_PID"] = str(proc.pid)
            try:
                s, j = self.env.post("/api/run", {"task": "x", "engine": "claude",
                                                  "cwd": str(self.env.projects / "alpha")})
            finally:
                os.environ.pop("FAKE_PANE_PID", None)
            self.assertEqual((s, j["name"]), (200, "run-1"))
            s, j = self.env.get("/api/desks")
            d = [x for x in j["desks"] if x["name"] == "run-1"][0]
            self.assertEqual((d["pane"], d["cli"], d["meta"]["kind"]), (True, "claude", "run"))
            self.assertEqual(d["reach"], "idle")              # never "pane" for a run desk
            self.env.clear_calls()
            s, j = self.env.post("/api/send", {"text": "rm -rf the old build dir please", "target": "run-1"})
            self.assertEqual(s, 409, j)
            self.assertEqual(j["error"], "a run desk takes no chat — watch it in Runs")
            s, j = self.env.post("/api/send", {"text": "x", "target": "run-1", "kind": "handoff"})
            self.assertEqual(s, 409)
            s, j = self.env.post("/api/pane", {"target": "run-1", "action": "arm"})
            self.assertEqual(s, 409)
            s, j = self.env.post("/api/pane", {"target": "run-1", "action": "start-claude"})
            self.assertEqual(s, 409)
            # a desk's mc-send to a run lands in the lane, never in the pty
            s, j = self.env.post("/api/send", {"text": "mc-send run-3 hello", "target": "run-1", "from": "desk-a"})
            self.assertEqual((s, j["via"]), (200, "lane"))
            tmux = [c[0] for c in self.env.calls("tmux")]
            for forbidden in ("set-buffer", "load-buffer", "paste-buffer", "send-keys"):
                self.assertNotIn(forbidden, tmux)
        finally:
            proc.kill()
            proc.wait()

    def test_cli_desk_takes_no_chat_either(self):
        proc = subprocess.Popen(["bash", "-c", "exec -a claude sleep 30"])
        try:
            time.sleep(0.2)
            os.environ["FAKE_PANE_PID"] = str(proc.pid)
            try:
                s, j = self.env.post("/api/cli/desk", {"id": "chat", "params": {}})
            finally:
                os.environ.pop("FAKE_PANE_PID", None)
            self.assertEqual(s, 200, j)
            s, j = self.env.post("/api/send", {"text": "hi", "target": "chat"})
            self.assertEqual((s, j["error"]), (409, "a command desk takes no chat — open its terminal"))
            self.assertEqual([c for c in self.env.calls("tmux") if c[:1] in (["set-buffer"], ["load-buffer"])], [])
        finally:
            proc.kill()
            proc.wait()


class RespawnTests(unittest.TestCase):
    """server-flows-2: a finished catalogue desk never re-runs its command."""

    def setUp(self):
        self.env = Env().start()

    def tearDown(self):
        self.env.stop()

    def test_dead_cli_desk_comes_back_as_a_note_shell(self):
        for cid, params in (("workflow.run", {"ref": "nightly"}), ("plugin.remove", {"name": "foo"}),
                            ("connect.revoke", {"name": "svc"}), ("login.remove", {"name": "work"})):
            s, j = self.env.post("/api/cli/desk", {"id": cid, "params": params})
            self.assertEqual(s, 200, j)
            name, argv = j["name"], j["argv"]
            st = json.loads(self.env.tmux_state.read_text())
            del st["sessions"][f"fc-{name}"]
            self.env.tmux_state.write_text(json.dumps(st))
            self.env.clear_calls()
            s, status, rest = self.env.ws(f"/ws/term?desk={name}", origin=f"http://{self.env.host}")
            self.assertEqual(status, 101)
            op, payload, rest = ws_read_frame(s, rest)
            self.assertEqual(op, 2)
            self.assertIn(f"attached fc-{name}".encode(), payload)
            s.close()
            time.sleep(0.2)
            news = [c for c in self.env.calls("tmux") if c[:1] == ["new-session"]]
            self.assertEqual(len(news), 1, cid)
            call = news[0]
            self.assertIn(mb.FIXED_NOTE_SCRIPT, call)
            self.assertNotIn(mb.FIXED_SCRIPT, call)
            self.assertEqual(desk_argv(call), argv)   # shown as a note only: the note script never runs "$@"
            self.assertNotIn('; "$@"; s=', mb.FIXED_NOTE_SCRIPT)     # the run step of FIXED_SCRIPT
            self.assertIn('; "$@"; s=', mb.FIXED_SCRIPT)
            self.assertNotIn(' "$@" <', mb.FIXED_NOTE_SCRIPT)          # nor the run step of the run script
            self.assertTrue(mb.FIXED_NOTE_SCRIPT.endswith("exec bash -l"))
            s, j = self.env.get("/api/events?limit=1")
            self.assertTrue(j["events"][0]["data"].get("note_only"))
        # the note script prints the argv and gives a shell, without running the command
        with tempfile.TemporaryDirectory() as home:
            r = subprocess.run(["bash", "-lc", mb.FIXED_NOTE_SCRIPT, "metachat", "touch", "/tmp/metachat-note-ran"],
                               capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL,
                               env={"HOME": home, "PATH": os.environ["PATH"], "TERM": "dumb"})
            self.assertIn("$ touch /tmp/metachat-note-ran (finished earlier; not run again)", r.stdout)
            self.assertFalse(os.path.exists("/tmp/metachat-note-ran"))

    def test_named_desks_still_respawn_their_command(self):
        s, j = self.env.post("/api/spawn", {"kind": "cursor", "label": "c1"})
        st = json.loads(self.env.tmux_state.read_text())
        del st["sessions"]["fc-c1"]
        self.env.tmux_state.write_text(json.dumps(st))
        self.env.clear_calls()
        s, status, rest = self.env.ws("/ws/term?desk=c1")
        ws_read_frame(s, rest)
        s.close()
        call = [c for c in self.env.calls("tmux") if c[:1] == ["new-session"]][0]
        self.assertIn(mb.FIXED_SCRIPT, call)
        self.assertEqual(desk_argv(call), ["cursor-agent"])


class RunLabelTests(unittest.TestCase):
    """server-flows-4: a run name longer than desk_slug keeps is refused; an old one is killable."""

    def setUp(self):
        self.env = Env().start()

    def tearDown(self):
        self.env.stop()

    def test_long_run_label_400_and_registry_name_kill(self):
        cwd = str(self.env.projects / "alpha")
        s, j = self.env.post("/api/run", {"task": "x", "engine": "claude", "cwd": cwd, "label": "a" * 37})
        self.assertEqual((s, j), (400, {"ok": False, "error": "bad label"}))
        self.assertEqual([c for c in self.env.calls("tmux") if c[:1] == ["new-session"]], [])
        self.assertEqual(list((self.env.state / "prompts").glob("*")), [])
        s, j = self.env.post("/api/run", {"task": "x", "engine": "claude", "cwd": cwd, "label": "a" * 36})
        self.assertEqual((s, j["name"]), (200, "run-" + "a" * 36))
        self.assertEqual(server.desk_slug(j["name"]), j["name"])
        s, status, rest = self.env.ws(f"/ws/term?desk={j['name']}")
        op, payload, _ = ws_read_frame(s, rest)
        self.assertEqual(op, 2)   # it has a terminal
        s.close()
        # a registry entry made before the cap (44 chars) is still killable by its name
        long = "run-" + "b" * 40
        mb.reg_write(server.MB, {"name": long, "kind": "run", "account": None, "engine": "claude",
                                 "cwd": cwd, "label": "b" * 40, "created": 1, "catalog": None,
                                 "argv": ["x"], "prompt_file": None})
        self.env.add_session(f"fc-{long}")
        self.assertNotEqual(server.desk_slug(long), long)
        s, j = self.env.post("/api/kill", {"name": long, "confirm": True})
        self.assertEqual((s, j), (200, {"ok": True}))
        self.assertNotIn(f"fc-{long}", self.env.tmux_sessions())
        self.assertFalse((self.env.state / "desks" / f"{long}.json").exists())
        s, j = self.env.post("/api/kill", {"name": "x" * 50, "confirm": True})
        self.assertEqual((s, j), (400, {"ok": False, "error": "bad name"}))   # not registered: the slug rule


class CatalogFolderTests(unittest.TestCase):
    """server-flows-6/-7/-8: reads that need a folder, cli/read cwd, workflow.new, login.add."""

    def setUp(self):
        self.env = Env().start()

    def tearDown(self):
        self.env.stop()

    def metabot_calls(self):
        out = []
        for line in self.env.log.read_text().splitlines():
            rec = json.loads(line)
            if rec["bin"] == "metabot":
                out.append(rec)
        return out

    def test_repository_reads_take_a_folder(self):
        alpha = str(self.env.projects / "alpha")
        for cid in ("desks.list", "office.check"):
            s, j = self.env.post("/api/cli/read", {"id": cid, "params": {}})
            self.assertEqual((s, j), (400, {"ok": False, "error": "missing folder"}), cid)
            s, j = self.env.post("/api/cli/read", {"id": cid, "params": {"folder": alpha}})
            self.assertEqual(s, 200, j)
            self.assertEqual(j["argv"][-2:], ["--dir", alpha])
            s, j = self.env.post("/api/cli/read", {"id": cid, "params": {"folder": str(self.env.home)}})
            self.assertEqual(s, 400)
        for cid, params in (("office.trust", {}), ("desks.pick", {"name": "run/desk"}), ("desks.clean", {"run": "r1"})):
            s, j = self.env.post("/api/cli/desk", {"id": cid, "params": dict(params, folder=alpha)})
            self.assertEqual(s, 200, j)
            self.assertIn("--dir", j["argv"])
            self.assertEqual(j["argv"][j["argv"].index("--dir") + 1], alpha)

    def test_cli_read_runs_in_the_given_cwd(self):
        alpha = str(self.env.projects / "alpha")
        s, j = self.env.post("/api/cli/read", {"id": "status", "params": {}})
        self.assertEqual(s, 200)
        self.assertEqual(self.metabot_calls()[-1]["cwd"], str(self.env.home))
        s, j = self.env.post("/api/cli/read", {"id": "status", "params": {}, "cwd": alpha})
        self.assertEqual(s, 200, j)
        self.assertEqual(self.metabot_calls()[-1]["cwd"], alpha)
        s, j = self.env.post("/api/cli/read", {"id": "status", "params": {}, "cwd": str(self.env.home / ".ssh")})
        self.assertEqual((s, j), (400, {"ok": False, "error": "bad cwd"}))
        s, j = self.env.post("/api/cli/read", {"id": "status", "params": {}, "cwd": "/tmp"})
        self.assertEqual(s, 400)

    def test_workflow_new_is_a_desk_command(self):
        alpha = str(self.env.projects / "alpha")
        s, j = self.env.post("/api/cli/desk", {"id": "workflow.new", "params": {
            "name": "nightly", "engine": "claude", "folder": alpha, "prompt": "Summarise the day's commits"}})
        self.assertEqual(s, 200, j)
        self.assertEqual(j["name"], "workflow-new")
        self.assertEqual(j["argv"], [str(FIX / "fake-metabot"), "workflow", "new", "-e", "claude", "--cwd", alpha,
                                     "nightly", "Summarise the day's commits"])
        s, j = self.env.post("/api/cli/desk", {"id": "workflow.new", "params": {
            "name": "../x", "engine": "claude", "folder": alpha, "prompt": "p"}})
        self.assertEqual(s, 400)
        s, j = self.env.post("/api/cli/desk", {"id": "workflow.new", "params": {
            "name": "n", "engine": "claude", "folder": alpha, "prompt": "bell\x07"}})
        self.assertEqual(s, 400)
        r = catalog.load_modules(ROOT / "modules", "/nonexistent")
        wf = [m for m in r["modules"] if m["id"] == "workflows"][0]
        items = [it["catalog"] for s in wf["sections"] for it in s["items"]]
        self.assertIn("workflow.new", items)


class WorkflowEndToEndTests(unittest.TestCase):
    """21.4: new (desk) -> approve (desk) -> run (desk) -> /api/mb/runs done -> /api/mb/verify pc_exit 0."""

    def setUp(self):
        self.env = Env().start()

    def tearDown(self):
        self.env.stop()

    def run_desk_command(self, name, cwd):
        """What the desk does when it opens: run the argv the fixed script was handed."""
        call = [c for c in self.env.calls("tmux") if c[:1] == ["new-session"] and c[3] == f"fc-{name}"][-1]
        self.assertEqual(call[5], cwd)
        argv = desk_argv(call)
        env = dict(os.environ, METABOT_HOME=str(self.env.mbhome))
        r = subprocess.run(argv, capture_output=True, text=True, timeout=30, cwd=cwd, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def test_new_approve_run_verify(self):
        alpha = str(self.env.projects / "alpha")
        s, j = self.env.post("/api/cli/desk", {"id": "workflow.new", "cwd": alpha, "params": {
            "name": "nightly", "engine": "claude", "folder": alpha, "prompt": "Summarise the commits"}})
        self.assertEqual(s, 200, j)
        self.run_desk_command(j["name"], alpha)
        s, j = self.env.post("/api/cli/desk", {"id": "workflow.approve", "cwd": alpha, "params": {"ref": "nightly"}})
        self.assertEqual(s, 200, j)
        self.assertEqual(j["argv"][1:], ["workflow", "approve", "nightly"])
        self.run_desk_command(j["name"], alpha)
        s, j = self.env.get("/api/mb/runs")
        self.assertEqual(j["runs"], [])
        s, j = self.env.post("/api/cli/desk", {"id": "workflow.run", "cwd": alpha, "params": {"ref": "nightly"}})
        self.assertEqual(s, 200, j)
        self.assertEqual(j["argv"][1:], ["workflow", "run", "nightly"])
        out = self.run_desk_command(j["name"], alpha)
        run_id = out.split(": ", 1)[1].strip()
        self.assertTrue(run_id.startswith("wf-nightly-"))
        s, j = self.env.get("/api/mb/runs")
        self.assertEqual(s, 200)
        self.assertEqual(j["total"], 1)
        row = j["runs"][0]
        self.assertEqual((row["run_id"], row["state"], row["receipt_state"], row["project"]),
                         (run_id, "done", "valid", "alpha"))
        s, j = self.env.post("/api/mb/verify", {"id": run_id})
        self.assertEqual(s, 200, j)
        self.assertEqual(j["pc_exit"], 0)
        self.assertTrue(j["pc"]["valid"])
        self.assertIs(j["index_sha_ok"], True)
        self.assertEqual(j["metabot"], {"receipt_state": "valid"})
        s, j, raw, _ = self.env.request("GET", f"/api/mb/receipt/{run_id}")
        self.assertEqual(s, 200)
        self.assertEqual(json.loads(raw)["format"], "orcha.run-attestation/v1")
        s, j = self.env.get("/api/tree")
        alpha_node = [p for p in j["projects"] if p["name"] == "alpha"][0]
        self.assertEqual([r["run_id"] for r in alpha_node["folders"][0]["runs"]], [run_id])
        self.assertEqual(alpha_node["folders"][0]["runs"][0]["label"], "workflow nightly")
        s, j = self.env.get("/api/events?limit=20")
        kinds = [e["kind"] for e in j["events"]]
        self.assertEqual(kinds.count("cli_desk"), 3)
        self.assertIn("verify", kinds)


class ModuleRuleTests(unittest.TestCase):
    """21.4 manifest modes; server-flows-10 cross-module slashes."""

    def test_modes_validated_and_builtins_carry_them(self):
        base = {"id": "m", "label": "M", "icon": "m", "order": 1, "kind": "card", "phase": 1,
                "card": {"text": "t", "links": []}}
        self.assertIsNone(catalog._check_manifest(dict(base)))
        self.assertIsNone(catalog._check_manifest(dict(base, modes=["user", "dev"])))
        self.assertIsNone(catalog._check_manifest(dict(base, modes=["dev"])))
        for bad in ([], ["admin"], "dev", ["dev", "dev"], ["user", "dev", "x"], None):
            self.assertEqual(catalog._check_manifest(dict(base, modes=bad)), "bad modes", bad)
        r = catalog.load_modules(ROOT / "modules", "/nonexistent")
        self.assertEqual(r["errors"], [])
        modes = {m["id"]: m["modes"] for m in r["modules"]}
        # 25.4 U3: the plugins module is Extend, listed in user mode too
        self.assertEqual({k for k, v in modes.items() if "user" in v},
                         {"runs", "accounts", "plugins", "workflows", "desktop"})
        self.assertTrue(all(set(v) <= {"user", "dev"} and "dev" in v for v in modes.values()))
        for m in r["modules"]:
            for key in ("order", "icon", "label", "kind", "modes"):
                self.assertIn(key, m, m["id"])

    def test_duplicate_slash_across_modules_is_an_error_row(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            mk = lambda mid, order, slash: json.dumps({  # noqa: E731
                "id": mid, "label": mid, "icon": "x", "order": order, "kind": "card", "phase": 1,
                "card": {"text": "t", "links": []},
                "commands": [{"slash": slash, "label": "x", "run": {"catalog": "status"}}]})
            (d / "one.json").write_text(mk("mod-one", 5, "a2a"))     # before workflows (order 40)
            (d / "two.json").write_text(mk("mod-two", 50, "a2a"))    # after it
            (d / "three.json").write_text(mk("mod-three", 60, "ping"))
            r = catalog.load_modules(ROOT / "modules", d)
            ids = [m["id"] for m in r["modules"]]
            self.assertIn("mod-one", ids)
            self.assertIn("mod-three", ids)
            self.assertNotIn("workflows", ids)   # /a2a now belongs to the earlier-ordered module
            self.assertNotIn("mod-two", ids)
            self.assertEqual(sorted((e["file"], e["error"]) for e in r["errors"]),
                             [("builtin/workflows.json", "slash /a2a taken by module mod-one"),
                              ("user/two.json", "slash /a2a taken by module mod-one")])
            owners = {c["slash"]: m["id"] for m in r["modules"] for c in m.get("commands", [])}
            self.assertEqual(owners["a2a"], "mod-one")
            self.assertEqual(len([s for m in r["modules"] for c in m.get("commands", []) for s in [c["slash"]]]),
                             len(owners))


class TreeTests(unittest.TestCase):
    """22.3: GET /api/tree from two projects, one stray folder and two ended names."""

    def test_tree_shape(self):
        with Env() as env:
            alpha, gamma, beta = (str(env.projects / n) for n in ("alpha", "gamma", "beta"))
            (env.projects / "gamma").mkdir()
            cfg = json.loads(env.config.read_text())
            cfg["projects"] = [{"name": "alpha", "paths": [alpha]}, {"name": "gamma", "paths": [gamma]}]
            env.config.write_text(json.dumps(cfg))
            server.configure()
            s, j = env.post("/api/spawn", {"kind": "shell", "label": "s-alpha", "cwd": alpha})
            self.assertEqual(s, 200, j)
            s, j = env.post("/api/run", {"task": "x", "engine": "claude", "cwd": gamma})
            self.assertEqual(s, 200, j)
            env.add_session("fc-stray", cwd=beta)                 # a legacy desk in a folder of no project
            server.NAMES_DIR.mkdir(parents=True, exist_ok=True)
            for n in ("stray", "fl-one", "fl-two"):
                (server.NAMES_DIR / n).write_text(n + "\n")
            env.set_pane("fc-s-alpha", "Edit notes.md\nDo you want to proceed?\n 1. Yes")
            with open(env.mbhome / "runs.jsonl", "a") as f:
                f.write(json.dumps({"run_id": "r-alpha", "cwd": alpha, "label": "the alpha run"}) + "\n")
                f.write(json.dumps({"run_id": "r-x", "meta": {"cwd": "/x/y", "prompt": "first line\nsecond"}}) + "\n")
            server.invalidate()
            server.MB._needs.clear()
            s, j = env.get("/api/tree")
            self.assertEqual(s, 200)
            self.assertEqual(set(j), {"waiting", "projects", "other", "ended"})
            self.assertEqual([p["name"] for p in j["projects"]], ["alpha", "gamma"])
            a = j["projects"][0]["folders"]
            self.assertEqual([f["path"] for f in a], [alpha])
            self.assertEqual([d["name"] for d in a[0]["desks"]], ["s-alpha"])
            self.assertEqual(a[0]["desks"][0]["needs_you"], True)
            self.assertEqual(a[0]["runs"], [{"run_id": "r-alpha", "state": "done", "receipt_state": "no receipt",
                                             "label": "the alpha run"}])
            g = j["projects"][1]["folders"][0]
            self.assertEqual([(d["name"], d["meta"]["kind"]) for d in g["desks"]], [("run-1", "run")])
            self.assertEqual(g["runs"], [])
            others = {o["path"]: o for o in j["other"]}
            self.assertEqual([o["path"] for o in j["other"]], sorted(others))
            self.assertEqual({p: ([d["name"] for d in o["desks"]], [r["run_id"] for r in o["runs"]])
                              for p, o in others.items()},
                             {"/x/y": ([], ["r-x"]), beta: (["stray"], [])})
            self.assertEqual(others["/x/y"]["runs"][0]["label"], "first line")
            self.assertEqual(sorted(d["name"] for d in j["ended"]), ["fl-one", "fl-two"])   # stray has a pane
            self.assertTrue(all(d["pane"] is False and d["meta"] == {} for d in j["ended"]))
            self.assertEqual(len(j["waiting"]), 1)
            w = j["waiting"][0]
            self.assertEqual((w["desk"], w["run"], w["cls"]), ("s-alpha", None, "write"))
            self.assertIsInstance(w["since"], float)
            # cached with the desks snapshot: a second read is the same object's data, no new tmux scan
            env.clear_calls()
            s, j2 = env.get("/api/tree")
            self.assertEqual(j2, j)
            self.assertEqual([c for c in env.calls("tmux") if c[:1] == ["list-panes"]], [])
            self.assertEqual(env.calls("mbpython"), [])


if __name__ == "__main__":
    unittest.main()


class MalformedProjectsTests(unittest.TestCase):
    """A hand-written config with a project as a bare string (or paths as one string)
    once crashed /api/tree and every run placement (AttributeError in project_of)."""

    def test_bad_project_entries_are_dropped_and_the_tree_still_answers(self):
        with Env() as env:
            alpha = str(env.projects / "alpha")
            cfg = json.loads(env.config.read_text())
            cfg["projects"] = ["alpha", {"name": "flat", "paths": alpha}, 7,
                               {"name": "alpha", "paths": [alpha, 5, ""]}, {"paths": [alpha]}]
            env.config.write_text(json.dumps(cfg))
            server.configure()
            self.assertEqual(server.CFG["projects"], [{"name": "alpha", "paths": [alpha]},
                                                      {"name": "other", "paths": [alpha]}])
            server.invalidate()
            s, j = env.get("/api/tree")
            self.assertEqual(s, 200, j)
            self.assertEqual([p["name"] for p in j["projects"]], ["alpha", "other"])
            s, j = env.post("/api/run", {"task": "x", "engine": "claude", "cwd": alpha})
            self.assertEqual(s, 200, j)
