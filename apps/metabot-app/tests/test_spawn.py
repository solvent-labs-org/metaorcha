"""Sections 4.6-4.8, 4.10-4.11, 5, 6: spawn, run, cli, kill; exact tmux argv."""
import json
import os
import stat
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))
from harness import Env, FIX, server  # noqa: E402
from metabot_app import mb  # noqa: E402


def unset_pairs(extra=()):
    """The -u pairs the server emits: MetaBot's rule over the merged environment
    (this process, the tmux server's global environment, the known names)."""
    out = []
    for n in mb.unset_list(server.MB, extra):
        out += ["-u", n]
    return out


class SpawnTests(unittest.TestCase):
    def setUp(self):
        self.env = Env().start()
        os.environ["ANTHROPIC_API_KEY"] = "test-only-not-a-key"
        os.environ["CLAUDE_CODE_USE_COWORK_PLUGINS"] = "1"
        self.home = str(self.env.home)
        self.mbhome = str(self.env.mbhome)
        # 25.3 B4: claude and cursor desks open in the first project folder by default
        self.alpha = str((self.env.projects / "alpha").resolve())

    def tearDown(self):
        self.env.stop()

    def new_session_calls(self):
        return [c for c in self.env.calls("tmux") if c[:1] == ["new-session"]]

    def test_spawn_claude_exact_argv(self):
        s, j = self.env.post("/api/spawn", {"kind": "claude"})
        self.assertEqual((s, j), (200, {"ok": True, "name": "claude"}))
        call = self.new_session_calls()[-1]
        reg = json.loads((self.env.state / "desks" / "claude.json").read_text())
        sid = reg["session_id"]   # 11-09: the PC names the Claude session, so the desk can be resumed
        self.assertRegex(sid, r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
        self.assertEqual(call, ["new-session", "-d", "-s", "fc-claude", "-c", self.alpha,
                                "-e", f"METABOT_HOME={self.mbhome}", "--", "env", *unset_pairs(),
                                "bash", "-lc", mb.FIXED_SCRIPT, "metachat", "claude", "--session-id", sid])
        self.assertIn("-u", call)
        self.assertIn("ANTHROPIC_API_KEY", call)
        self.assertNotIn("CLAUDE_CODE_USE_COWORK_PLUGINS", call)
        opts = [c for c in self.env.calls("tmux") if c[:1] == ["set-option"]]
        self.assertEqual(opts[-2:], [["set-option", "-t", "=fc-claude", "status", "off"],
                                     ["set-option", "-t", "=fc-claude", "mouse", "off"]])
        self.assertTrue((server.NAMES_DIR / "claude").is_file())
        reg = json.loads((self.env.state / "desks" / "claude.json").read_text())
        self.assertEqual(reg["kind"], "claude")
        self.assertEqual(reg["cwd"], self.alpha)
        self.assertEqual(reg["argv"], ["claude", "--session-id", sid])
        self.assertIsNone(reg["account"])
        s, j = self.env.post("/api/spawn", {"kind": "claude"})
        self.assertEqual(j["name"], "claude-2")
        s, j = self.env.get("/api/desks?scope=metachat")
        self.assertEqual(sorted(d["name"] for d in j["desks"]), ["claude", "claude-2"])
        self.assertEqual(j["desks"][0]["meta"]["kind"], "claude")

    def test_spawn_shell_and_cursor(self):
        s, j = self.env.post("/api/spawn", {"kind": "shell", "cwd": str(self.env.projects / "alpha")})
        self.assertEqual(j["name"], "shell")
        call = self.new_session_calls()[-1]
        self.assertEqual(call[-6:], ["bash", "-lc", mb.FIXED_SCRIPT, "metachat", "bash", "-l"])
        self.assertEqual(call[5], str(self.env.projects / "alpha"))
        s, j = self.env.post("/api/spawn", {"kind": "cursor"})
        call = self.new_session_calls()[-1]
        self.assertEqual(call[-2:], ["metachat", "cursor-agent"])
        self.assertNotIn("CURSOR_API_KEY", call)   # no account: cursor keys stay

    def test_spawn_claude_with_account(self):
        (self.env.mbhome / "fake-logins.json").write_text(json.dumps(
            [{"name": "work", "engine": "claude", "home": "/acc/work", "max_at_once": 3}]))
        s, j = self.env.post("/api/spawn", {"kind": "claude", "account": "work", "label": "w1"})
        self.assertEqual((s, j), (200, {"ok": True, "name": "w1"}))
        call = self.new_session_calls()[-1]
        self.assertEqual(call[:11], ["new-session", "-d", "-s", "fc-w1", "-c", self.alpha,
                                     "-e", f"METABOT_HOME={self.mbhome}", "-e", "CLAUDE_CONFIG_DIR=/acc/work", "--"])
        s, j = self.env.post("/api/spawn", {"kind": "claude", "account": "nope"})
        self.assertEqual(s, 404)

    def test_spawn_cursor_with_account(self):
        self.env.post("/api/accounts", {"engine": "cursor", "name": "alt"})
        s, j = self.env.post("/api/spawn", {"kind": "cursor", "account": "alt"})
        self.assertEqual(s, 200, j)
        call = self.new_session_calls()[-1]
        folder = str(self.env.state / "accounts/cursor/alt")
        self.assertEqual(call, ["new-session", "-d", "-s", "fc-cursor", "-c", self.alpha,
                                "-e", f"METABOT_HOME={self.mbhome}",
                                "-e", f"XDG_CONFIG_HOME={folder}/config",
                                "-e", f"GH_CONFIG_DIR={self.home}/.config/gh",
                                "-e", f"CURSOR_CONFIG_DIR={self.home}/.cursor",
                                "--", "env", *unset_pairs(mb.CURSOR_UNSET),
                                "bash", "-lc", mb.FIXED_SCRIPT, "metachat", "cursor-agent"])
        self.assertIn("CURSOR_API_KEY", call)
        self.assertIn("CURSOR_AUTH_TOKEN", call)

    def test_spawn_validation(self):
        s, j = self.env.post("/api/spawn", {"kind": "python"})
        self.assertEqual((s, j), (400, {"ok": False, "error": "bad kind"}))
        s, j = self.env.post("/api/spawn", {"kind": "shell", "label": "Bad Name"})
        self.assertEqual((s, j), (400, {"ok": False, "error": "bad label"}))
        for cwd in ("relative", str(self.env.home), str(self.env.home / ".ssh"), "/tmp",
                    str(self.env.state / "accounts"), str(self.env.projects / "missing")):
            s, j = self.env.post("/api/spawn", {"kind": "shell", "cwd": cwd})
            self.assertEqual((s, j), (400, {"ok": False, "error": "bad cwd"}), cwd)
        link = self.env.projects / "link"
        link.symlink_to(self.env.home / ".ssh")
        (self.env.home / ".ssh").mkdir()
        s, j = self.env.post("/api/spawn", {"kind": "shell", "cwd": str(link)})
        self.assertEqual(s, 400)   # a symlink into the denylist is still denied
        self.assertEqual(self.new_session_calls(), [])

    def test_cwd_under_home_allowed_home_itself_denied(self):
        # F1: the denylist names the home folder ITSELF, not everything under it
        with Env() as env:
            cfg = json.loads(env.config.read_text())
            cfg["project_roots"] = [str(env.home), str(env.state)]
            env.config.write_text(json.dumps(cfg))
            server.configure()
            proj = env.home / "proj"
            proj.mkdir()
            (env.home / ".ssh").mkdir()
            (env.home / ".ssh" / "keys").mkdir()
            for d in (".orcha", ".metabot", ".claude-work", ".claude", ".config"):
                (env.home / d / "sub").mkdir(parents=True, exist_ok=True)
            (env.state / "accounts" / "claude" / "x").mkdir(parents=True)
            (env.state / "prompts").mkdir(exist_ok=True)
            ok = [str(proj), str(env.home / ".config" / "sub"), str(env.state / "prompts")]
            for cwd in ok:
                s, j = env.post("/api/spawn", {"kind": "shell", "cwd": cwd})
                self.assertEqual(s, 200, (cwd, j))
                reg = json.loads((env.state / "desks" / f"{j['name']}.json").read_text())
                self.assertEqual(reg["cwd"], cwd)
            bad = [str(env.home), str(env.home / ".ssh"), str(env.home / ".ssh" / "keys"),
                   str(env.home / ".orcha" / "sub"), str(env.home / ".metabot"),
                   str(env.home / ".claude" / "sub"), str(env.home / ".claude-work"),
                   str(env.state / "accounts"), str(env.state / "accounts" / "claude" / "x")]
            for cwd in bad:
                s, j = env.post("/api/spawn", {"kind": "shell", "cwd": cwd})
                self.assertEqual((s, j), (400, {"ok": False, "error": "bad cwd"}), cwd)
            # the same rule for /api/run and a catalogue path param
            s, j = env.post("/api/run", {"task": "x", "engine": "claude", "cwd": str(proj)})
            self.assertEqual(s, 200, j)
            s, j = env.post("/api/run", {"task": "x", "engine": "claude", "cwd": str(env.home)})
            self.assertEqual((s, j), (400, {"ok": False, "error": "bad cwd"}))
            s, j = env.post("/api/cli/desk", {"id": "workflow.add", "params": {"file": str(proj)}})
            self.assertEqual(s, 200, j)
            s, j = env.post("/api/cli/desk", {"id": "workflow.add", "params": {"file": str(env.home)}})
            self.assertEqual((s, j), (400, {"ok": False, "error": "bad file: bad path"}))

    def test_spawns_are_serialised_under_the_caps(self):
        # SEC-12: concurrent spawns never exceed max_desks
        import threading
        with Env({"max_desks": 2}) as env:
            results = []
            lock = threading.Lock()

            def one():
                s, j = env.post("/api/spawn", {"kind": "shell"})
                with lock:
                    results.append((s, j))
            threads = [threading.Thread(target=one) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            codes = sorted(s for s, _ in results)
            self.assertEqual(codes, [200, 200] + [409] * 6, results)
            names = sorted(j["name"] for s, j in results if s == 200)
            self.assertEqual(names, ["shell", "shell-2"])
            self.assertEqual(len(list((env.state / "desks").glob("*.json"))), 2)
            self.assertEqual(len([c for c in env.calls("tmux") if c[:1] == ["new-session"]]), 2)
            self.assertNotIn("with ctx.lock:\n        pass", (Path(mb.__file__)).read_text())

    def test_unset_list_covers_the_tmux_server_environment(self):
        # SEC-3: a desk inherits the tmux server's environment, not MetaChat's
        self.env.set_tmux_global_env({"ANTHROPIC_AUTH_TOKEN": "x", "CLAUDE_CODE_OAUTH_TOKEN": "y",
                                      "CLAUDE_CODE_USE_COWORK_PLUGINS": "1", "LANG": "C"})
        os.environ.pop("ANTHROPIC_API_KEY", None)
        names = mb.unset_list(server.MB)
        for n in ("ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY",
                  "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_SKIP_BEDROCK_AUTH", *mb.DROPPED_ENV):
            self.assertIn(n, names)
        for n in ("CLAUDE_CODE_USE_COWORK_PLUGINS", "LANG", "PATH", "HOME"):
            self.assertNotIn(n, names)
        self.assertEqual(names, sorted(set(names)))
        s, j = self.env.post("/api/spawn", {"kind": "claude"})
        call = self.new_session_calls()[-1]
        self.assertIn("ANTHROPIC_AUTH_TOKEN", call)
        self.assertIn("CLAUDE_CODE_OAUTH_TOKEN", call)
        sid = json.loads((self.env.state / "desks" / "claude.json").read_text())["session_id"]
        self.assertEqual(call, ["new-session", "-d", "-s", "fc-claude", "-c", self.alpha,
                                "-e", f"METABOT_HOME={self.mbhome}", "--", "env", *unset_pairs(),
                                "bash", "-lc", mb.FIXED_SCRIPT, "metachat", "claude", "--session-id", sid])
        env_calls = [c for c in self.env.calls("tmux") if c[:1] == ["show-environment"]]
        self.assertEqual(env_calls[-1], ["show-environment", "-g"])

    def test_fixed_script_unsets_sign_in_names_the_profile_exports(self):
        # SEC-3 second half: bash -l re-sources ~/.profile after env -u ran
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory() as home:
            Path(home, ".profile").write_text(
                "export ANTHROPIC_API_KEY=from-profile\nexport CLAUDE_CODE_OAUTH_TOKEN=t\n"
                "export CLAUDE_CODE_USE_BEDROCK=1\nexport CLAUDE_CODE_SKIP_VERTEX_AUTH=1\n"
                "export CLAUDE_CODE_USE_COWORK_PLUGINS=keep\nexport CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=keep\n"
                "export CLAUDECODE=1\nexport CLAUDE_PID=9\nexport KEEP_ME=keep\n")
            probe = ("for v in ANTHROPIC_API_KEY CLAUDE_CODE_OAUTH_TOKEN CLAUDE_CODE_USE_BEDROCK "
                     "CLAUDE_CODE_SKIP_VERTEX_AUTH CLAUDE_CODE_USE_COWORK_PLUGINS "
                     "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC CLAUDECODE CLAUDE_PID KEEP_ME; do "
                     "printf 'PROBE %s=%s\\n' \"$v\" \"${!v-unset}\"; done")
            for script in (mb.FIXED_SCRIPT,):
                r = subprocess.run(["bash", "-lc", script, "metachat", "bash", "-c", probe],
                                   capture_output=True, text=True, timeout=20,
                                   env={"HOME": home, "PATH": os.environ["PATH"], "TERM": "dumb"},
                                   stdin=subprocess.DEVNULL)
                got = dict(ln.split(" ", 1)[1].split("=", 1) for ln in r.stdout.splitlines()
                           if ln.startswith("PROBE "))
                self.assertEqual(got, {"ANTHROPIC_API_KEY": "unset", "CLAUDE_CODE_OAUTH_TOKEN": "unset",
                                       "CLAUDE_CODE_USE_BEDROCK": "unset", "CLAUDE_CODE_SKIP_VERTEX_AUTH": "unset",
                                       "CLAUDE_CODE_USE_COWORK_PLUGINS": "keep",
                                       "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "keep",
                                       "CLAUDECODE": "unset", "CLAUDE_PID": "unset", "KEEP_ME": "keep"},
                                 r.stdout + r.stderr)
                self.assertIn("[metachat] exit 0", r.stdout)
            # the run script: the prompt file is read on fd 3 and unlinked first
            with tempfile.NamedTemporaryFile("w", dir=home, suffix=".txt", delete=False) as f:
                f.write("the prompt")
            r = subprocess.run(["bash", "-lc", mb.FIXED_RUN_SCRIPT, "metachat", f.name, "bash", "-c",
                                "cat; echo; " + probe],
                               capture_output=True, text=True, timeout=20,
                               env={"HOME": home, "PATH": os.environ["PATH"], "TERM": "dumb"},
                               stdin=subprocess.DEVNULL)
            self.assertIn("the prompt", r.stdout)
            self.assertIn("PROBE ANTHROPIC_API_KEY=unset", r.stdout)
            self.assertIn("PROBE KEEP_ME=keep", r.stdout)
            self.assertFalse(os.path.exists(f.name))

    def test_name_clash_409(self):
        self.env.add_session("fc-taken")
        s, j = self.env.post("/api/spawn", {"kind": "shell", "label": "taken"})
        self.assertEqual((s, j), (409, {"ok": False, "error": "name taken"}))
        self.env.post("/api/spawn", {"kind": "shell", "label": "mine"})
        s, j = self.env.post("/api/cli/desk", {"id": "init", "label": "mine"})
        self.assertEqual((s, j), (409, {"ok": False, "error": "name taken"}))
        s, j = self.env.post("/api/spawn", {"kind": "shell"})
        self.assertEqual(j["name"], "shell")

    def test_spawn_caps(self):
        with Env({"max_desks": 2}) as env:
            env.post("/api/spawn", {"kind": "shell"})
            env.post("/api/spawn", {"kind": "shell"})
            s, j = env.post("/api/spawn", {"kind": "shell"})
            self.assertEqual((s, j), (409, {"ok": False, "error": "cap"}))
        with Env({"spawn_per_minute": 1}) as env:
            s, _ = env.post("/api/spawn", {"kind": "shell"})
            self.assertEqual(s, 200)
            s, j = env.post("/api/spawn", {"kind": "shell"})
            self.assertEqual((s, j), (409, {"ok": False, "error": "cap"}))

    def test_run_exact_argv_and_prompt_file(self):
        cwd = str(self.env.projects / "alpha")
        task = "Summarise\tthis\n\x1b[31mred\x07 bell"
        s, j = self.env.post("/api/run", {"task": task, "engine": "cursor", "cwd": cwd,
                                          "max_cost": "0.50"})   # 11-11: a cursor account named here is applied (see below), an unknown one is 404
        self.assertEqual(s, 200, j)
        self.assertEqual(j["name"], "run-1")
        self.assertIs(j["account_used"], False)
        pf = self.env.state / "prompts" / j["prompt_file"]
        self.assertRegex(j["prompt_file"], r"^[0-9a-f]{32}\.txt$")
        self.assertEqual(stat.S_IMODE(pf.stat().st_mode), 0o600)
        self.assertEqual(pf.read_text(), "Summarise\tthis\n[31mred bell")
        argv = [str(FIX / "fake-metabot"), "ask", "-e", "cursor", "--cwd", cwd, "--read-only",
                "--max-cost", "0.50", "-"]
        self.assertEqual(j["argv"], argv)
        call = self.new_session_calls()[-1]
        self.assertEqual(call, ["new-session", "-d", "-s", "fc-run-1", "-c", cwd,
                                "-e", f"METABOT_HOME={self.mbhome}", "--", "env", *unset_pairs(),
                                "bash", "-lc", mb.FIXED_RUN_SCRIPT, "metachat", str(pf), *argv])
        self.assertFalse((server.NAMES_DIR / "run-1").exists())   # runs are not inbox names
        reg = json.loads((self.env.state / "desks" / "run-1.json").read_text())
        self.assertEqual((reg["kind"], reg["engine"], reg["prompt_file"]), ("run", "cursor", pf.name))
        s, j = self.env.get("/api/events?limit=1")
        self.assertEqual(j["events"][0]["kind"], "run")
        self.assertEqual(j["events"][0]["desk"], "run-1")
        self.assertNotIn("task", json.dumps(j["events"][0]["data"]))

    def test_run_with_login_and_ask_mode(self):
        (self.env.mbhome / "fake-logins.json").write_text(json.dumps(
            [{"name": "work", "engine": "claude", "home": "/acc/work", "max_at_once": 3}]))
        cwd = str(self.env.projects / "beta")
        s, j = self.env.post("/api/run", {"task": "x", "engine": "claude", "cwd": cwd, "mode": "ask",
                                          "account": "work", "label": "mine"})
        self.assertEqual(s, 200, j)
        self.assertEqual(j["name"], "run-mine")
        self.assertIs(j["account_used"], True)
        self.assertEqual(j["argv"], [str(FIX / "fake-metabot"), "ask", "-e", "claude", "--login", "work",
                                     "--cwd", cwd, "-"])
        s, j = self.env.post("/api/run", {"task": "x", "engine": "claude", "cwd": cwd, "account": "ghost"})
        self.assertEqual(s, 404)
        s, j = self.env.post("/api/run", {"task": "x", "engine": "codex", "cwd": cwd, "account": "work"})
        self.assertEqual(s, 404)   # a claude login is not a codex one

    def test_run_validation(self):
        cwd = str(self.env.projects / "alpha")
        s, j = self.env.post("/api/run", {"task": "x", "engine": "claude", "cwd": cwd, "mode": "allow-write"})
        self.assertEqual((s, j), (403, {"ok": False, "error": "mode not allowed"}))
        s, j = self.env.post("/api/run", {"task": "", "engine": "claude", "cwd": cwd})
        self.assertEqual(s, 400)
        s, j = self.env.post("/api/run", {"task": "x" * 20001, "engine": "claude", "cwd": cwd})
        self.assertEqual((s, j), (400, {"ok": False, "error": "bad task"}))
        s, j = self.env.post("/api/run", {"task": "x", "engine": "Claude!", "cwd": cwd})
        self.assertEqual((s, j), (400, {"ok": False, "error": "bad engine"}))
        s, j = self.env.post("/api/run", {"task": "x", "engine": "claude"})
        self.assertEqual((s, j), (400, {"ok": False, "error": "bad cwd"}))
        s, j = self.env.post("/api/run", {"task": "x", "engine": "claude", "cwd": cwd, "max_cost": "1.234"})
        self.assertEqual((s, j), (400, {"ok": False, "error": "bad max_cost"}))
        self.assertEqual(self.new_session_calls(), [])
        self.assertEqual(list((self.env.state / "prompts").glob("*")), [])

    def test_run_model_is_passed_to_the_core(self):
        cwd = str(self.env.projects / "alpha")
        s, j = self.env.post("/api/run", {"task": "x", "engine": "cursor", "cwd": cwd, "model": "grok-4.7"})
        self.assertEqual(s, 200, j)
        self.assertEqual(j["argv"], [str(FIX / "fake-metabot"), "ask", "-e", "cursor", "--cwd", cwd,
                                     "--read-only", "-m", "grok-4.7", "-"])
        reg = json.loads((self.env.state / "desks" / "run-1.json").read_text())
        self.assertEqual(reg["model"], "grok-4.7")
        s, j = self.env.post("/api/run", {"task": "x", "engine": "cursor", "cwd": cwd, "model": ""})
        self.assertEqual(s, 200, j)   # empty = the engine's default, as when the field is absent
        self.assertNotIn("-m", j["argv"])
        for bad in ("a b", "x" * 81, "../etc", "-m"):
            s, j = self.env.post("/api/run", {"task": "x", "engine": "cursor", "cwd": cwd, "model": bad})
            self.assertEqual((s, j), (400, {"ok": False, "error": "bad model"}), bad)
        self.assertEqual(len(self.new_session_calls()), 2)

    def test_run_cursor_with_account_applies_its_config_home(self):
        """A Cursor account is a folder, not a core login: the run gets the same
        environment a Cursor desk gets (11-11: runs from the phone pick the account)."""
        self.env.post("/api/accounts", {"engine": "cursor", "name": "alt"})
        cwd = str(self.env.projects / "alpha")
        s, j = self.env.post("/api/run", {"task": "x", "engine": "cursor", "cwd": cwd, "account": "alt"})
        self.assertEqual(s, 200, j)
        self.assertTrue(j["account_used"])
        self.assertNotIn("--login", j["argv"])
        call = self.new_session_calls()[-1]
        folder = str(self.env.state / "accounts/cursor/alt")
        self.assertEqual(call[6:14], ["-e", f"METABOT_HOME={self.mbhome}",
                                      "-e", f"XDG_CONFIG_HOME={folder}/config",
                                      "-e", f"GH_CONFIG_DIR={self.home}/.config/gh",
                                      "-e", f"CURSOR_CONFIG_DIR={self.home}/.cursor"])
        self.assertIn("CURSOR_API_KEY", call)
        self.assertIn("CURSOR_AUTH_TOKEN", call)
        reg = json.loads((self.env.state / "desks" / "run-1.json").read_text())
        self.assertEqual(reg["account"], "alt")
        s, j = self.env.post("/api/run", {"task": "x", "engine": "cursor", "cwd": cwd, "account": "nope"})
        self.assertEqual((s, j), (404, {"ok": False, "error": "no such account"}))
        s, j = self.env.post("/api/run", {"task": "x", "engine": "cursor", "cwd": cwd})
        self.assertEqual(s, 200, j)   # no account: the server's own Cursor sign-in, as before
        self.assertFalse(j["account_used"])
        self.assertNotIn("XDG_CONFIG_HOME", " ".join(self.new_session_calls()[-1]))

    def test_run_cap(self):
        with Env({"max_runs": 1}) as env:
            cwd = str(env.projects / "alpha")
            s, _ = env.post("/api/run", {"task": "x", "engine": "claude", "cwd": cwd})
            self.assertEqual(s, 200)
            s, j = env.post("/api/run", {"task": "x", "engine": "claude", "cwd": cwd})
            self.assertEqual((s, j), (409, {"ok": False, "error": "cap"}))
            s, _ = env.post("/api/spawn", {"kind": "shell"})
            self.assertEqual(s, 200)   # the run cap does not stop a plain desk

    def test_run_cap_frees_when_the_desk_run_ended(self):
        # a run desk keeps its shell open after the run; the slot it held comes back
        with Env({"max_runs": 1}) as env:
            cwd = str(env.projects / "alpha")
            s, _ = env.post("/api/run", {"task": "x", "engine": "claude", "cwd": cwd})
            self.assertEqual(s, 200)
            s, j = env.post("/api/run", {"task": "x", "engine": "claude", "cwd": cwd})
            self.assertEqual((s, j), (409, {"ok": False, "error": "cap"}))
            env.set_pane("fc-run-1", "[metabot] receipt /h/receipts/mb-1.json (valid)\n[metachat] exit 0\n")
            s, j = env.post("/api/run", {"task": "x", "engine": "claude", "cwd": cwd})
            self.assertEqual(s, 200, j)

    def test_prompt_sweep(self):
        p = mb.write_prompt(server.MB, "old")
        os.utime(p, (1, 1))
        q = mb.write_prompt(server.MB, "new")
        self.assertEqual(mb.sweep_prompts(server.MB), 1)
        self.assertFalse(p.exists())
        self.assertTrue(q.exists())

    def test_cli_read(self):
        s, j = self.env.post("/api/cli/read", {"id": "plugin.show", "params": {"name": "demo"}})
        self.assertEqual(s, 200)
        self.assertEqual(j["id"], "plugin.show")
        self.assertEqual(j["argv"], [str(FIX / "fake-metabot"), "plugin", "show", "demo"])
        self.assertEqual(j["exit"], 0)
        self.assertEqual(j["stdout"], "fake metabot: plugin show demo[0m\n")   # control chars gone
        self.assertEqual(j["stderr"], "")
        self.assertIsInstance(j["ms"], int)
        s, j = self.env.post("/api/cli/read", {"id": "plugin.show", "params": {"name": "missing"}})
        self.assertEqual((s, j["ok"], j["exit"]), (200, True, 1))
        self.assertEqual(j["stderr"], "no such plugin: missing\n")
        s, j = self.env.post("/api/cli/read", {"id": "receipts.list", "params": {"limit": 20}})
        self.assertEqual(j["argv"][-2:], ["-n", "20"])
        s, j = self.env.post("/api/cli/read", {"id": "receipts.list", "params": {"limit": 201}})
        self.assertEqual(s, 400)
        s, j = self.env.post("/api/cli/read", {"id": "plugin.show", "params": {}})
        self.assertEqual((s, j), (400, {"ok": False, "error": "missing name"}))
        s, j = self.env.post("/api/cli/read", {"id": "plugin.show", "params": {"name": "../x"}})
        self.assertEqual(s, 400)
        s, j = self.env.post("/api/cli/read", {"id": "nope", "params": {}})
        self.assertEqual((s, j), (404, {"ok": False, "error": "unknown id"}))
        s, j = self.env.post("/api/cli/read", {"id": "plugin.new", "params": {"name": "x"}})
        self.assertEqual(s, 403)
        s, j = self.env.get("/api/events?limit=10")
        ev = [e for e in j["events"] if e["kind"] == "cli_read"]
        self.assertTrue(ev)
        self.assertEqual(ev[-1]["data"]["id"], "plugin.show")
        # the read runs with METABOT_HOME pinned: the fake sees it through its argv log only,
        # so check the argv was the catalogue's and nothing else
        reads = [c for c in self.env.calls("metabot") if c[:2] == ["plugin", "show"]]
        self.assertEqual(reads[0], ["plugin", "show", "demo"])

    def test_cli_read_timeout_502(self):
        server.MB.read_timeout = 1
        os.environ["FAKE_SLEEP"] = "3"
        try:
            s, j = self.env.post("/api/cli/read", {"id": "status", "params": {}})
        finally:
            os.environ.pop("FAKE_SLEEP")
            server.MB.read_timeout = 60
        self.assertEqual((s, j), (502, {"ok": False, "error": "timeout"}))

    def test_cli_desk(self):
        s, j = self.env.post("/api/cli/desk", {"id": "plugin.new", "params": {"name": "demo"}})
        self.assertEqual(s, 200, j)
        self.assertEqual(j["name"], "plugin-new")
        self.assertEqual(j["argv"], [str(FIX / "fake-metabot"), "plugin", "new", "demo"])
        call = self.new_session_calls()[-1]
        self.assertEqual(call[-6:], ["bash", "-lc", mb.FIXED_SCRIPT, "metachat", *j["argv"]][-6:])
        self.assertEqual(call[3], "fc-plugin-new")
        self.assertFalse((server.NAMES_DIR / "plugin-new").exists())
        reg = json.loads((self.env.state / "desks" / "plugin-new.json").read_text())
        self.assertEqual((reg["kind"], reg["catalog"]), ("cli", "plugin.new"))
        s, j = self.env.post("/api/cli/desk", {"id": "plugin.new", "params": {"name": "demo"}})
        self.assertEqual(j["name"], "plugin-new-2")
        s, j = self.env.post("/api/cli/desk", {"id": "plugin.list", "params": {}})
        self.assertEqual(s, 403)
        # login.add: home is a folder NAME under <state>/accounts/<engine>/, made 0700
        # (a path must exist under a project root, which no sign-in folder does)
        s, j = self.env.post("/api/cli/desk", {"id": "login.add", "params": {
            "name": "work", "engine": "codex", "home": "work-home"}})
        self.assertEqual(s, 200, j)
        folder = self.env.state / "accounts" / "codex" / "work-home"
        self.assertEqual(j["argv"][1:], ["login", "add", "--engine", "codex", "--home", str(folder), "work"])
        self.assertTrue(folder.is_dir())
        self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
        for home in (str(self.env.projects / "beta"), "default", "Bad", "~/.claude-work"):
            s, j = self.env.post("/api/cli/desk", {"id": "login.add", "params": {
                "name": "work", "engine": "claude", "home": home}})
            self.assertEqual((s, j), (400, {"ok": False, "error": "bad home: not a slug"}), home)
        s, j = self.env.post("/api/cli/desk", {"id": "login.add", "params": {
            "name": "work", "engine": "gemini", "home": "x"}})
        self.assertEqual(s, 400)
        s, j = self.env.post("/api/cli/desk", {"id": "sdk.record", "params": {"file": "/etc/passwd"}})
        self.assertEqual(s, 400)
        s, j = self.env.get("/api/events?limit=10")
        self.assertIn("cli_desk", [e["kind"] for e in j["events"]])

    def test_kill(self):
        s, j = self.env.post("/api/spawn", {"kind": "shell"})
        name = j["name"]
        s, j = self.env.post("/api/kill", {"name": name})
        self.assertEqual((s, j), (400, {"ok": False, "error": "confirm"}))
        s, j = self.env.post("/api/kill", {"name": "desk-a", "confirm": True})
        self.assertEqual((s, j), (403, {"ok": False, "error": "default desk"}))
        s, j = self.env.post("/api/kill", {"name": "ghost", "confirm": True})
        self.assertEqual((s, j), (404, {"ok": False, "error": "no such desk"}))
        s, j = self.env.post("/api/kill", {"name": "../x", "confirm": True})
        self.assertEqual((s, j), (400, {"ok": False, "error": "bad name"}))
        # a desk that holds the inbox adapter
        self.env.add_session("fc-armed-one")
        self.env.arm("armed-one")
        s, j = self.env.post("/api/kill", {"name": "armed-one", "confirm": True})
        self.assertEqual((s, j), (403, {"ok": False, "error": "armed"}))
        self.assertIn("fc-armed-one", self.env.tmux_sessions())
        s, j = self.env.post("/api/kill", {"name": name, "confirm": True})
        self.assertEqual((s, j), (200, {"ok": True}))
        kills = [c for c in self.env.calls("tmux") if c[:1] == ["kill-session"]]
        self.assertEqual(kills, [["kill-session", "-t", f"=fc-{name}"]])
        self.assertNotIn(f"fc-{name}", self.env.tmux_sessions())
        self.assertFalse((self.env.state / "desks" / f"{name}.json").exists())
        s, j = self.env.get("/api/events?limit=1")
        self.assertEqual((j["events"][0]["kind"], j["events"][0]["desk"]), ("kill", name))
        self.assertFalse((server.NAMES_DIR / name).exists())   # the names-dir file MetaChat wrote

    def test_kill_sweeps_a_registered_desk_whose_session_is_gone(self):
        # F11: a dead tmux session must not keep the registry entry, the name or the slot
        s, j = self.env.post("/api/spawn", {"kind": "claude", "label": "gone"})
        self.assertEqual(s, 200)
        self.assertTrue((server.NAMES_DIR / "gone").is_file())
        st = json.loads(self.env.tmux_state.read_text())
        del st["sessions"]["fc-gone"]
        self.env.tmux_state.write_text(json.dumps(st))
        s, j = self.env.get("/api/desks?scope=metachat")
        self.assertEqual([d["name"] for d in j["desks"]], ["gone"])
        self.env.clear_calls()
        s, j = self.env.post("/api/kill", {"name": "gone", "confirm": True})
        self.assertEqual((s, j), (200, {"ok": True}))
        self.assertFalse((self.env.state / "desks" / "gone.json").exists())
        self.assertFalse((server.NAMES_DIR / "gone").exists())
        self.assertEqual([c for c in self.env.calls("tmux") if c[:1] == ["kill-session"]], [])
        s, j = self.env.get("/api/events?limit=1")
        self.assertEqual((j["events"][0]["kind"], j["events"][0]["desk"]), ("kill", "gone"))
        self.assertTrue(j["events"][0]["data"].get("session_gone"))
        s, j = self.env.post("/api/kill", {"name": "gone", "confirm": True})
        self.assertEqual((s, j), (404, {"ok": False, "error": "no such desk"}))
        s, j = self.env.post("/api/spawn", {"kind": "claude", "label": "gone"})
        self.assertEqual(s, 200)   # the name is free again
        # a legacy desk's names file (not written by MetaChat) is left alone
        self.env.add_session("fc-legacy-k")
        server.NAMES_DIR.mkdir(parents=True, exist_ok=True)
        (server.NAMES_DIR / "legacy-k").write_text("legacy-k\n")
        s, j = self.env.post("/api/kill", {"name": "legacy-k", "confirm": True})
        self.assertEqual(s, 200)
        self.assertTrue((server.NAMES_DIR / "legacy-k").is_file())


if __name__ == "__main__":
    unittest.main()
