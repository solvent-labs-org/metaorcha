"""Real work: a four-step workflow (a DAG of asks and a ladder) across two providers and
two accounts, run by the core and sealed; its receipts name every engine, endpoint and
rung; the exported bundle verifies offline as one chain; a missing account stops the
chain and still leaves receipts that verify.

Providers: the fake OpenAI-compatible engine (two instances = two hosts, two accounts,
``ALPHA_KEY`` and ``BETA_KEY``) and a bridge provider ``gamma`` (``metabot.bridge/1``).
The ladder's first rung is the alpha account, which answers 500; the second is beta."""

from __future__ import annotations

import json

from .. import fake_engine as fe

AXIS = "real work"
NEEDS = ()  # the core alone; the app view is taken when a server is up
BUDGET = {"run (4 steps)": 60_000, "bundle verifies offline": 15_000}
NAME = "chain"
BRIDGE_NAME = "gamma"
BRIDGE_MODEL = "gamma-1"
SCENARIOS = {
    "wf_plan": [
        fe.call("list_dir", {"path": "."}),
        fe.answer("Plan: read notes.txt, then summarise it."),
    ],
    "rung_down": [fe.error(500)],
    "rung_ok": [fe.answer("notes.txt is there; nothing to change.")],
    "wf_sum": [fe.answer("Done: the plan was reviewed and the check passed.")],
}
BRIDGE = f"""import json, sys
job = json.load(open(sys.argv[1], encoding="utf-8"))
say = lambda **e: print(json.dumps(e), flush=True)
say(type="started", session_id="g-1", model=job.get("model") or {BRIDGE_MODEL!r})
say(type="text", text="gamma reviewing")
say(type="usage", data={{"prompt_tokens": 5, "completion_tokens": 2}})
say(type="final", text="Reviewed by provider gamma: the plan is sound.", session_id="g-1")
"""


def workflow(cwd: str) -> dict:
    return {
        "format": "metabot.workflow/1",
        "name": NAME,
        "about": "plan on A, review on gamma, check up the ladder (alpha then beta), summarise on A",
        "cwd": cwd,
        "steps": [
            {
                "name": "plan",
                "kind": "ask",
                "prompt": "Look at the folder and make a plan.",
                "engine": "openai",
                "model": "wf_plan",
                "read_only": True,
            },
            {
                "name": "review",
                "kind": "ask",
                "prompt": "Review the plan.",
                "engine": BRIDGE_NAME,
                "read_only": True,
            },
            {
                "name": "check",
                "kind": "ladder",
                "prompt": "Make sure notes.txt is present.",
                "checks": ["test -f notes.txt"],
                "read_only": True,
            },
            {
                "name": "summary",
                "kind": "ask",
                "prompt": "Summarise.",
                "engine": "openai",
                "model": "wf_sum",
                "read_only": True,
            },
        ],
    }


def core_config(
    cfg: dict, alpha_url: str, beta_url: str, bridge_cmd: list[str]
) -> dict:
    out = dict(cfg)
    out["endpoints"] = {
        "alpha": {"base_url": alpha_url, "model": "rung_down", "key_env": "ALPHA_KEY"},
        "beta": {"base_url": beta_url, "model": "rung_ok", "key_env": "BETA_KEY"},
    }
    out["ladder"] = [
        {"engine": "openai", "endpoint": "alpha"},
        {"engine": "openai", "endpoint": "beta"},
    ]
    out["bridges"] = {
        BRIDGE_NAME: {
            "command": list(bridge_cmd),
            "read_only": "honored",
            "models": [BRIDGE_MODEL],
        }
    }
    return out


def parse_run(text: str) -> tuple[str, dict[str, str]]:
    """The seal's run id and each step's run id from ``workflow run``'s table."""
    seal = ""
    steps: dict[str, str] = {}
    for line in text.splitlines():
        if line.startswith("workflow run "):
            seal = line.split()[2]
        elif line.startswith("  ") and " receipt " in line:
            parts = line.split()
            if len(parts) >= 4 and parts[2] in ("ok", "error"):
                steps[parts[0]] = parts[3]
    return seal, steps


def detail(receipt: dict, check: str) -> dict[str, str]:
    """The ``k=v;k=v`` detail of a signed verdict, as a dict ({} when absent)."""
    rows = list(receipt.get("verdicts") or [])
    for step in receipt.get("steps") or []:
        if isinstance(step, dict):
            rows.extend(step.get("verdicts") or [])
    for v in rows:
        if isinstance(v, dict) and v.get("check") == check:
            out = {}
            for part in str(v.get("detail") or "").split(";"):
                k, sep, val = part.partition("=")
                if sep:
                    out[k.strip()] = val.strip()
            return out
    return {}


def _receipt(s, run_id: str) -> dict:
    return json.loads(s.receipt_path(run_id).read_text(encoding="utf-8"))


def run(s):
    J = "workflow_chain"
    s.set_model("wf_plan")
    beta = fe.FakeEngine(
        s.scenarios, port=0, log=s.logs / "engine-beta-requests.jsonl"
    ).start()
    try:
        _run(s, J, beta)
    finally:
        beta.close()


