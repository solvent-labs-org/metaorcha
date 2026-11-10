"""Contract 25.3 B6-B8 and the Extend module: GET /api/extend, the catalogue
additions, the starter module, and mbq.py's plugins / marketplaces / engines
run against the MetaBot 0.4.0 source itself (in the harness's scratch HOME and
METABOT_HOME) when its venv is on this machine."""
import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))
from harness import Env, FIX, ROOT, server  # noqa: E402
from metabot_app import catalog  # noqa: E402
from metabot_app import mb  # noqa: E402

REPO = Path(__file__).resolve().parents[3]
VENV_PY = Path(os.environ.get("METABOT_PYTHON", REPO / ".venv/bin/python"))
MB_SRC = Path(os.environ.get("METABOT_SRC", REPO / "apps/metabot/src")) / "metabot"
M = str(FIX / "fake-metabot")
BUILTIN_IDS = ["runs", "accounts", "plugins", "workflows", "connections", "office", "sdk", "desktop",
               "tools", "a2a"]
NEW_READS = {"propose": "workflows", "suggest": "runs", "ladder": "runs", "engines.manifest": "runs"}
NEW_DESKS = {"plugin.new-bridge": "plugins", "plugin.marketplace.add": "plugins",
             "plugin.marketplace.remove": "plugins", "plugin.enable-bridges": "plugins",
             "plugin.enable-servers": "plugins", "plugin.enable-hooks": "plugins",
             "workflow.new-ladder": "workflows", "workflow.new-office": "workflows",
             "config.set": "tools", "serve": "workflows", "login.set": "accounts",
             "doctor.models": "tools"}   # follow-up E: probing outlasts the 60 s read cap


