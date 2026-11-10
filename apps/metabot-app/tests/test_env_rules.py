"""6.1: mb.py's env rule equals MetaBot's (diffed against the source tree)."""
import ast
import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from metabot_app import mb  # noqa: E402

REPO = Path(__file__).resolve().parents[3]
MB_SRC = Path(os.environ.get("METABOT_SRC", REPO / "apps/metabot/src")) / "metabot"
BASE = MB_SRC / "engines" / "base.py"
CLAUDE = MB_SRC / "engines" / "claude.py"


def _frozenset_literal(src, name):
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == name for t in node.targets):
            call = node.value
            arg = call.args[0] if isinstance(call, ast.Call) else call
            return frozenset(ast.literal_eval(arg))
    raise KeyError(name)


class EnvRuleTests(unittest.TestCase):
    @unittest.skipUnless(BASE.is_file() and CLAUDE.is_file(),
                         f"MetaBot source tree absent at {MB_SRC}; the -u list is unchecked")
    def test_lists_match_metabot_source(self):
        self.assertEqual(mb.DROPPED_ENV, _frozenset_literal(BASE.read_text(), "DROPPED_ENV"))
        claude_src = CLAUDE.read_text()
        self.assertEqual(mb.SIGN_IN_WORDS, _frozenset_literal(claude_src, "SIGN_IN_WORDS"))
        self.assertEqual(mb.NOT_A_PROVIDER, _frozenset_literal(claude_src, "NOT_A_PROVIDER"))
        # the decision function, line for line (whitespace-insensitive body)
        m = re.search(r"def sign_in_variable\(self, name: str\) -> bool:\n((?:        .*\n)+)", claude_src)
        self.assertIsNotNone(m)
        theirs = [ln.strip() for ln in m.group(1).splitlines() if ln.strip()]
        import inspect
        ours = [ln.strip() for ln in inspect.getsource(mb.sign_in_variable).splitlines()[2:] if ln.strip()]
        self.assertEqual(ours, theirs)

    def test_rule_behaviour(self):
        self.assertTrue(mb.sign_in_variable("ANTHROPIC_API_KEY"))
        self.assertTrue(mb.sign_in_variable("CLAUDE_CODE_USE_BEDROCK"))
        self.assertFalse(mb.sign_in_variable("CLAUDE_CODE_USE_COWORK_PLUGINS"))
        self.assertTrue(mb.sign_in_variable("CLAUDE_CODE_SKIP_BEDROCK_AUTH"))
        self.assertTrue(mb.sign_in_variable("CLAUDE_CODE_OAUTH_TOKEN"))
        self.assertFalse(mb.sign_in_variable("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"))
        self.assertFalse(mb.sign_in_variable("HOME"))
        names = mb.unset_names({"ANTHROPIC_BASE_URL": "x", "PATH": "y"}, mb.CURSOR_UNSET)
        self.assertEqual(names, sorted(set(mb.DROPPED_ENV) | {"ANTHROPIC_BASE_URL"} | set(mb.CURSOR_UNSET)))
        self.assertEqual(names, sorted(names))

    def test_known_sign_in_names_follow_the_rule(self):
        # SEC-3: the fixed list can never name something the rule would keep
        for n in mb.KNOWN_SIGN_IN:
            self.assertTrue(mb.sign_in_variable(n), n)
        self.assertTrue(mb.KNOWN_SIGN_IN.isdisjoint(mb.DROPPED_ENV))
        self.assertTrue(mb.KNOWN_SIGN_IN.isdisjoint(mb.NOT_A_PROVIDER))

    def test_prelude_mirrors_the_rule(self):
        # the bash copy of sign_in_variable is built from the same constants
        pre = mb.UNSET_PRELUDE
        self.assertTrue(pre.startswith("unset " + " ".join(sorted(mb.DROPPED_ENV)) + "; "))
        for n in mb.NOT_A_PROVIDER:
            self.assertIn(n, pre)
        for w in mb.SIGN_IN_WORDS:
            self.assertIn(f"*_{w}_*", pre)
        self.assertIn('"${!ANTHROPIC_@}" "${!CLAUDE_CODE_@}"', pre)
        self.assertTrue(mb.FIXED_SCRIPT.startswith(pre))
        self.assertTrue(mb.FIXED_RUN_SCRIPT.startswith(pre))
        # the scripts still end the contract's way and never carry client text
        self.assertTrue(mb.FIXED_SCRIPT.endswith('echo "[metachat] exit $s"; exec bash -l'))
        self.assertIn("f=$1; shift; exec 3<\"$f\"; rm -f -- \"$f\";", mb.FIXED_RUN_SCRIPT)
        self.assertNotIn("{", pre.replace("${!", "").replace("${v#", ""))


if __name__ == "__main__":
    unittest.main()