def _run(s, J: str, beta) -> None:
    bridge = s.root / "gamma.py"
    bridge.write_text(BRIDGE, encoding="utf-8")
    wf_file = s.root / "chain.json"
    (s.job / "notes.txt").write_text(
        "three lines\nof notes\nto summarise\n", encoding="utf-8"
    )

    def configure():
        path = s.home / "config.json"
        cfg = core_config(
            json.loads(path.read_text(encoding="utf-8")),
            s.engine_url,
            beta.url,
            [s.metabot_python, str(bridge)],
        )
        path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
        s.extra_env.update({"ALPHA_KEY": "alpha-account", "BETA_KEY": "beta-account"})
        wf_file.write_text(
            json.dumps(workflow(str(s.job)), indent=2) + "\n", encoding="utf-8"
        )
        r = s.core(["workflow", "check", str(wf_file)])
        assert r.returncode == 0, (
            f"workflow check: {(r.stderr or r.stdout).strip()[:300]}"
        )
        r = s.core(["workflow", "add", str(wf_file)])
        assert r.returncode == 0, (
            f"workflow add: {(r.stderr or r.stdout).strip()[:300]}"
        )
        return True

    if not s.step(J, "two accounts, a bridge, a workflow", configure):
        return

    def approve():
        piped = s.core(["workflow", "approve", NAME, "--yes"])
        assert piped.returncode != 0, (
            "a piped approval was accepted (an approval is given at a terminal)"
        )
        assert "never a script's" in piped.stderr, (
            f"the refusal did not say why: {piped.stderr.strip()[:200]}"
        )
        code, text = s.core_tty(["workflow", "approve", NAME, "--yes"], timeout=60)
        assert code == 0, f"approve exited {code}: {text.strip()[-300:]}"
        assert "wfa-" in text, "no approval id was printed"
        return text.strip()[-120:]

    if not s.step(J, "approve at a terminal", approve):
        return

    def run_wf():
        r = s.core(["workflow", "run", NAME, "-q"], timeout=180)
        seal, steps = parse_run(r.stdout)
        assert r.returncode == 0, (
            f"workflow run exited {r.returncode}: {(r.stderr or r.stdout).strip()[-400:]}"
        )
        assert set(steps) == {"plan", "review", "check", "summary"}, (
            f"steps ran: {sorted(steps)}"
        )
        rows = {
            line.split()[0]: line.split()[2]
            for line in r.stdout.splitlines()
            if " receipt " in line
        }
        assert all(rows.get(n) == "ok" for n in steps), f"steps said {rows}"
        assert "sealed over every step" in r.stdout, "the run was not sealed"
        return {"_report": True, "value": (seal, steps), "run_id": seal}

    got = s.step(J, "run (4 steps)", run_wf, budget_ms=BUDGET["run (4 steps)"])
    if not got:
        return
    seal, steps = got

    def engines_named():
        plan, review, check, summary = (
            _receipt(s, steps[n]) for n in ("plan", "review", "check", "summary")
        )
        assert _verdict(plan, "model") == "openai/wf_plan", "plan's model"
        assert detail(plan, "endpoint").get("base_url") == s.engine_url, (
            "plan ran elsewhere than host A"
        )
        assert _verdict(review, "model") == f"{BRIDGE_NAME}/{BRIDGE_MODEL}", (
            "review's model"
        )
        assert detail(review, "bridge").get("name") == BRIDGE_NAME, (
            "review did not go through the bridge"
        )
        assert detail(review, "bridge").get("source") == "config", (
            "the bridge was not the configured one"
        )
        seal_d = detail(check, "seal")
        assert seal_d.get("kind") == "ladder", f"ladder seal {seal_d}"
        assert seal_d.get("members") == "3", f"ladder members {seal_d}"
        rung1, rung2 = (
            _receipt(s, steps["check"] + ".1"),
            _receipt(s, steps["check"] + ".2"),
        )
        assert detail(rung1, "endpoint").get("name") == "alpha", (
            "rung 1 was not the alpha account"
        )
        assert detail(rung1, "endpoint").get("base_url") == s.engine_url, "alpha's host"
        assert detail(rung1, "lineage").get("rung") == "1", "rung 1's lineage"
        assert detail(rung2, "endpoint").get("name") == "beta", (
            "rung 2 was not the beta account"
        )
        assert detail(rung2, "endpoint").get("base_url") == beta.url, "beta's host"
        assert detail(rung2, "lineage").get("parent") == steps["check"] + ".1", (
            "rung 2 does not follow rung 1"
        )
        checks = _receipt(s, steps["check"] + ".2.checks")
        assert _verdict(checks, "declared_acceptance") == "ok", (
            "the ladder's check did not pass"
        )
        assert _verdict(summary, "model") == "openai/wf_sum", "summary's model"
        top = detail(_receipt(s, seal), "seal")
        assert top == {"kind": "workflow", "name": NAME, "members": "4"}, (
            f"workflow seal {top}"
        )
        assert (
            detail(_receipt(s, seal), "workflow_run")
            .get("approval", "")
            .startswith("wfa-")
        ), "no approval in the seal"
        beta_rows = [
            json.loads(line)
            for line in (s.logs / "engine-beta-requests.jsonl").read_text().splitlines()
        ]
        assert [r["model"] for r in beta_rows] == ["rung_ok"], (
            f"host B served {[r['model'] for r in beta_rows]}"
        )
        return {
            "_report": True,
            "value": {"rungs": 2, "providers": ["openai", BRIDGE_NAME]},
            "run_id": seal,
        }

    s.step(J, "receipts name every engine, account and rung", engines_named)

    def each_verifies():
        ids = [
            seal,
            *steps.values(),
            steps["check"] + ".1",
            steps["check"] + ".2",
            steps["check"] + ".2.checks",
        ]
        for rid in ids:
            ok, _ = s.verify_cli(s.receipt_path(rid))
            assert ok, f"receipt {rid} did not verify"
        ok, sha = s.verify_cli(s.receipt_path(seal))
        return {
            "_report": True,
            "value": len(ids),
            "run_id": seal,
            "receipt_sha256": sha,
        }

    s.step(J, "every receipt verifies", each_verifies)

    def bundle():
        out = s.root / "bundle"
        r = s.core(["receipts", "export", seal, "--out", str(out)], timeout=60)
        assert r.returncode == 0, f"export: {(r.stderr or r.stdout).strip()[-300:]}"
        assert (out / "bundle.json").is_file(), "no bundle.json"
        r = s.core(["receipts", "verify", "--chain", str(out)], timeout=60)
        assert r.returncode == 0, f"chain: {(r.stdout or r.stderr).strip()[:300]}"
        assert r.stdout.startswith("VALID"), f"chain: {r.stdout.strip()[:300]}"
        assert f"workflow {NAME} " in r.stdout, "the chain did not name the workflow"
        assert "approval wfa-" in r.stdout, "the chain did not name the approval"
        assert "7 members" in r.stdout, f"members: {r.stdout.strip()[:300]}"
        return {
            "_report": True,
            "value": r.stdout.strip().splitlines()[0],
            "run_id": seal,
        }

    s.step(
        J,
        "bundle verifies offline",
        bundle,
        budget_ms=BUDGET["bundle verifies offline"],
    )

    def tampered():
        out = s.root / "bundle"
        target = next(
            p for p in (out / "receipts").iterdir() if p.name.endswith(".2.json")
        )
        data = json.loads(target.read_text(encoding="utf-8"))
        for st in data.get("steps") or []:
            for v in st.get("verdicts") or []:
                if v.get("check") == "endpoint":
                    v["detail"] = v["detail"].replace("name=beta", "name=alpha")
        target.write_text(json.dumps(data), encoding="utf-8")
        r = s.core(["receipts", "verify", "--chain", str(out)], timeout=60)
        assert r.returncode != 0, "a rewritten account name still verified (exit 0)"
        assert not r.stdout.startswith("VALID"), (
            "a rewritten account name still verified"
        )
        return (
            r.stdout.strip().splitlines()[0]
            if r.stdout.strip()
            else r.stderr.strip()[:120]
        )

    s.step(J, "a rewritten rung is caught", tampered)

    if s.token:

        def app_view():
            status, body = s.api("GET", f"/api/mb/run/{seal}")
            assert status == 200, f"/api/mb/run {status}"
            assert (body.get("run") or {}).get("run_id") == seal, (
                "the app shows another run"
            )
            return {"_report": True, "value": body.get("receipt_state"), "run_id": seal}

        s.step(J, "the app shows the sealed run", app_view)

    def missing_account():
        s.extra_env.pop("BETA_KEY", None)
        r = s.core(["workflow", "run", NAME, "-q"], timeout=180)
        seal2, steps2 = parse_run(r.stdout)
        assert r.returncode != 0, "the chain passed without the beta account"
        assert "BETA_KEY is not set" in (r.stdout + r.stderr), (
            "the missing account was not named"
        )
        assert (
            "summary" not in steps2 or "  summary      ask     not run" in r.stdout
        ), "summary ran after a failed check"
        assert steps2.get("plan"), "the plan step before the failure left no receipt"
        assert steps2.get("review"), (
            "the review step before the failure left no receipt"
        )
        for rid in (steps2["plan"], steps2["review"]):
            ok, _ = s.verify_cli(s.receipt_path(rid))
            assert ok, f"receipt {rid} of the stopped chain did not verify"
        ok, sha = s.verify_cli(s.receipt_path(seal2))
        assert ok, "the stopped chain's seal did not verify"
        return {
            "_report": True,
            "value": "stopped at check; sealed",
            "run_id": seal2,
            "receipt_sha256": sha,
        }

    s.step(J, "a missing account stops the chain, sealed", missing_account)


def _verdict(receipt: dict, check: str) -> str:
    rows = list(receipt.get("verdicts") or [])
    for step in receipt.get("steps") or []:
        if isinstance(step, dict):
            rows.extend(step.get("verdicts") or [])
    for v in rows:
        if isinstance(v, dict) and v.get("check") == check:
            return str(v.get("detail") or "")
    return ""