class ExtendRouteTests(unittest.TestCase):
    """B6: GET /api/extend."""

    def setUp(self):
        self.env = Env().start()

    def tearDown(self):
        self.env.stop()

    def write(self, sub, obj):
        (self.env.mbhome / f"fake-{sub}.json").write_text(json.dumps(obj))

    def mbq_calls(self, sub):
        return [c for c in self.env.calls("mbpython") if c[1:2] == [sub]]

    def test_shape_names_only(self):
        self.write("plugins", {"plugins": [
            {"name": "demo", "ref": "demo@mk", "marketplace": "mk", "tree": "a" * 64, "version": "0.1.0",
             "enabled": True, "skills": ["demo"], "commands": ["go"], "agents": ["helper"],
             "hooks": {"PreToolUse": 2, "Stop": ["SECRET_HOOK_CMD"]}, "hooks_allowed": False,
             "servers": ["db"], "servers_added": True, "bridges": ["mybridge"], "bridges_on": True,
             "error": None, "source": "/SECRET_SOURCE_PATH", "server_lines": {"db": "SECRET_SERVER_CMD"},
             "env": {"TOKEN": "SECRET_ENV_VALUE"}},
            {"name": "odd", "skills": {"x": "SECRET_SKILL_TEXT"}, "servers": {"db": "SECRET_SERVER_2"},
             "enabled": "yes", "error": "its installed copy does not read"}]})
        self.write("marketplaces", {"marketplaces": [
            {"name": "metabot", "folder": "/b", "error": None, "extra": "SECRET_MARKET",
             "plugins": [{"name": "bizopsmap", "description": "Map a business", "source": "SECRET_SRC"}]},
            {"name": "broken", "folder": "/x", "plugins": [], "error": "PluginError: no marketplace.json"}]})
        self.write("engines", {"engines": [
            {"name": "claude", "kind": "builtin", "local": False, "models": [], "plugin": None},
            {"name": "openai", "kind": "builtin", "local": True, "models": ["qwen2.5:3b"], "plugin": None},
            {"name": "mybridge", "kind": "bridge", "local": False, "models": ["m-1"], "plugin": "demo@mk",
             "command": ["SECRET_BRIDGE_CMD"]}], "error": None})
        s, j = self.env.get("/api/extend")
        self.assertEqual(s, 200)
        self.assertEqual(set(j), {"plugins", "marketplaces", "engines", "modules", "serve", "errors"})
        self.assertEqual(j["plugins"][0], {
            "name": "demo", "ref": "demo@mk", "marketplace": "mk", "tree": "a" * 64, "version": "0.1.0",
            "enabled": True, "skills": ["demo"], "commands": ["go"], "agents": ["helper"],
            "hooks": {"PreToolUse": 2, "Stop": 1}, "hooks_allowed": False, "servers": ["db"],
            "servers_added": True, "bridges": ["mybridge"], "bridges_on": True, "error": None})
        odd = j["plugins"][1]
        self.assertEqual((odd["skills"], odd["servers"], odd["hooks"], odd["enabled"], odd["error"]),
                         ([], [], {}, False, "its installed copy does not read"))
        self.assertEqual(j["marketplaces"], [
            {"name": "metabot", "folder": "/b", "plugins": [{"name": "bizopsmap", "description": "Map a business"}],
             "error": None},
            {"name": "broken", "folder": "/x", "plugins": [], "error": "PluginError: no marketplace.json"}])
        self.assertEqual(j["engines"][2], {"name": "mybridge", "kind": "bridge", "local": False,
                                          "models": ["m-1"], "plugin": "demo@mk"})
        self.assertEqual(j["engines"][1]["local"], True)
        self.assertEqual(j["errors"], [])
        self.assertEqual(j["serve"], {"running": False, "desk": None})
        self.assertNotIn("SECRET", json.dumps(j))

    def test_cached_ten_seconds_fresh_skips(self):
        self.env.clear_calls()
        self.assertEqual(self.env.get("/api/extend")[0], 200)
        self.assertEqual(self.env.get("/api/extend")[0], 200)
        for sub in ("plugins", "marketplaces", "engines"):
            self.assertEqual(len(self.mbq_calls(sub)), 1, sub)
        self.assertEqual(self.env.get("/api/extend?fresh=1")[0], 200)
        for sub in ("plugins", "marketplaces", "engines"):
            self.assertEqual(len(self.mbq_calls(sub)), 2, sub)
        self.assertEqual(mb.EXTEND_TTL, 10.0)
        saved, mb.EXTEND_TTL = mb.EXTEND_TTL, 0.3
        try:
            time.sleep(0.4)
            self.env.get("/api/extend")
        finally:
            mb.EXTEND_TTL = saved
        self.assertEqual(len(self.mbq_calls("plugins")), 3)
        # the modules and serve halves are read on every request, never cached
        (self.env.state / "modules" / "late.json").write_text("{")
        s, j = self.env.get("/api/extend")
        self.assertEqual(len(self.mbq_calls("plugins")), 3)
        self.assertEqual(j["modules"]["user"], [{"id": None, "file": str(self.env.state / "modules" / "late.json"),
                                                 "ok": False, "error": "cannot read: JSONDecodeError"}])

    def test_a_part_that_fails_is_empty_and_named(self):
        os.environ["FAKE_MBQ_FAIL"] = "1"
        s, j = self.env.get("/api/extend?fresh=1")
        self.assertEqual(s, 200)
        self.assertEqual((j["plugins"], j["marketplaces"], j["engines"]), ([], [], []))
        self.assertEqual(j["errors"], [{"part": p, "error": "RuntimeError: boom"}
                                       for p in ("plugins", "marketplaces", "engines")])
        os.environ.pop("FAKE_MBQ_FAIL")
        self.write("engines", {"engines": [{"name": "claude", "kind": "builtin", "local": False,
                                            "models": [], "plugin": None}],
                               "error": "ConfigError: cannot read config.json"})
        s, j = self.env.get("/api/extend?fresh=1")
        self.assertEqual([e["name"] for e in j["engines"]], ["claude"])
        self.assertEqual(j["errors"], [{"part": "engines", "error": "ConfigError: cannot read config.json"}])

    def test_modules_report(self):
        user = self.env.state / "modules"
        self.assertTrue(user.is_dir())
        self.assertEqual(user.stat().st_mode & 0o777, 0o700)
        (user / "broken.json").write_text(json.dumps({
            "id": "broken", "label": "B", "icon": "x", "order": 1, "kind": "catalog", "phase": 1,
            "sections": [{"title": "t", "items": [{"catalog": "x.y", "label": "l"}]}]}))
        (user / "good.json").write_text(json.dumps({
            "id": "good", "label": "ok", "icon": "✓", "order": 1, "kind": "card", "phase": 2,
            "card": {"text": "fine", "links": []}}))
        (user / "twin.json").write_text(json.dumps({
            "id": "good", "label": "ok", "icon": "✓", "order": 1, "kind": "card", "phase": 2,
            "card": {"text": "the later file with this id wins", "links": []}}))
        s, j = self.env.get("/api/extend")
        self.assertEqual(j["modules"]["builtin"], BUILTIN_IDS)
        self.assertEqual(j["modules"]["folder"], str(user))
        self.assertEqual(j["modules"]["user"], [
            {"id": "broken", "file": str(user / "broken.json"), "ok": False, "error": "unknown catalog id x.y"},
            {"id": "good", "file": str(user / "good.json"), "ok": False,
             "error": "another file has this id: twin.json"},
            {"id": "good", "file": str(user / "twin.json"), "ok": True, "error": None}])

    def test_a_twin_whose_later_file_is_dropped_loads_neither(self):
        """Of two user files with one id the later takes the id; when the later
        is dropped (its slash is taken), the earlier does not load either."""
        user = self.env.state / "modules"
        base = {"id": "dup", "label": "Dup", "icon": "x", "order": 120, "kind": "catalog",
                "phase": 1, "sections": [{"title": "t", "items": [{"catalog": "status", "label": "s"}]}]}
        (user / "a-dup.json").write_text(json.dumps(base))
        (user / "b-dup.json").write_text(json.dumps(dict(base, commands=[
            {"slash": "mcp", "label": "Mine", "hint": "/mcp", "args": [], "run": {"catalog": "status"}}])))
        loaded = [m["id"] for m in catalog.load_modules(ROOT / "modules", user)["modules"]]
        self.assertNotIn("dup", loaded)          # what a Reload does: neither file loads
        s, j = self.env.get("/api/extend")
        self.assertEqual(j["modules"]["user"], [
            {"id": "dup", "file": str(user / "a-dup.json"), "ok": False,
             "error": "another file has this id: b-dup.json"},
            {"id": "dup", "file": str(user / "b-dup.json"), "ok": False,
             "error": "slash /mcp taken by module connections"}])

    def test_serve_one_at_a_time(self):
        s, j = self.env.post("/api/cli/desk", {"id": "serve"})
        self.assertEqual((s, j["argv"]), (200, [M, "serve"]))
        first = j["name"]
        self.assertEqual(self.env.get("/api/extend")[1]["serve"], {"running": True, "desk": first})
        s, j = self.env.post("/api/cli/desk", {"id": "serve", "label": "serve-again"})
        self.assertEqual((s, j), (409, {"ok": False, "error": "serve already runs"}))
        # serve ended: the desk is a shell now (FIXED_SCRIPT prints the exit line)
        self.env.set_pane(f"fc-{first}", "watching routines\n\n[metachat] exit 1\nuser@host:~$ ")
        self.assertEqual(self.env.get("/api/extend")[1]["serve"], {"running": False, "desk": None})
        s, j = self.env.post("/api/cli/desk", {"id": "serve"})
        self.assertEqual(s, 200, j)
        second = j["name"]
        self.assertNotEqual(second, first)
        self.assertEqual(self.env.get("/api/extend")[1]["serve"], {"running": True, "desk": second})
        # a serve desk that came back after its session closed shows the argv as a note, never runs it
        self.env.set_pane(f"fc-{second}", f"$ {M} serve (finished earlier; not run again)\nuser@host:~$ ")
        self.assertFalse(self.env.get("/api/extend")[1]["serve"]["running"])
        self.env.set_pane(f"fc-{second}", "watching routines\n")
        self.assertTrue(self.env.get("/api/extend")[1]["serve"]["running"])
        self.assertEqual(self.env.post("/api/kill", {"name": second, "confirm": True})[0], 200)
        self.assertEqual(self.env.get("/api/extend")[1]["serve"], {"running": False, "desk": None})


