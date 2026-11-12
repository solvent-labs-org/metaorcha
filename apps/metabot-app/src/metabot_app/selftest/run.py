"""The self-test runner: the only orchestrator of a self-test session.

It builds a scratch world (a MetaBot home with its own signing key, an app state
folder, a job folder that is the only project root), starts the scripted fake
engine, initialises the core, starts the app server as a child process, runs the
journeys through the app API and the core CLI, verifies every receipt on the same
machine, and writes report.jsonl + report.md. The page driver (driver.js) is a
leaf it opens where a WebView exists; nothing here needs one.

    python -m metabot_app.selftest.run --out DIR [--journeys a,b] [--metabot-bin P]
        [--metabot-python P] [--orcha-bin P] [--app-python P] [--port N]
        [--enforce-budgets] [--keep] [--mode linux|on_device]

Exit code: the report's verdict (0 clean, 1 a failed step, 2 a budget miss where
budgets are enforced, 3 incomplete).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import secrets
import shutil
import signal
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

from . import journeys, report

META_WAIT_S = 30.0
RUN_WAIT_S = 90.0
PASS_ENV = ("PATH", "LANG", "LC_ALL", "TERM", "TMPDIR", "METABOT_ON_DEVICE")


def device_info(model: str = "") -> dict:
    return {"model": model or ("android" if hasattr(sys, "getandroidapilevel") else "linux-pc"),
            "os": platform.system().lower(), "abi": platform.machine()}


class Session:
    def __init__(self, out: Path, *, metabot_bin: str = "", metabot_python: str = "",
                 orcha_bin: str = "", app_python: str = "", port: int = 0, enforce_budgets: bool = False,
                 mode: str = "linux", keep: bool = False, device_model: str = "") -> None:
        self.id = time.strftime("st-%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + secrets.token_hex(2)
        self.root = Path(out) / self.id
        self.userhome = self.root / "userhome"
        self.home = self.root / "home"
        self.state = self.root / "state"
        self.job = self.root / "job"
        self.scenarios = self.root / "scenarios"
        self.logs = self.root / "logs"
        self.shots = self.root / "shots"
        for d in (self.userhome, self.home, self.state, self.job, self.scenarios, self.logs, self.shots):
            d.mkdir(parents=True, exist_ok=True)
        self.state.chmod(0o700)
        self.home.chmod(0o700)
        self.config = self.root / "app-config.json"
        self.metabot_bin = metabot_bin or shutil.which("metabot") or "metabot"
        self.metabot_python = metabot_python or sys.executable
        self.orcha_bin = orcha_bin or shutil.which("orcha") or "orcha"
        self.app_python = app_python or sys.executable
        self.port = port            # 0 = the server picks; the port is read from state/server.json (A6)
        self.enforce_budgets = enforce_budgets
        self.mode = mode
        self.keep = keep
        self.report = report.Report(self.root / "report.jsonl", session=self.id,
                                    device=device_info(device_model), mode=mode)
        self.failed = False
        self.caps: dict = {}
        self.engine: subprocess.Popen | None = None
        self.engine_url = ""
        self.server: subprocess.Popen | None = None
        self.token = ""
        self.journey = ""
        self.extra_env: dict[str, str] = {}   # a journey's own variables (account keys for the core)

    # ── the world ───────────────────────────────────────────────────────────

    def env(self) -> dict:
        env = {k: os.environ[k] for k in PASS_ENV if k in os.environ}
        env.update({"HOME": str(self.userhome), "METABOT_HOME": str(self.home),
                    "ORCHA_KEY_PATH": str(self.home / "key"), "METACHAT_CONFIG": str(self.config),
                    "METACHAT_STATE": str(self.state), "PYTHONDONTWRITEBYTECODE": "1"})
        env.update(self.extra_env)
        return env

    def write_app_config(self) -> None:
        cfg = {"port": self.port, "bind": ["127.0.0.1"], "state_dir": str(self.state),
               "metabot_bin": self.metabot_bin, "metabot_python": self.metabot_python,
               "orcha_bin": self.orcha_bin, "metabot_home": str(self.home),
               "projects": [{"name": "selftest", "paths": [str(self.job)]}],
               "project_roots": [str(self.job)], "default_folder": str(self.job),
               # a run desk stays registered after its run (its shell is kept open to read), so
               # the whole journey set needs more desk slots than runs at once: the run cap is
               # what the journeys test, the desk cap must not be what they hit
               "max_desks": 32, "max_runs": 3, "spawn_per_minute": 30,
               "phone_listener": False, "tmux_socket": f"mb-{self.id}"}
        self.config.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")

    def install_scenarios(self, names: list[str]) -> list[str]:
        from . import fake_engine
        installed = []
        for n in names:
            for sname, lines in journeys.load(n).SCENARIOS.items():
                fake_engine.write_scenario(self.scenarios, sname, lines)
                installed.append(sname)
        return installed

    def set_model(self, scenario: str) -> None:
        path = self.home / "config.json"
        try:
            cfg = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cfg = {}
        cfg["openai_base_url"] = self.engine_url
        cfg["openai_model"] = scenario
        cfg.setdefault("default_engine", "openai")
        path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
        path.chmod(0o600)

    # ── processes ───────────────────────────────────────────────────────────

    def start_engine(self) -> str:
        port_file = self.root / "engine.port"
        log = open(self.logs / "engine.log", "ab")
        self.engine = subprocess.Popen(
            [self.app_python, "-m", "metabot_app.selftest.fake_engine", "--port", "0",
             "--port-file", str(port_file), "--scenarios", str(self.scenarios),
             "--log", str(self.logs / "engine-requests.jsonl")],
            env={**self.env(), "PYTHONPATH": os.environ.get("PYTHONPATH", "")},
            stdout=log, stderr=log, start_new_session=True)
        log.close()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if port_file.is_file() and port_file.read_text().strip():
                self.engine_url = f"http://127.0.0.1:{int(port_file.read_text())}/v1"
                return self.engine_url
            if self.engine.poll() is not None:
                break
            time.sleep(0.05)
        raise RuntimeError("the fake engine did not start")

    def core(self, args: list[str], timeout: float = 120, stdin: str | None = None) -> subprocess.CompletedProcess:
        return subprocess.run([self.metabot_bin, *args], env=self.env(), cwd=str(self.job),
                              capture_output=True, text=True, timeout=timeout, input=stdin)

    def core_tty(self, args: list[str], timeout: float = 120) -> tuple[int, str]:
        """The core at a terminal of its own (a workflow approval is given at a tty, never
        piped): the exit code and everything it wrote."""
        import pty
        import select

        master, slave = pty.openpty()
        try:
            proc = subprocess.Popen([self.metabot_bin, *args], env=self.env(), cwd=str(self.job),
                                    stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
        finally:
            os.close(slave)
        out = bytearray()
        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline:
                ready, _, _ = select.select([master], [], [], 0.2)
                if ready:
                    try:
                        chunk = os.read(master, 4096)
                    except OSError:       # the slave end closed: the child is done
                        break
                    if not chunk:
                        break
                    out += chunk
                elif proc.poll() is not None:
                    break
            else:
                proc.kill()
                proc.wait(timeout=5)
                raise TimeoutError(f"{' '.join(args[:2])} did not finish in {timeout:g} s")
        finally:
            os.close(master)
        code = proc.wait(timeout=timeout)
        return code, out.decode("utf-8", "replace")

    def init_core(self) -> None:
        r = self.core(["init", "--engine", "openai"])
        if r.returncode != 0:
            raise RuntimeError(f"metabot init failed: {r.stderr.strip()[:300]}")

    def start_server(self) -> float:
        """Start the app server; the milliseconds until /api/meta answered."""
        if self.server is not None and self.server.poll() is None:
            raise RuntimeError("the server is already running")
        if not self.config.is_file():
            self.write_app_config()
        log = open(self.logs / "server.log", "ab")
        t0 = time.monotonic()
        self.server = subprocess.Popen(
            [self.app_python, "-m", "metabot_app.cli", "serve"],
            env={**self.env(), "PYTHONPATH": os.environ.get("PYTHONPATH", "")},
            stdout=log, stderr=log, start_new_session=True)
        log.close()
        deadline = t0 + META_WAIT_S
        while time.monotonic() < deadline:
            try:
                self.token = (self.state / "token").read_text().strip()
                rec = json.loads((self.state / "server.json").read_text())
                self.port = int(rec["port"])
            except (OSError, ValueError, KeyError, TypeError):
                self.token = ""
            if self.token and self.port:
                status, body = self.api("GET", "/api/meta")
                if status == 200:
                    self.caps = body.get("caps") or {}
                    self.report.build = str(body.get("build") or "")
                    return (time.monotonic() - t0) * 1000
            if self.server.poll() is not None:
                raise RuntimeError(f"the server exited with {self.server.returncode}")
            time.sleep(0.05)
        raise RuntimeError("the server did not answer /api/meta in time")

    def _end(self, proc: subprocess.Popen | None, sig: int) -> None:
        if proc is None or proc.poll() is not None:
            return
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=5)

    def stop_server(self) -> None:
        self._end(self.server, signal.SIGTERM)

    def kill_server(self) -> None:
        """What a phone's OS does mid-run: the server dies at once, no shutdown."""
        self._end(self.server, signal.SIGKILL)

    def stop_engine(self) -> None:
        self._end(self.engine, signal.SIGTERM)

    def tmux_down(self) -> None:
        """End the session's own tmux server (never the user's): -L mb-<id>."""
        tmux = shutil.which("tmux")
        if tmux:
            subprocess.run([tmux, "-L", f"mb-{self.id}", "kill-server"], capture_output=True, timeout=20)

    # ── the app API ─────────────────────────────────────────────────────────

    def api_raw(self, method: str, path: str, body: dict | None = None) -> tuple[int, bytes]:
        """(status, body bytes) of one app API call; status 0 when the server did not answer."""
        url = f"http://127.0.0.1:{self.port}{path}{'&' if '?' in path else '?'}t={self.token}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Host": f"127.0.0.1:{self.port}", "Content-Type": "application/json", "X-MetaChat": "1"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except (urllib.error.URLError, OSError) as e:
            return 0, json.dumps({"error": str(e)}).encode()

    def api(self, method: str, path: str, body: dict | None = None):
        status, raw = self.api_raw(method, path, body)
        try:
            return status, json.loads(raw or b"{}")
        except ValueError:
            return status, {}

    def wait_run(self, desk: str, deadline_s: float = RUN_WAIT_S, every_s: float = 0.25) -> dict:
        """The run index row for ``desk`` once it is done (wait on state, never events)."""
        deadline = time.monotonic() + deadline_s
        wait = every_s
        seen = None
        while True:
            status, body = self.api("GET", "/api/mb/runs?limit=50")
            if status == 200:
                for row in body.get("runs") or []:
                    if isinstance(row, dict) and row.get("desk") == desk:
                        seen = row
                        if row.get("state") == "done":
                            return row
            if time.monotonic() >= deadline:
                raise AssertionError(f"{desk} did not finish in {deadline_s:g} s"
                                     + (f" (last state {seen.get('state')})" if seen else " (never in the index)"))
            time.sleep(wait)
            wait = min(wait * 1.5, 1.0)

    def verify_cli(self, receipt: Path) -> tuple[bool, str]:
        r = self.core(["receipts", "verify", str(receipt)], timeout=60)
        sha = hashlib.sha256(receipt.read_bytes()).hexdigest()
        return r.returncode == 0 and r.stdout.startswith("VALID"), sha

    def receipt_path(self, run_id: str) -> Path:
        return self.home / "receipts" / f"{run_id}.json"

    # ── steps and journeys ──────────────────────────────────────────────────

    def step(self, journey: str, name: str, fn, budget_ms: int | None = None, origin: str = "runner", **kw):
        t0 = time.monotonic()
        try:
            value = fn()
        except AssertionError as exc:
            self.failed = True
            self.report.step(journey, name, False, (time.monotonic() - t0) * 1000, budget_ms, origin,
                             detail=str(exc) or "assertion failed", **kw)
            return None
        except Exception as exc:  # a crash is a failed step with its traceback, never a dead runner
            self.failed = True
            tb = traceback.format_exc().strip().splitlines()[-1] if traceback.format_exc() else ""
            self.report.step(journey, name, False, (time.monotonic() - t0) * 1000, budget_ms, origin,
                             detail=f"{type(exc).__name__}: {exc} | {tb}"[:2000], **kw)
            return None
        extra = dict(kw)
        if isinstance(value, dict) and value.get("_report"):   # a step may name its run and receipt
            extra.update({k: value.get(k) for k in ("run_id", "receipt_sha256") if value.get(k)})
            value = value.get("value")
        self.report.step(journey, name, True, (time.monotonic() - t0) * 1000, budget_ms, origin, **extra)
        return value

    def run_journey(self, name: str) -> None:
        mod = journeys.load(name)
        missing = [need for need in getattr(mod, "NEEDS", ()) if not self.caps.get(need)]
        if missing:
            self.report.skip(name, "needs " + ", ".join(missing) + " (not in caps)")
            return
        self.journey = name
        mod.run(self)

    def ensure_server(self) -> None:
        if self.server is None or self.server.poll() is not None:
            self.start_server()

    def run(self, names: list[str]) -> int:
        names = journeys.pick(names) if names else journeys.names()
        try:
            self.install_scenarios(names)
            self.start_engine()
            self.init_core()
            self.set_model("ask")
            self.write_app_config()
            for n in names:
                if n != "cold_start":
                    self.ensure_server()
                self.run_journey(n)
        except Exception as exc:
            self.failed = True
            self.report.step(self.journey or "session", "runner", False, 0, detail=f"{type(exc).__name__}: {exc}")
        finally:
            self.stop_server()
            self.stop_engine()
            self.tmux_down()
            self.report.summary(caps=self.caps)
            lines = report.read(self.report.path)
            (self.root / "report.md").write_text(report.render_md(lines), encoding="utf-8")
        return report.verdict(lines, self.enforce_budgets)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="run the MetaBot self-test")
    ap.add_argument("--out", required=True, help="where the session folder is made")
    ap.add_argument("--journeys", default="", help="comma-separated; default: all, in plan order")
    ap.add_argument("--metabot-bin", default="")
    ap.add_argument("--metabot-python", default="")
    ap.add_argument("--orcha-bin", default="")
    ap.add_argument("--app-python", default="")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--mode", default="linux", choices=("linux", "on_device", "remote"))
    ap.add_argument("--device-model", default="")
    ap.add_argument("--enforce-budgets", action="store_true")
    ap.add_argument("--keep", action="store_true", help="keep the scratch world (default: keep the report and logs only)")
    a = ap.parse_args(argv)
    s = Session(Path(a.out), metabot_bin=a.metabot_bin, metabot_python=a.metabot_python, orcha_bin=a.orcha_bin,
                app_python=a.app_python, port=a.port, enforce_budgets=a.enforce_budgets, mode=a.mode,
                keep=a.keep, device_model=a.device_model)
    names = [n for n in a.journeys.split(",") if n] if a.journeys else []
    code = s.run(names)
    if not a.keep:
        for d in (s.userhome, s.home, s.state, s.job, s.scenarios):
            shutil.rmtree(d, ignore_errors=True)
    print((s.root / "report.md").read_text(encoding="utf-8"))
    print(f"report: {s.root / 'report.jsonl'}")
    return code


if __name__ == "__main__":
    sys.exit(main())
