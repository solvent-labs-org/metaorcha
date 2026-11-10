"""4.9 / 4.12: catalogue templates, validators and module manifest checks."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from metabot_app import catalog  # noqa: E402

ROOT = Path(__file__).resolve().parents[1] / "src" / "metabot_app"
READS = """status cost doctor config.show engines.list login.list login.check plugin.list
plugin.show plugin.marketplace.list workflow.list workflow.show workflow.check connect.list
connect.scope connect.test routine.list routine.show routine.firings office.check desks.list
roster.show receipts.list receipts.show receipts.verify sdk.verify
propose suggest ladder engines.manifest""".split()
DESKS = """init chat plugin.new plugin.install plugin.enable plugin.disable plugin.remove
plugin.import-skills workflow.new workflow.add workflow.approve workflow.revoke workflow.run workflow.schedule
connect.add connect.revoke routine.save routine.run routine.enable routine.disable routine.remove
office.trust office.run login.add login.remove roster.set roster.unset receipts.export desks.pick
desks.clean sdk.init sdk.record login.set plugin.new-bridge plugin.marketplace.add
plugin.marketplace.remove plugin.enable-bridges plugin.enable-servers plugin.enable-hooks
workflow.new-ladder workflow.new-office config.set serve doctor.models""".split()


class CatalogTests(unittest.TestCase):
    def test_ids_fixed(self):
        self.assertEqual({e["id"] for e in catalog.ENTRIES if e["kind"] == "read"}, set(READS))
        self.assertEqual({e["id"] for e in catalog.ENTRIES if e["kind"] == "desk"}, set(DESKS))

    def test_templates_sound(self):
        self.assertEqual(catalog.check_templates(), [])
        bad = [{"id": "x", "argv": ["{metabot}", "--name={name}"], "params": [{"name": "name"}]}]
        self.assertTrue(catalog.check_templates(bad))

    def test_param_validators(self):
        ctx = {"metabot": "/m", "orcha": "/o", "metabot_home": "/none", "project_roots": [], "state_dir": "/s"}
        ok = lambda t, v, **k: catalog.validate_param({"name": "p", "type": t, **k}, v, ctx)[0]  # noqa: E731
        self.assertTrue(ok("id", "a.b-c_1"))
        for v in ("../x", "a/b", ".h", "a" * 129, "a..b", 5):
            self.assertFalse(ok("id", v), v)
        self.assertTrue(ok("slug", "work-2"))
        for v in ("Work", "1a", "a" * 25, "a_b"):
            self.assertFalse(ok("slug", v), v)
        self.assertTrue(ok("sha256", "a" * 64))
        self.assertFalse(ok("sha256", "A" * 64))
        self.assertTrue(ok("int", 5, min=1, max=10))
        self.assertTrue(ok("int", "7", min=1, max=10))
        self.assertFalse(ok("int", 11, min=1, max=10))
        self.assertFalse(ok("int", True, min=0, max=10))
        self.assertTrue(ok("decimal", "123456.99"))
        self.assertFalse(ok("decimal", "1234567"))
        self.assertTrue(ok("text", "hello\tworld\n"))
        self.assertFalse(ok("text", "bell\x07"))
        self.assertFalse(ok("text", "x" * 2001))
        self.assertTrue(ok("url", "https://example.test/mcp"))
        self.assertFalse(ok("url", "ftp://x"))
        self.assertFalse(ok("url", "http://a b"))
        self.assertTrue(ok("enum", "a", values=["a"]))
        self.assertFalse(ok("enum", "b", values=["a"]))
        self.assertFalse(ok("path", "/etc"))      # no roots configured
        self.assertFalse(ok("receipt", "run-1"))  # no such file
        self.assertFalse(ok("receipt_path", "run-1"))
        with tempfile.TemporaryDirectory() as d:
            rec = Path(d) / "receipts"
            rec.mkdir()
            (rec / "run-1.json").write_text("{}")
            c2 = dict(ctx, metabot_home=d)
            self.assertEqual(catalog.validate_param({"name": "run", "type": "receipt"}, "run-1", c2),
                             (True, "run-1"))
            self.assertEqual(catalog.validate_param({"name": "run", "type": "receipt_path"}, "run-1", c2),
                             (True, str((rec / "run-1.json").resolve())))
            for bad in ("../run-1", "run-2", "a/b"):
                self.assertFalse(catalog.validate_param({"name": "run", "type": "receipt_path"}, bad, c2)[0])
        self.assertEqual([p["type"] for p in catalog.BY_ID["sdk.verify"]["params"]], ["receipt_path"])
        self.assertEqual([p["type"] for p in catalog.BY_ID["receipts.verify"]["params"]], ["receipt"])

    def test_path_denylist_is_the_home_folder_itself(self):
        # F1 at the function level, with a project root inside the (temp) home
        import os
        with tempfile.TemporaryDirectory() as d:
            home = Path(d) / "home"
            state = Path(d) / "state"
            for sub in ("proj/src", ".ssh/k", ".orcha/x", ".metabot/x", ".claude/x", ".claude-two/x",
                        ".config/x", "work"):
                (home / sub).mkdir(parents=True)
            (state / "accounts" / "x").mkdir(parents=True)
            (state / "other").mkdir(parents=True)
            saved = os.environ.get("HOME")
            os.environ["HOME"] = str(home)
            try:
                ctx = {"metabot": "/m", "orcha": "/o", "metabot_home": "/none",
                       "project_roots": [str(home), str(state)], "state_dir": str(state)}
                for p in ("proj", "proj/src", ".config/x", "work"):
                    self.assertEqual(catalog.path_ok(str(home / p), ctx), str((home / p).resolve()), p)
                self.assertEqual(catalog.path_ok(str(state / "other"), ctx), str((state / "other").resolve()))
                for p in ("", ".ssh", ".ssh/k", ".orcha", ".orcha/x", ".metabot/x", ".claude", ".claude/x",
                          ".claude-two", ".claude-two/x"):
                    self.assertIsNone(catalog.path_ok(str(home / p) if p else str(home), ctx), p)
                self.assertIsNone(catalog.path_ok(str(state / "accounts"), ctx))
                self.assertIsNone(catalog.path_ok(str(state / "accounts" / "x"), ctx))
                self.assertIsNone(catalog.path_ok(str(home / "proj") + "\x00", ctx))
                self.assertIsNone(catalog.path_ok("proj", ctx))
                self.assertIsNone(catalog.path_ok(str(Path(d)), ctx))   # above every root
                # a home that is itself under a root (roots: [d]) is still denied
                ctx2 = dict(ctx, project_roots=[d])
                self.assertIsNone(catalog.path_ok(str(home), ctx2))
                self.assertEqual(catalog.path_ok(str(home / "work"), ctx2), str((home / "work").resolve()))
            finally:
                if saved is None:
                    os.environ.pop("HOME", None)
                else:
                    os.environ["HOME"] = saved

    def test_expand(self):
        ctx = {"metabot": "/m", "orcha": "/o", "metabot_home": "/none", "project_roots": [], "state_dir": "/s"}
        argv, err = catalog.expand("plugin.show", {"name": "demo"}, ctx)
        self.assertEqual((argv, err), (["/m", "plugin", "show", "demo"], None))
        argv, err = catalog.expand("plugin.show", {"name": "{metabot}"}, ctx)
        self.assertIsNone(argv)   # braces are not a safe id, and never re-expanded
        argv, err = catalog.expand("connect.add", {"name": "svc", "url": "https://x.test"}, ctx)
        self.assertEqual(argv, ["/m", "connect", "add", "--url", "https://x.test", "svc"])
        argv, err = catalog.expand("nope", {}, ctx)
        self.assertEqual(err, "unknown id")
        argv, err = catalog.expand("plugin.show", "x", ctx)
        self.assertEqual(err, "bad params")

    def test_builtin_modules_valid_and_complete(self):
        r = catalog.load_modules(ROOT / "modules", "/nonexistent")
        self.assertEqual(r["errors"], [])
        ids = {m["id"] for m in r["modules"]}
        self.assertEqual(ids, {"runs", "accounts", "plugins", "workflows", "connections", "office",
                               "sdk", "desktop", "tools", "a2a"})
        used = {it["catalog"] for m in r["modules"] if m["kind"] == "catalog"
                for s in m["sections"] for it in s["items"]}
        self.assertTrue(used <= set(catalog.BY_ID))

    def test_module_commands_schema(self):
        # section 18: palette commands from modules
        base = {"id": "m", "label": "M", "icon": "m", "order": 1, "kind": "card", "phase": 1,
                "card": {"text": "t", "links": []}}
        good = [{"slash": "a2a", "label": "Run a workflow", "hint": "/a2a <ref>",
                 "args": [{"name": "ref", "type": "text"}], "run": {"catalog": "workflow.run"}},
                {"slash": "ping", "label": "Ping a desk", "args": [{"name": "desk", "type": "desk"},
                                                                   {"name": "text", "type": "text"}],
                 "run": {"send": {"kind": "message"}}},
                {"slash": "pass", "label": "Hand over", "args": [{"name": "desk", "type": "desk"},
                                                                 {"name": "text", "type": "text"}],
                 "run": {"send": {"kind": "handoff"}}},
                {"slash": "login", "label": "Add a login",
                 "args": [{"name": "engine", "type": "enum", "values": ["claude", "codex"]}],
                 "run": {"catalog": "login.add"}}]
        self.assertIsNone(catalog._check_manifest(dict(base, commands=good)))
        self.assertIsNone(catalog._check_manifest(dict(base, commands=[])))
        bad = [
            ("not a list", "x"),
            ("bad command", ["x"]),
            ("bad slash", [{"slash": "A2A", "label": "x", "run": {"catalog": "status"}}]),
            ("bad slash", [{"slash": "/a2a", "label": "x", "run": {"catalog": "status"}}]),
            ("built-in slash", [{"slash": "handoff", "label": "x", "run": {"catalog": "status"}}]),
            ("built-in slash", [{"slash": "run", "label": "x", "run": {"catalog": "status"}}]),
            ("duplicate slash", [{"slash": "a", "label": "x", "run": {"catalog": "status"}},
                                 {"slash": "a", "label": "y", "run": {"catalog": "status"}}]),
            ("bad command label", [{"slash": "a", "label": "x" * 33, "run": {"catalog": "status"}}]),
            ("bad hint", [{"slash": "a", "label": "x", "hint": 5, "run": {"catalog": "status"}}]),
            ("bad args", [{"slash": "a", "label": "x", "args": "x", "run": {"catalog": "status"}}]),
            ("bad arg", [{"slash": "a", "label": "x", "args": [{"name": "Bad", "type": "text"}],
                          "run": {"catalog": "status"}}]),
            ("bad arg type", [{"slash": "a", "label": "x", "args": [{"name": "p", "type": "path"}],
                               "run": {"catalog": "status"}}]),
            ("enum needs values", [{"slash": "a", "label": "x", "args": [{"name": "p", "type": "enum"}],
                                    "run": {"catalog": "status"}}]),
            ("bad run", [{"slash": "a", "label": "x"}]),
            ("bad run", [{"slash": "a", "label": "x", "run": {}}]),
            ("bad run", [{"slash": "a", "label": "x", "run": {"catalog": "status", "send": {"kind": "message"}}}]),
            ("unknown catalog id nope", [{"slash": "a", "label": "x", "run": {"catalog": "nope"}}]),
            ("bad send", [{"slash": "a", "label": "x", "run": {"send": {"kind": "shout"}}}]),
            ("arg names no param: zzz", [{"slash": "a", "label": "x", "args": [{"name": "zzz", "type": "text"}],
                                          "run": {"catalog": "plugin.show"}}]),
            ("send needs desk and text args", [{"slash": "a", "label": "x",
                                                "args": [{"name": "desk", "type": "desk"}],
                                                "run": {"send": {"kind": "message"}}}]),
        ]
        for err, cmds in bad:
            self.assertEqual(catalog._check_manifest(dict(base, commands=cmds)), err, cmds)
        self.assertEqual(catalog.BUILTIN_SLASHES, frozenset({"handoff", "to", "arm", "start", "rename",
                                                             "desks", "run", "new", "all", "desktop"}))
        for slash in ("new", "all", "desktop"):   # 21.3 / 22.5 composer built-ins stay the composer's
            self.assertEqual(catalog._check_manifest(dict(base, commands=[
                {"slash": slash, "label": "x", "run": {"catalog": "status"}}])), "built-in slash")

    def test_builtin_modules_carry_the_section_18_commands(self):
        r = catalog.load_modules(ROOT / "modules", "/nonexistent")
        by = {m["id"]: m for m in r["modules"]}
        slashes = {c["slash"]: (mid, c) for mid, m in by.items() for c in m.get("commands", [])}
        self.assertEqual(slashes["a2a"][0], "workflows")
        self.assertEqual(slashes["a2a"][1]["run"], {"catalog": "workflow.run"})
        self.assertEqual([a["name"] for a in slashes["a2a"][1]["args"]], ["ref"])
        self.assertEqual(slashes["mcp"][0], "connections")
        self.assertEqual(slashes["mcp"][1]["run"], {"catalog": "connect.list"})
        self.assertTrue(set(slashes).isdisjoint(catalog.BUILTIN_SLASHES))

    def test_module_errors_reported_not_raised(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            cases = {
                "notjson.json": "{",
                "list.json": "[]",
                "badid.json": json.dumps({"id": "Bad", "label": "x", "icon": "x", "order": 1, "kind": "page", "page": "runs", "phase": 1}),
                "badlabel.json": json.dumps({"id": "a", "label": "x" * 33, "icon": "x", "order": 1, "kind": "page", "page": "runs", "phase": 1}),
                "badicon.json": json.dumps({"id": "b", "label": "x", "icon": "xx", "order": 1, "kind": "page", "page": "runs", "phase": 1}),
                "badorder.json": json.dumps({"id": "c", "label": "x", "icon": "x", "order": "1", "kind": "page", "page": "runs", "phase": 1}),
                "badkind.json": json.dumps({"id": "d", "label": "x", "icon": "x", "order": 1, "kind": "widget", "phase": 1}),
                "badphase.json": json.dumps({"id": "e", "label": "x", "icon": "x", "order": 1, "kind": "page", "page": "runs", "phase": 3}),
                "badpage.json": json.dumps({"id": "f", "label": "x", "icon": "x", "order": 1, "kind": "page", "page": "admin", "phase": 1}),
                "badcat.json": json.dumps({"id": "g", "label": "x", "icon": "x", "order": 1, "kind": "catalog", "phase": 1,
                                           "sections": [{"title": "t", "items": [{"catalog": "nope", "label": "l"}]}]}),
                "badui.json": json.dumps({"id": "h", "label": "x", "icon": "x", "order": 1, "kind": "catalog", "phase": 1,
                                          "sections": [{"title": "t", "items": [{"catalog": "plugin.show", "label": "l",
                                                                                 "params_ui": {"zzz": {"default": 1}}}]}]}),
                "badcard.json": json.dumps({"id": "i", "label": "x", "icon": "x", "order": 1, "kind": "card", "phase": 1,
                                            "card": {"text": "x" * 601, "links": []}}),
                "good.json": json.dumps({"id": "good", "label": "ok", "icon": "✓", "order": 1, "kind": "card", "phase": 2,
                                         "card": {"text": "fine", "links": [{"label": "l", "catalog": "status"}]}}),
            }
            for name, text in cases.items():
                (d / name).write_text(text)
            r = catalog.load_modules("/nonexistent", d)
            self.assertEqual([m["id"] for m in r["modules"]], ["good"])
            files = sorted(e["file"] for e in r["errors"])
            self.assertEqual(files, sorted(f"user/{n}" for n in cases if n != "good.json"))
            errs = {e["file"]: e["error"] for e in r["errors"]}
            self.assertEqual(errs["user/badcat.json"], "unknown catalog id nope")
            self.assertEqual(errs["user/badpage.json"], "bad page")


if __name__ == "__main__":
    unittest.main()