class CatalogAdditionsTests(unittest.TestCase):
    """B7 (and B2's login.set): ids, kinds, argv and validators."""

    def setUp(self):
        self.env = Env().start()
        self.ctx = server.MB.catalog_ctx()
        self.alpha = str((self.env.projects / "alpha").resolve())

    def tearDown(self):
        self.env.stop()

    def argv(self, cid, **params):
        argv, err = catalog.expand(cid, params, self.ctx)
        self.assertIsNone(err, (cid, params))
        return argv

    def err(self, cid, **params):
        return catalog.expand(cid, params, self.ctx)[1]

    def test_ids_kinds_modules_and_labels(self):
        for cid, module in NEW_READS.items():
            self.assertEqual((catalog.BY_ID[cid]["kind"], catalog.BY_ID[cid]["module"]), ("read", module), cid)
        for cid, module in NEW_DESKS.items():
            self.assertEqual((catalog.BY_ID[cid]["kind"], catalog.BY_ID[cid]["module"]), ("desk", module), cid)
        self.assertEqual(len(catalog.ENTRIES), 74)
        self.assertEqual(catalog.check_templates(), [])
        self.assertEqual(catalog.BY_ID["plugin.enable-hooks"]["label"], "Enable with hooks (developer)")
        self.assertEqual(catalog.BY_ID["plugin.enable-bridges"]["label"], "Enable as an engine")
        for cid in (*NEW_READS, *NEW_DESKS):
            words = catalog.BY_ID[cid]["label"].lower().split()
            for word in ("safe", "first", "only", "nobody", "trustless"):
                self.assertNotIn(word, words, cid)

    def test_enable_entries_say_hooks_and_bridges_every_time(self):
        """Follow-up 1: MetaBot 0.4.0 refuses to enable a plugin with hooks or
        bridges until each is allowed or left out, and takes --without-hooks /
        --without-bridges on a plugin that has none; so every enable entry says
        both, and works on any plugin."""
        want = {
            "plugin.enable": ["--without-hooks", "--without-bridges"],
            "plugin.enable-bridges": ["--allow-bridges", "--without-hooks"],
            "plugin.enable-hooks": ["--allow-hooks", "--without-bridges"],
            "plugin.enable-servers": ["--add-servers", "--without-hooks", "--without-bridges"],
        }
        for cid, flags in want.items():
            self.assertEqual(self.argv(cid, name="skilly"), [M, "plugin", "enable", *flags, "skilly"], cid)
        self.assertEqual(catalog.BY_ID["plugin.enable"]["label"], "Enable")

    def test_plugin_names_take_metabots_own_rule(self):
        """Follow-up 2: an installed plugin or marketplace is named by MetaBot's
        rule (lower-case, digits, '.', '-', '_', up to 64), so the entries that
        name one take the id type, as plugin.show does; MetaBot checks its own
        rule. A new bridge and a new marketplace keep the slug type."""
        long = "p" * 30 + ".x_y"
        for cid in ("plugin.enable", "plugin.enable-bridges", "plugin.enable-servers", "plugin.enable-hooks",
                    "plugin.marketplace.remove", "plugin.show", "plugin.disable", "plugin.remove"):
            for good in ("a.b_c", long, "2fa", "skilly"):
                self.assertEqual(self.argv(cid, name=good)[-1], good, (cid, good))
            for bad in ("-x", "demo@mk", "a b", ".hidden", "../x"):
                self.assertEqual(self.err(cid, name=bad), "bad name: not a safe id", (cid, bad))
        self.assertEqual(catalog.BY_ID["plugin.new-bridge"]["params"][0]["type"], "slug")
        self.assertEqual([p["type"] for p in catalog.BY_ID["plugin.marketplace.add"]["params"]], ["slug", "path"])

    def test_reads_argv(self):
        self.assertEqual(self.argv("propose", folder=self.alpha, task="fix the flaky test"),
                         [M, "propose", "--json", "--folder", self.alpha, "fix the flaky test"])
        self.assertEqual(self.argv("suggest", task="review the release notes"),
                         [M, "suggest", "review the release notes"])
        self.assertEqual(self.argv("ladder"), [M, "ladder"])
        self.assertEqual(self.argv("engines.manifest"), [M, "engines", "--manifest"])
        self.assertEqual(self.err("propose", folder="/tmp", task="x"), "bad folder: bad path")
        self.assertEqual(self.err("propose", folder=self.alpha), "missing task")
        self.assertEqual(self.err("suggest", task="bell\x07"), "bad task: bad text")
        s, j = self.env.post("/api/cli/read", {"id": "propose",
                                               "params": {"folder": self.alpha, "task": "fix the flaky test"}})
        self.assertEqual((s, j["argv"], j["exit"]), (200, [M, "propose", "--json", "--folder", self.alpha,
                                                           "fix the flaky test"], 0))
        s, j = self.env.post("/api/cli/read", {"id": "ladder"})
        self.assertEqual((s, j["argv"]), (200, [M, "ladder"]))

    def test_desks_argv(self):
        a = self.alpha
        cases = [
            ("plugin.new-bridge", {"name": "mybridge"}, [M, "plugin", "new", "--bridge", "mybridge"]),
            ("plugin.marketplace.add", {"name": "mk", "folder": a}, [M, "plugin", "marketplace", "add", "mk", a]),
            ("plugin.marketplace.remove", {"name": "mk"}, [M, "plugin", "marketplace", "remove", "mk"]),
            ("plugin.enable-bridges", {"name": "bridgy"},
             [M, "plugin", "enable", "--allow-bridges", "--without-hooks", "bridgy"]),
            ("plugin.enable-servers", {"name": "skilly"},
             [M, "plugin", "enable", "--add-servers", "--without-hooks", "--without-bridges", "skilly"]),
            ("plugin.enable-hooks", {"name": "skilly"},
             [M, "plugin", "enable", "--allow-hooks", "--without-bridges", "skilly"]),
            ("workflow.new-ladder", {"name": "fixer", "folder": a, "prompt": "fix the tests"},
             [M, "workflow", "new", "--ladder", "--cwd", a, "fixer", "fix the tests"]),
            ("workflow.new-office", {"name": "office-run", "folder": a, "prompt": "ship it"},
             [M, "workflow", "new", "--office", a, "--cwd", a, "office-run", "ship it"]),
            ("config.set", {"key": "fan_cap", "value": "3"}, [M, "config", "set", "fan_cap", "3"]),
            ("serve", {}, [M, "serve"]),
            ("doctor.models", {}, [M, "doctor", "--models"]),
            ("login.set", {"name": "work", "n": 2}, [M, "login", "set", "--max-at-once", "2", "work"]),
        ]
        for cid, params, want in cases:
            self.assertEqual(self.argv(cid, **params), want, cid)
        for cid in ("plugin.new-bridge", "plugin.marketplace.add"):
            for bad in ("My_Bridge", "-x", "a" * 25, "demo@mk", "a.b_c"):
                self.assertEqual(self.err(cid, name=bad, folder=a), "bad name: not a slug", (cid, bad))
        for key in ("project_receipts", "fan_cap", "ladder_attempts", "sandbox"):
            for value in ("true", "false", "1", "5"):
                self.assertIsNone(self.err("config.set", key=key, value=value))
        self.assertEqual(self.err("config.set", key="openai_model", value="1"), "bad key: not a choice")
        self.assertEqual(self.err("config.set", key="fan_cap", value="9"), "bad value: not a choice")
        self.assertEqual(self.err("workflow.new-office", name="o", folder="/etc", prompt="p"),
                         "bad folder: bad path")
        # through the route: the desk opens where the bridge folder is written (./NAME)
        s, j = self.env.post("/api/cli/desk", {"id": "plugin.new-bridge", "params": {"name": "mybridge"},
                                               "cwd": a})
        self.assertEqual((s, j["argv"]), (200, [M, "plugin", "new", "--bridge", "mybridge"]))
        reg = json.loads((self.env.state / "desks" / f"{j['name']}.json").read_text())
        self.assertEqual((reg["cwd"], reg["catalog"]), (a, "plugin.new-bridge"))


