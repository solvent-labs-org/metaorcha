"""Real work with no network: the model drives write_file, read_file and a shell
command through the core's gate, the files exist, the receipt names every tool.
The write run goes through the core CLI with writes allowed (the app's runs are
read-only until it carries asks); a read-only run goes through the app too."""
import json

from .. import fake_engine as fe

AXIS = "real work"
NEEDS = ("desks",)
BUDGET = {"cli run with writes": 15_000, "app read-only run": 10_000}
SCENARIOS = {
    "tools_offline": [fe.call("write_file", {"path": "a.txt", "content": "hello from the scenario\n"}),
                      fe.call("read_file", {"path": "a.txt"}),
                      fe.call("shell", {"command": "ls"}),
                      fe.answer("a.txt was written, read back and listed.")],
    "read_tools": [fe.call("list_dir", {"path": "."}),
                   fe.call("read_file", {"path": "notes.txt"}),
                   fe.answer("notes.txt says: keep going.")],
}


def run(s):
    J = "tools_offline"
    s.set_model("tools_offline")

    def cli_run():
        r = s.core(["ask", "-e", "openai", "--allow-write", "--cwd", str(s.job), "--json", "-q",
                    "make a.txt, read it back and list the folder"], timeout=120)
        assert r.returncode == 0, f"ask exit {r.returncode}: {r.stderr.strip()[:300]}"
        d = json.loads(r.stdout)
        assert d.get("ok") is True, f"run not ok: {d.get('error')}"
        assert d.get("steps") == 3, f"{d.get('steps')} steps"
        return {"_report": True, "value": d, "run_id": d.get("run_id")}
    d = s.step(J, "cli run with writes", cli_run, budget_ms=BUDGET["cli run with writes"])
    if not d:
        return

    def files():
        a = s.job / "a.txt"
        assert a.is_file(), "a.txt was not written"
        assert a.read_text() == "hello from the scenario\n", "a.txt has other content"
    s.step(J, "files exist", files, run_id=d["run_id"])

    def receipt_tools():
        r = json.loads(s.receipt_path(d["run_id"]).read_text())
        tools = [st.get("tool") for st in r.get("steps") or []]
        assert tools == ["metabot/write_file", "metabot/read_file", "metabot/shell"], f"receipt tools {tools}"
        ok, sha = s.verify_cli(s.receipt_path(d["run_id"]))
        assert ok, "the receipt does not verify"
        return {"_report": True, "value": tools, "run_id": d["run_id"], "receipt_sha256": sha}
    s.step(J, "receipt names the tools", receipt_tools)

    # the same through the app, read-only: two read tools and an answer
    (s.job / "notes.txt").write_text("keep going\n")
    s.set_model("read_tools")

    def app_run():
        status, body = s.api("POST", "/api/run", {"task": "what do my notes say?", "engine": "openai",
                                                   "mode": "read-only", "cwd": str(s.job), "label": "tools"})
        assert status == 200, f"/api/run {status}: {body.get('error')}"
        row = s.wait_run(body["name"])
        run_id = row["run_id"]
        r = json.loads(s.receipt_path(run_id).read_text())
        tools = [st.get("tool") for st in r.get("steps") or []]
        assert tools == ["metabot/list_dir", "metabot/read_file"], f"receipt tools {tools}"
        ok, sha = s.verify_cli(s.receipt_path(run_id))
        assert ok, "the receipt does not verify"
        return {"_report": True, "value": tools, "run_id": run_id, "receipt_sha256": sha}
    s.step(J, "app read-only run", app_run, budget_ms=BUDGET["app read-only run"])
