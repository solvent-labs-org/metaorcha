"""T0: the scripted fake engine behind every self-test journey. An OpenAI-compatible
chat server on 127.0.0.1 only, standard library only, that answers from a scenario
file: one JSON line per model turn. The turn is the number of assistant messages
already in the request, so one engine serves concurrent runs with no per-run state,
and the request's ``model`` names the scenario."""
import http.client
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from metabot_app.selftest import fake_engine  # noqa: E402

TOOL = {"tool_call": {"name": "write_file", "args": {"path": "a.txt", "content": "hi"}}}


def post(port, body, path="/v1/chat/completions"):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request("POST", path, json.dumps(body), {"Content-Type": "application/json"})
    r = c.getresponse()
    raw = r.read()
    c.close()
    return r.status, raw


def chat(port, model, assistant_turns=0, user="do it"):
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": user}]
    for i in range(assistant_turns):
        messages.append({"role": "assistant", "content": f"turn {i}"})
        messages.append({"role": "tool", "tool_call_id": f"c{i}", "content": "ok"})
    return post(port, {"model": model, "messages": messages})


class ScenarioTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="metachat-fake-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        lines = [TOOL, {"answer": "wrote it", "usage": {"prompt_tokens": 7, "completion_tokens": 3}}]
        (self.dir / "two_steps.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n")
        (self.dir / "faults.jsonl").write_text("\n".join(json.dumps(x) for x in [
            {"error": 500}, {"refuse": "no schema here"}, {"truncate": True},
            {"answer": "late", "delay_ms": 300}]) + "\n")
        self.log = self.dir / "requests.jsonl"
        self.engine = fake_engine.FakeEngine(self.dir, log=self.log)
        self.engine.start()
        self.addCleanup(self.engine.close)
        self.port = self.engine.port

    def test_turn_is_the_count_of_assistant_messages(self):
        status, raw = chat(self.port, "two_steps")
        self.assertEqual(status, 200)
        msg = json.loads(raw)["choices"][0]["message"]
        call = msg["tool_calls"][0]
        self.assertEqual(call["type"], "function")
        self.assertEqual(call["function"]["name"], "write_file")
        self.assertEqual(json.loads(call["function"]["arguments"]), {"path": "a.txt", "content": "hi"})
        self.assertTrue(call["id"])
        status, raw = chat(self.port, "two_steps", assistant_turns=1)
        body = json.loads(raw)
        self.assertEqual(body["choices"][0]["message"]["content"], "wrote it")
        self.assertEqual(body["choices"][0]["finish_reason"], "stop")
        self.assertEqual(body["usage"]["prompt_tokens"], 7)
        self.assertEqual(body["usage"]["total_tokens"], 10)
        self.assertEqual(body["model"], "two_steps")

    def test_past_the_end_the_engine_answers_and_says_so(self):
        status, raw = chat(self.port, "two_steps", assistant_turns=5)
        self.assertEqual(status, 200)
        content = json.loads(raw)["choices"][0]["message"]["content"]
        self.assertEqual(content, fake_engine.OVER)
        self.assertNotIn("tool_calls", json.loads(raw)["choices"][0]["message"])

    def test_concurrent_runs_share_one_engine(self):
        # two runs of the same scenario, interleaved: each is answered by its own turn
        a0 = json.loads(chat(self.port, "two_steps")[1])
        b0 = json.loads(chat(self.port, "two_steps")[1])
        a1 = json.loads(chat(self.port, "two_steps", 1)[1])
        self.assertIn("tool_calls", a0["choices"][0]["message"])
        self.assertIn("tool_calls", b0["choices"][0]["message"])
        self.assertEqual(a1["choices"][0]["message"]["content"], "wrote it")

    def test_unknown_model_echoes_the_last_user_message(self):
        status, raw = chat(self.port, "no-such-scenario", user="what time is it")
        self.assertEqual(status, 200)
        content = json.loads(raw)["choices"][0]["message"]["content"]
        self.assertIn("what time is it", content)
        self.assertIn("No tools were used", content)

    def test_models_lists_the_scenarios_and_echo(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("GET", "/v1/models")
        r = c.getresponse()
        body = json.loads(r.read())
        c.close()
        self.assertEqual(r.status, 200)
        self.assertEqual(sorted(m["id"] for m in body["data"]), ["echo", "faults", "two_steps"])

    def test_fault_lines(self):
        status, raw = chat(self.port, "faults", 0)
        self.assertEqual(status, 500)
        self.assertIn("error", json.loads(raw))
        status, raw = chat(self.port, "faults", 1)
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(raw)["error"]["message"], "no schema here")
        status, raw = chat(self.port, "faults", 2)
        self.assertEqual(status, 200)
        with self.assertRaises(ValueError):
            json.loads(raw)                      # cut off mid-body, as a dying proxy does
        t0 = time.monotonic()
        status, raw = chat(self.port, "faults", 3)
        self.assertGreaterEqual(time.monotonic() - t0, 0.3)
        self.assertEqual(json.loads(raw)["choices"][0]["message"]["content"], "late")

    def test_streaming_is_refused(self):
        status, raw = post(self.port, {"model": "two_steps", "stream": True, "messages": []})
        self.assertEqual(status, 400)

    def test_other_paths_are_404(self):
        self.assertEqual(post(self.port, {}, path="/v1/embeddings")[0], 404)

    def test_every_request_is_logged(self):
        chat(self.port, "two_steps", user="first")
        chat(self.port, "two_steps", 1, user="second")
        rows = [json.loads(l) for l in self.log.read_text().splitlines()]
        self.assertEqual([r["model"] for r in rows], ["two_steps", "two_steps"])
        self.assertEqual([r["turn"] for r in rows], [0, 1])
        self.assertEqual(rows[0]["messages"][-1]["content"], "first")
        self.assertEqual(rows[0]["reply"], "tool_call")
        self.assertEqual(rows[1]["reply"], "answer")

    def test_scenario_helpers_write_lines(self):
        lines = [fake_engine.call("shell", {"command": "ls"}), fake_engine.answer("done"),
                 fake_engine.refused(), fake_engine.error(), fake_engine.truncated(),
                 fake_engine.answer("slow", delay_ms=20)]
        self.assertEqual(lines[0], {"tool_call": {"name": "shell", "args": {"command": "ls"}}})
        self.assertEqual(lines[1]["answer"], "done")
        self.assertEqual(lines[2]["refuse"], fake_engine.REFUSAL)
        self.assertEqual(lines[3], {"error": 500})
        self.assertEqual(lines[4], {"truncate": True})
        self.assertEqual(lines[5]["delay_ms"], 20)
        path = fake_engine.write_scenario(self.dir, "made", lines)
        self.assertEqual(path, self.dir / "made.jsonl")
        self.assertEqual(len(path.read_text().splitlines()), 6)
        self.assertIn("made", fake_engine.scenarios(self.dir))


class ProcessTests(unittest.TestCase):
    """The engine as the runner starts it: a child process, port 0, a port file."""

    def test_main_writes_the_port_and_serves_until_terminated(self):
        d = Path(tempfile.mkdtemp(prefix="metachat-fake-"))
        self.addCleanup(shutil.rmtree, d, True)
        port_file = d / "port"
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
        p = subprocess.Popen([sys.executable, "-m", "metabot_app.selftest.fake_engine",
                              "--port", "0", "--port-file", str(port_file), "--scenarios", str(d)],
                             env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(200):
                if port_file.is_file() and port_file.read_text().strip():
                    break
                time.sleep(0.05)
            port = int(port_file.read_text())
            status, raw = chat(port, "echo", user="ping")
            self.assertEqual(status, 200)
            with socket.socket() as s:   # loopback only: nothing else is bound
                s.settimeout(1)
                self.assertNotEqual(s.connect_ex(("127.0.0.1", port)), 111)
        finally:
            p.send_signal(signal.SIGTERM)
            self.assertEqual(p.wait(timeout=10), 0)


if __name__ == "__main__":
    unittest.main()