class StarterModuleTests(unittest.TestCase):
    """B8: POST /api/modules/new."""

    def setUp(self):
        self.env = Env().start()
        self.user = self.env.state / "modules"

    def tearDown(self):
        self.env.stop()

    def test_writes_the_template_and_reloads(self):
        s, j = self.env.post("/api/modules/new", {"id": "mine", "label": "My tools"})
        f = self.user / "mine.json"
        self.assertEqual((s, j), (200, {"ok": True, "file": str(f), "error": None}))
        self.assertEqual(f.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.user.stat().st_mode & 0o777, 0o700)
        m = json.loads(f.read_text())
        self.assertIsNone(catalog._check_manifest(m))
        self.assertEqual((m["id"], m["label"], m["kind"], m["modes"]), ("mine", "My tools", "catalog",
                                                                       ["user", "dev"]))
        self.assertEqual(len(m["sections"]), 1)
        items = m["sections"][0]["items"]
        self.assertEqual(len(items), 2)
        self.assertTrue(all(catalog.BY_ID[it["catalog"]]["kind"] == "read" for it in items))
        self.assertEqual(len(m["commands"]), 1)
        self.assertEqual(m["commands"][0]["slash"], "mine")
        self.assertEqual(catalog.BY_ID[m["commands"][0]["run"]["catalog"]]["kind"], "read")
        s, j = self.env.get("/api/modules")
        self.assertIn("mine", [x["id"] for x in j["modules"]])
        self.assertEqual(j["errors"], [])
        s, j = self.env.get("/api/events?limit=5")
        self.assertEqual((j["events"][0]["kind"], j["events"][0]["data"]["new"]), ("modules_reload", "mine"))
        s, j = self.env.post("/api/modules/new", {"id": "mine", "label": "Again"})
        self.assertEqual((s, j), (409, {"ok": False, "error": "module exists"}))
        self.assertEqual(json.loads(f.read_text())["label"], "My tools")
        s, j = self.env.get("/api/extend")
        self.assertEqual(j["modules"]["user"], [{"id": "mine", "file": str(f), "ok": True, "error": None}])

    def test_refusals_write_nothing(self):
        for body, want in (({"id": "Mine", "label": "x"}, (400, "bad id")),
                           ({"id": "1x", "label": "x"}, (400, "bad id")),
                           ({"id": "", "label": "x"}, (400, "bad id")),
                           ({"id": "../x", "label": "x"}, (400, "bad id")),
                           ({"id": ["x"], "label": "x"}, (400, "bad id")),
                           ({"label": "x"}, (400, "bad id")),
                           ({"id": "ok", "label": ""}, (400, "bad label")),
                           ({"id": "ok", "label": "  "}, (400, "bad label")),
                           ({"id": "ok", "label": "x" * 33}, (400, "bad label")),
                           ({"id": "ok", "label": "a\nb"}, (400, "bad label")),
                           ({"id": "ok"}, (400, "bad label")),
                           ({"id": "runs", "label": "Mine"}, (409, "a built-in module has that id"))):
            s, j = self.env.post("/api/modules/new", body)
            self.assertEqual((s, j), (want[0], {"ok": False, "error": want[1]}), body)
        self.assertEqual(list(self.user.iterdir()), [])
        s, j = self.env.post("/api/modules/new", {"id": "ok", "label": "x"}, headers={"X-MetaChat": None})
        self.assertEqual((s, j), (403, {"ok": False, "error": "csrf"}))
        (self.user / "taken.json").symlink_to(self.env.root / "elsewhere.json")   # a dangling link is there
        s, j = self.env.post("/api/modules/new", {"id": "taken", "label": "x"})
        self.assertEqual((s, j), (409, {"ok": False, "error": "module exists"}))
        self.assertFalse((self.env.root / "elsewhere.json").exists())

    def test_starters_never_take_a_taken_slash(self):
        for mid in ("one", "two", "new", "mcp"):   # new: a composer slash; mcp: the connections module's
            s, j = self.env.post("/api/modules/new", {"id": mid, "label": mid})
            self.assertEqual(s, 200, j)
            self.assertIsNone(j["error"], mid)
        s, j = self.env.get("/api/modules")
        self.assertEqual(j["errors"], [])
        slashes = {c["slash"] for m in j["modules"] for c in m.get("commands", [])}
        self.assertTrue({"one", "two", "new-2", "mcp-2"} <= slashes, slashes)
        long_id = "a" * 24
        self.assertEqual(catalog.free_slash(long_id, {long_id}), "a" * 22 + "-2")

    def test_a_desk_can_open_in_the_modules_folder(self):
        s, j = self.env.post("/api/spawn", {"kind": "shell", "cwd": str(self.user)})
        self.assertEqual(s, 200, j)
        reg = json.loads((self.env.state / "desks" / f"{j['name']}.json").read_text())
        self.assertEqual(reg["cwd"], str(self.user.resolve()))
        sub = self.user / "drafts"
        sub.mkdir()
        self.assertEqual(mb.cwd_ok(server.MB, str(sub)), str(sub.resolve()))
        for bad in (self.env.state, self.env.state / "accounts", self.env.state / "desks",
                    self.env.state / "signin", self.user / ".." / "accounts"):
            Path(bad).mkdir(exist_ok=True)
            self.assertIsNone(mb.cwd_ok(server.MB, str(bad)), bad)


