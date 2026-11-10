"""The workflow_chain journey: a four-step workflow across two providers and two accounts,
run by the real core; the parts a unit test can hold without the core."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from metabot_app.selftest import journeys
from metabot_app.selftest.journeys import workflow_chain as wc
from metabot_app.selftest.run import Session


class WorkflowChainTests(unittest.TestCase):
    def test_it_is_in_the_plan_order_after_tools_offline(self):
        order = journeys.ORDER
        self.assertIn("workflow_chain", order)
        self.assertLess(order.index("tools_offline"), order.index("workflow_chain"))
        self.assertLess(order.index("workflow_chain"), order.index("recovery_engine"))

    def test_scenarios_cover_a_plan_a_failing_rung_a_good_rung_and_a_summary(self):
        s = wc.SCENARIOS
        self.assertEqual(set(s), {"wf_plan", "rung_down", "rung_ok", "wf_sum"})
        self.assertEqual(s["wf_plan"][0]["tool_call"]["name"], "list_dir")
        self.assertEqual(s["rung_down"][0]["error"], 500)
        self.assertIn("answer", s["rung_ok"][0])

    def test_the_bridge_speaks_metabot_bridge_1(self):
        with tempfile.TemporaryDirectory() as d:
            job = Path(d) / "job.json"
            job.write_text(
                json.dumps(
                    {
                        "format": "metabot.bridge/1",
                        "prompt": "Review the plan.",
                        "model": None,
                        "cwd": d,
                        "read_only": True,
                    }
                )
            )
            script = Path(d) / "gamma.py"
            script.write_text(wc.BRIDGE)
            out = subprocess.run(
                [sys.executable, str(script), str(job)],
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
            ).stdout
        events = [json.loads(line) for line in out.splitlines() if line.strip()]
        self.assertEqual(
            [e["type"] for e in events], ["started", "text", "usage", "final"]
        )
        self.assertEqual(events[0]["model"], wc.BRIDGE_MODEL)
        self.assertIn("gamma", events[-1]["text"])

    def test_the_workflow_document_chains_two_providers_and_a_ladder(self):
        doc = wc.workflow("/tmp/job")
        self.assertEqual(doc["format"], "metabot.workflow/1")
        self.assertEqual(doc["name"], wc.NAME)
        self.assertEqual(
            [s["kind"] for s in doc["steps"]], ["ask", "ask", "ladder", "ask"]
        )
        self.assertEqual(
            [s.get("engine") for s in doc["steps"]],
            ["openai", wc.BRIDGE_NAME, None, "openai"],
        )
        self.assertTrue(all(s.get("read_only") for s in doc["steps"]))
        self.assertEqual(doc["steps"][2]["checks"], ["test -f notes.txt"])

    def test_the_core_config_names_two_accounts_and_the_ladder(self):
        cfg = wc.core_config(
            {"openai_model": "x"},
            "http://127.0.0.1:1/v1",
            "http://127.0.0.1:2/v1",
            ["python", "/tmp/gamma.py"],
        )
        self.assertEqual(cfg["endpoints"]["alpha"]["key_env"], "ALPHA_KEY")
        self.assertEqual(cfg["endpoints"]["beta"]["key_env"], "BETA_KEY")
        self.assertEqual(cfg["endpoints"]["alpha"]["base_url"], "http://127.0.0.1:1/v1")
        self.assertEqual(cfg["endpoints"]["beta"]["base_url"], "http://127.0.0.1:2/v1")
        self.assertEqual(
            cfg["ladder"],
            [
                {"engine": "openai", "endpoint": "alpha"},
                {"engine": "openai", "endpoint": "beta"},
            ],
        )
        self.assertEqual(cfg["bridges"][wc.BRIDGE_NAME]["read_only"], "honored")
        self.assertEqual(cfg["openai_model"], "x")

    def test_parsers_read_the_run_table_and_the_signed_details(self):
        table = (
            "workflow run mb-1-seal  chain 66f3b4095ad5, approved by wfa-1\n"
            "  plan         ask     ok     mb-1-aaaa  receipt valid\n"
            "  review       ask     ok     mb-1-bbbb  receipt valid\n"
            "  check        ladder  ok     mb-1-cccc  receipt valid\n"
            "  summary      ask     ok     mb-1-dddd  receipt valid\n"
        )
        self.assertEqual(
            wc.parse_run(table),
            (
                "mb-1-seal",
                {
                    "plan": "mb-1-aaaa",
                    "review": "mb-1-bbbb",
                    "check": "mb-1-cccc",
                    "summary": "mb-1-dddd",
                },
            ),
        )
        receipt = {
            "verdicts": [
                {
                    "check": "endpoint",
                    "result": "pass",
                    "detail": "name=beta;base_url=http://127.0.0.1:2/v1",
                },
                {
                    "check": "lineage",
                    "result": "pass",
                    "detail": "parent=mb-1-cccc.1;ladder=mb-1-cccc;rung=2",
                },
            ]
        }
        self.assertEqual(
            wc.detail(receipt, "endpoint"),
            {"name": "beta", "base_url": "http://127.0.0.1:2/v1"},
        )
        self.assertEqual(wc.detail(receipt, "lineage")["rung"], "2")
        self.assertEqual(wc.detail(receipt, "model"), {})


class SessionAdditionsTests(unittest.TestCase):
    def test_core_tty_runs_the_command_at_a_terminal(self):
        with tempfile.TemporaryDirectory() as d:
            s = Session(
                Path(d),
                metabot_bin=sys.executable,
                metabot_python=sys.executable,
                orcha_bin="orcha",
                app_python=sys.executable,
                port=0,
                enforce_budgets=False,
                mode="linux",
                keep=False,
                device_model=None,
            )
            code, text = s.core_tty(
                [
                    "-c",
                    "import sys; print('tty', sys.stdin.isatty(), sys.stdout.isatty())",
                ],
                timeout=20,
            )
        self.assertEqual(code, 0)
        self.assertIn("tty True True", text)

    def test_extra_env_reaches_the_core_environment(self):
        with tempfile.TemporaryDirectory() as d:
            s = Session(
                Path(d),
                metabot_bin=sys.executable,
                metabot_python=sys.executable,
                orcha_bin="orcha",
                app_python=sys.executable,
                port=0,
                enforce_budgets=False,
                mode="linux",
                keep=False,
                device_model=None,
            )
            s.extra_env["ALPHA_KEY"] = "a-1"
            self.assertEqual(s.env()["ALPHA_KEY"], "a-1")
            r = s.core(["-c", "import os; print(os.environ.get('ALPHA_KEY'))"])
        self.assertEqual(r.stdout.strip(), "a-1")


if __name__ == "__main__":
    unittest.main()
