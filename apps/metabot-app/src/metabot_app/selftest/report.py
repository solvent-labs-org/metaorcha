"""The self-test report: JSON Lines, one line per step, a summary line last.

A step line: ``{ts, session, journey, step, status, ok, ms, budget_ms, origin,
run_id, receipt_sha256, detail}``; ``status`` is pass | fail | skip | invalid
(``invalid``: the page took the step while hidden, so its timing means nothing).
The summary line: ``{summary: true, pass, fail, skipped, invalid, budget_misses,
caps, build, model_source, device, mode}``. The runner writes a line as each
step ends, so a run the server died under keeps every step it took; a report
with no summary line is incomplete and reads as such.

``read`` gives the lines back, ``render_md`` the human table, ``verdict`` the
exit code: 0 clean, 1 a failed step, 2 a budget miss (only where budgets are
enforced), 3 incomplete.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

STATUSES = ("pass", "fail", "skip", "invalid")


class Report:
    def __init__(self, path: Path, session: str, device: dict | None = None, mode: str = "linux",
                 build: str = "", model_source: str = "fake") -> None:
        self.path = Path(path)
        self.session = session
        self.device = dict(device or {})
        self.mode = mode
        self.build = build
        self.model_source = model_source
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _write(self, row: dict) -> None:
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def step(self, journey: str, step: str, ok: bool, ms: float, budget_ms: int | None = None,
             origin: str = "runner", run_id: str | None = None, receipt_sha256: str | None = None,
             detail: str = "", status: str | None = None) -> None:
        status = status or ("pass" if ok else "fail")
        if status not in STATUSES:
            raise ValueError(f"unknown status {status!r}")
        self._write({"ts": time.time(), "session": self.session, "journey": journey, "step": step,
                     "status": status, "ok": bool(ok), "ms": int(ms),
                     "budget_ms": int(budget_ms) if budget_ms is not None else None,
                     "origin": origin, "run_id": run_id, "receipt_sha256": receipt_sha256,
                     "detail": str(detail)[:2000]})

    def skip(self, journey: str, reason: str) -> None:
        self.step(journey, "skipped", True, 0, status="skip", detail=reason)

    def summary(self, caps: dict | None = None) -> dict:
        lines = read(self.path)
        row = {"summary": True, "ts": time.time(), "session": self.session,
               **counts(lines), "caps": dict(caps or {}), "build": self.build,
               "model_source": self.model_source, "device": self.device, "mode": self.mode}
        self._write(row)
        return row


def counts(lines: list[dict]) -> dict:
    steps = [l for l in lines if not l.get("summary")]
    by = {s: sum(1 for l in steps if l.get("status") == s) for s in STATUSES}
    misses = [{"journey": l["journey"], "step": l["step"], "ms": l["ms"], "budget_ms": l["budget_ms"]}
              for l in steps if l.get("status") == "pass" and l.get("budget_ms") is not None
              and l["ms"] > l["budget_ms"]]
    return {"pass": by["pass"], "fail": by["fail"], "skipped": by["skip"], "invalid": by["invalid"],
            "budget_misses": misses}


def read(path: Path) -> list[dict]:
    out = []
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return out
    for raw in text.splitlines():
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def summary_of(lines: list[dict]) -> dict | None:
    for l in reversed(lines):
        if l.get("summary"):
            return l
    return None


def verdict(lines: list[dict], enforce_budgets: bool) -> int:
    s = summary_of(lines)
    if s is None:
        return 3
    if s.get("fail"):
        return 1
    if enforce_budgets and s.get("budget_misses"):
        return 2
    return 0


def render_md(lines: list[dict]) -> str:
    s = summary_of(lines)
    steps = [l for l in lines if not l.get("summary")]
    session = (s or (steps[0] if steps else {})).get("session", "")
    out = [f"# Self-test report {session}", ""]
    if s is None:
        c = counts(steps)
        out.append(f"**incomplete**: no summary line; the run ended early after {len(steps)} step(s) "
                   f"(pass {c['pass']}, fail {c['fail']}).")
    else:
        dev = s.get("device") or {}
        out.append(f"mode {s.get('mode')} · build {s.get('build') or '-'} · engine {s.get('model_source')}"
                   f" · device {dev.get('model', '-')} {dev.get('os', '')} {dev.get('abi', '')}".rstrip())
        out.append(f"**pass {s['pass']} · fail {s['fail']} · skipped {s['skipped']} · invalid {s['invalid']}"
                   f" · budget misses {len(s.get('budget_misses') or [])}**")
        if s.get("caps"):
            out.append(f"caps: `{json.dumps(s['caps'], sort_keys=True)}`")
    out += ["", "| journey | step | status | ms | budget | run | detail |", "|---|---|---|---|---|---|---|"]
    for l in steps:
        budget = l.get("budget_ms")
        over = " **over**" if budget is not None and l.get("status") == "pass" and l["ms"] > budget else ""
        out.append(f"| {l.get('journey')} | {l.get('step')} | {l.get('status')} | {l.get('ms')}{over} | "
                   f"{budget if budget is not None else ''} | {l.get('run_id') or ''} | "
                   f"{str(l.get('detail') or '').replace('|', '/')[:120]} |")
    return "\n".join(out) + "\n"