class ExtendModuleTests(unittest.TestCase):
    """The plugins module becomes Extend (25.2, 25.4 U3); the workflows module offers 25.4 U4."""

    def setUp(self):
        r = catalog.load_modules(ROOT / "modules", "/nonexistent")
        self.assertEqual(r["errors"], [])
        self.by = {m["id"]: m for m in r["modules"]}

    def items(self, mid):
        return {it["catalog"]: it["label"] for s in self.by[mid]["sections"] for it in s["items"]}

    def test_plugins_module_is_extend(self):
        m = self.by["plugins"]
        self.assertEqual((m["label"], m["modes"], m["kind"]), ("Extend", ["user", "dev"], "catalog"))
        self.assertEqual([s["title"] for s in m["sections"]], ["Make", "Installed", "Marketplaces", "Engines"])
        items = self.items("plugins")
        self.assertTrue({"plugin.new", "plugin.new-bridge", "plugin.import-skills", "plugin.list", "plugin.show",
                         "plugin.enable", "plugin.enable-bridges", "plugin.enable-servers", "plugin.enable-hooks",
                         "plugin.disable", "plugin.remove", "plugin.marketplace.list", "plugin.marketplace.add",
                         "plugin.marketplace.remove", "plugin.install", "engines.list", "engines.manifest",
                         "doctor.models", "ladder"} <= set(items))
        self.assertEqual((items["plugin.new"], items["plugin.new-bridge"]), ("Skill", "Bridge"))
        self.assertEqual(items["plugin.enable-bridges"], "Enable as an engine")
        self.assertEqual(items["plugin.enable-hooks"], "Enable with hooks (developer)")

    def test_workflows_module_leaves_the_task_path_and_serve_to_the_page(self):
        """Follow-up E: the page's guided card owns propose, the keep shapes and
        serve (less clutter); the catalogue ids stay."""
        items = self.items("workflows")
        for cid in ("propose", "workflow.new-ladder", "workflow.new-office", "serve"):
            self.assertNotIn(cid, items)
            self.assertIn(cid, catalog.BY_ID)
        self.assertIn("workflow.new", items)            # the Workflows section keeps its own
        titles = [s["title"] for s in json.loads((ROOT / "modules" / "workflows.json").read_text())["sections"]]
        self.assertNotIn("From a task", titles)


@unittest.skipUnless(VENV_PY.exists() and MB_SRC.is_dir(), "the MetaBot 0.4.0 venv is not on this machine")
class RealMetaBotTests(unittest.TestCase):
    """mbq.py plugins / marketplaces / engines against MetaBot 0.4.0 itself, through
    GET /api/extend, in the harness's scratch HOME and METABOT_HOME. No engine
    runs: the setup only adds a marketplace, installs and enables."""

    SETUP = (
        "import sys\n"
        "from pathlib import Path\n"
        "from metabot import plugins as P\n"
        "P.add_marketplace('mk', Path(sys.argv[1]))\n"
        "for name in ('skilly', 'bridgy'):\n"
        "    assert P.install(name + '@mk', lambda facts, diff: True) is not None\n"
        "P.enable('bridgy', allow_bridges=True)\n"
        "print(P.__file__)\n"
    )

    def setUp(self):
        self.env = Env({"metabot_python": str(VENV_PY)}).start()

    def tearDown(self):
        self.env.stop()

    def make_market(self):
        mk = self.env.root / "market"

        def w(rel, text, mode=None):
            p = mk / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
            if mode:
                p.chmod(mode)

        w(".claude-plugin/marketplace.json", json.dumps({"name": "mk", "plugins": [
            {"name": "skilly", "source": "./skilly", "description": "a skill with a hook and a server"},
            {"name": "bridgy", "source": "./bridgy", "description": "an engine of its own"}]}))
        w("skilly/.claude-plugin/plugin.json",
          json.dumps({"name": "skilly", "version": "0.1.0", "description": "skills"}))
        w("skilly/skills/skilly/SKILL.md", "---\nname: skilly\ndescription: 'Say hello.'\n---\n\n# skilly\n")
        w("skilly/hooks/hooks.json", json.dumps({"hooks": {"PreToolUse": [
            {"matcher": "*", "hooks": [{"type": "command", "command": "echo HOOK_CMD_789"}]}]}}))
        w("skilly/.mcp.json", json.dumps({"mcpServers": {"db": {
            "command": "dbserver", "args": ["--password", "SECRET_VALUE_123"],
            "env": {"DB_TOKEN": "SECRET_ENV_456"}}}}))
        w("bridgy/.claude-plugin/plugin.json",
          json.dumps({"name": "bridgy", "version": "0.2.0", "description": "a bridge"}))
        w("bridgy/bin/bridgy", "#!/usr/bin/env python3\nprint('{}')\n", 0o755)
        w("bridgy/metabot/bridges/bridgy.yaml",
          "command: [bin/bridgy]\nmodels: [m-1]\ntools: own\nread_only: honored\n")
        return mk

    INSTALL = (
        "import sys\n"
        "from pathlib import Path\n"
        "from metabot import plugins as P\n"
        "if 'mk' not in P.marketplaces():\n"
        "    P.add_marketplace('mk', Path(sys.argv[1]))\n"
        "for name in sys.argv[2:]:\n"
        "    assert P.install(name + '@mk', lambda facts, diff: True) is not None\n"
        "print(P.__file__)\n"
    )
    CLI = "import sys\nfrom metabot.cli import main\nsys.exit(main(sys.argv[1:]))\n"

    def metabot(self, argv):
        """The real 0.4.0 CLI for a catalogue argv (its first word is the fake
        metabot the harness configures), in the scratch homes."""
        self.assertEqual(argv[0], M)
        return subprocess.run([str(VENV_PY), "-I", "-c", self.CLI, *argv[1:]], capture_output=True, text=True,
                              env=self.scratch_env(), cwd=str(self.env.home), timeout=120)

    def scratch_env(self):
        return {"HOME": str(self.env.home), "METABOT_HOME": str(self.env.mbhome), "PATH": "/usr/bin:/bin",
                "LANG": "C.UTF-8"}

    def test_every_enable_entry_works_on_any_plugin(self):
        """Follow-up 1, against MetaBot itself: each enable entry's own argv, on
        skilly (a hook and a server) and bridgy (a bridge). Hooks, servers or
        bridges come on only where the entry says so and the plugin has them."""
        mk = self.make_market()
        # a server with no environment values: MetaBot makes only those connections (below)
        (mk / "skilly" / ".mcp.json").write_text(json.dumps({"mcpServers": {"db": {
            "command": "dbserver", "args": ["--port", "5432"]}}}))
        r = subprocess.run([str(VENV_PY), "-I", "-c", self.INSTALL, str(mk), "skilly", "bridgy"],
                           capture_output=True, text=True, env=self.scratch_env(), cwd=str(self.env.home),
                           timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        self.assertTrue(r.stdout.strip().startswith(str(MB_SRC)), r.stdout)
        ctx = server.MB.catalog_ctx()
        want = {   # (hooks_allowed, servers_added, bridges_on) after the enable
            ("plugin.enable", "skilly"): (False, False, False),
            ("plugin.enable", "bridgy"): (False, False, False),
            ("plugin.enable-hooks", "skilly"): (True, False, False),
            ("plugin.enable-hooks", "bridgy"): (False, False, False),
            ("plugin.enable-servers", "skilly"): (False, True, False),
            ("plugin.enable-servers", "bridgy"): (False, False, False),
            ("plugin.enable-bridges", "skilly"): (False, False, False),
            ("plugin.enable-bridges", "bridgy"): (False, False, True),
        }
        for (cid, name), facts in want.items():
            argv, err = catalog.expand(cid, {"name": name}, ctx)
            self.assertIsNone(err, cid)
            r = self.metabot(argv)
            self.assertEqual(r.returncode, 0, (cid, name, r.stderr[-1000:]))
            s, j = self.env.get("/api/extend?fresh=1")
            p = {x["name"]: x for x in j["plugins"]}[name]
            self.assertEqual((p["enabled"], p["hooks_allowed"], p["servers_added"], p["bridges_on"]),
                             (True, *facts), (cid, name))
            argv, err = catalog.expand("plugin.disable", {"name": name}, ctx)
            self.assertEqual(self.metabot(argv).returncode, 0, (cid, name))
        # a server that needs environment values (a token) is never made a connection by
        # enable: MetaBot refuses and says how to add it (the UI shows that, in the desk)
        (mk / "skilly" / ".mcp.json").write_text(json.dumps({"mcpServers": {"db": {
            "command": "dbserver", "env": {"DB_TOKEN": "SECRET_ENV_456"}}}}))
        r = subprocess.run([str(VENV_PY), "-I", "-c", self.INSTALL, str(mk), "skilly"], capture_output=True,
                           text=True, env=self.scratch_env(), cwd=str(self.env.home), timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        r = self.metabot(catalog.expand("plugin.enable-servers", {"name": "skilly"}, ctx)[0])
        self.assertEqual(r.returncode, 1)
        self.assertIn("server db needs environment values", r.stderr)
        r = self.metabot(catalog.expand("plugin.enable", {"name": "skilly"}, ctx)[0])
        self.assertEqual(r.returncode, 0, r.stderr[-1000:])   # plain Enable still works on it

    def test_names_only_from_metabot_itself(self):
        mk = self.make_market()
        r = subprocess.run([str(VENV_PY), "-I", "-c", self.SETUP, str(mk)], capture_output=True, text=True,
                           env=self.scratch_env(), cwd=str(self.env.home), timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        self.assertTrue(r.stdout.strip().startswith(str(MB_SRC)), r.stdout)   # the 0.4.0 tree, not another
        s, j = self.env.get("/api/extend?fresh=1")
        self.assertEqual(s, 200)
        self.assertEqual(j["errors"], [])
        by = {p["name"]: p for p in j["plugins"]}
        self.assertEqual(set(by), {"skilly", "bridgy"})
        sk = by["skilly"]
        self.assertEqual((sk["ref"], sk["marketplace"], sk["version"], sk["enabled"]),
                         ("skilly@mk", "mk", "0.1.0", False))
        self.assertRegex(sk["tree"], r"^[0-9a-f]{64}$")
        self.assertEqual((sk["skills"], sk["commands"], sk["agents"], sk["hooks"], sk["servers"]),
                         (["skilly"], [], [], {"PreToolUse": 1}, ["db"]))
        self.assertEqual((sk["hooks_allowed"], sk["servers_added"], sk["bridges"], sk["bridges_on"], sk["error"]),
                         (False, False, [], False, None))
        br = by["bridgy"]
        self.assertEqual((br["enabled"], br["bridges"], br["bridges_on"], br["hooks"], br["skills"]),
                         (True, ["bridgy"], True, {}, []))
        self.assertEqual(j["marketplaces"][0]["name"], "metabot")
        markets = {m["name"]: m for m in j["marketplaces"]}
        self.assertIn("bizopsmap", [p["name"] for p in markets["metabot"]["plugins"]])
        self.assertEqual(markets["mk"]["folder"], str(mk.resolve()))
        self.assertEqual(markets["mk"]["plugins"], [
            {"name": "skilly", "description": "a skill with a hook and a server"},
            {"name": "bridgy", "description": "an engine of its own"}])
        engines = {e["name"]: e for e in j["engines"]}
        self.assertEqual([e["name"] for e in j["engines"]], ["claude", "codex", "cursor", "openai", "bridgy"])
        self.assertEqual(engines["openai"], {"name": "openai", "kind": "builtin", "local": True,
                                             "models": ["qwen2.5:3b"], "plugin": None})
        self.assertEqual(engines["claude"], {"name": "claude", "kind": "builtin", "local": False,
                                             "models": [], "plugin": None})
        self.assertEqual(engines["bridgy"], {"name": "bridgy", "kind": "bridge", "local": False,
                                             "models": ["m-1"], "plugin": "bridgy@mk"})
        text = json.dumps(j)
        for secret in ("SECRET_VALUE_123", "SECRET_ENV_456", "HOOK_CMD_789", "dbserver", "bin/bridgy"):
            self.assertNotIn(secret, text)
        # mbq.py itself prints names only: the command lines never leave MetaBot's side
        for sub in ("plugins", "marketplaces", "engines"):
            r = subprocess.run([str(VENV_PY), str(ROOT / "mbq.py"), sub], capture_output=True, text=True,
                               env=self.scratch_env(), cwd=str(self.env.home), timeout=60)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            for secret in ("SECRET_VALUE_123", "SECRET_ENV_456", "HOOK_CMD_789", "dbserver", "bin/bridgy"):
                self.assertNotIn(secret, r.stdout, sub)
        # this machine's settings: a remote openai is not local, a set model is listed
        (self.env.mbhome / "config.json").write_text(json.dumps({
            "openai_base_url": "https://models.example.test/v1", "models": {"claude": "sonnet"}}))
        engines = {e["name"]: e for e in self.env.get("/api/extend?fresh=1")[1]["engines"]}
        self.assertFalse(engines["openai"]["local"])
        self.assertEqual(engines["claude"]["models"], ["sonnet"])
        # settings that do not read: the built-in engines stay listed, the error is named
        (self.env.mbhome / "config.json").write_text("{")
        s, j = self.env.get("/api/extend?fresh=1")
        self.assertEqual([e["name"] for e in j["engines"]][:4], ["claude", "codex", "cursor", "openai"])
        self.assertEqual([e["part"] for e in j["errors"]], ["engines"])
        self.assertTrue(j["errors"][0]["error"].startswith("ConfigError: "), j["errors"])


if __name__ == "__main__":
    unittest.main()
