"""Real work: three runs at once through the app, a fourth refused at the cap, every
receipt verified. Overlap is proven from the receipts' own start and finish times, never
from the page. (``office``, the git fan, is December's; this is the November stand-in.)"""
from datetime import datetime

from .. import fake_engine as fe

AXIS = "real work"
NEEDS = ()
AT_ONCE = 3                          # the self-test config's max_runs
BUDGET = {"three runs finish": 30_000}
SCENARIOS = {"slow3": [fe.answer("done, slowly", delay_ms=3000)]}


def when(text):
    """An ISO-8601 time from a receipt (a trailing Z is UTC)."""
    return datetime.fromisoformat(str(text).replace("Z", "+00:00"))


def overlapped(windows):
    """True when every (start, finish) window shares one instant with all the others."""
    starts = [when(a) for a, _ in windows]
    ends = [when(b) for _, b in windows]
    return max(starts) < min(ends)


def run(s):
    s.set_model("slow3")
    J = "concurrency"
    labels = [f"c{i}" for i in range(1, AT_ONCE + 1)]

    def start_three():
        names = []
        for label in labels:
            status, body = s.api("POST", "/api/run", {"task": "take your time", "engine": "openai",
                                                       "mode": "read-only", "cwd": str(s.job), "label": label})
            assert status == 200, f"/api/run {label}: {status} {body.get('error')}"
            names.append(body["name"])
        assert names == [f"run-{x}" for x in labels], names
        return names
    desks = s.step(J, "three runs start at once", start_three)
    if not desks:
        return

    def fourth():
        status, body = s.api("POST", "/api/run", {"task": "one more", "engine": "openai",
                                                   "mode": "read-only", "cwd": str(s.job), "label": "c4"})
        assert (status, body.get("error")) == (409, "cap"), f"the fourth run got {status} {body!r}"
        return "409 cap"
    s.step(J, "a fourth is refused at the cap", fourth)

    def finish():
        rows = [s.wait_run(d) for d in desks]
        ids = [r.get("run_id") for r in rows]
        assert all(ids), rows
        return ids
    run_ids = s.step(J, "three runs finish", finish, budget_ms=BUDGET["three runs finish"])
    if not run_ids:
        return

    def windows():
        out = []
        for rid in run_ids:
            status, body = s.api("GET", f"/api/mb/run/{rid}")
            assert status == 200, f"/api/mb/run/{rid}: {status}"
            view = body.get("run") or {}  # the journal's copy of the signed times
            a, b = view.get("started_at"), view.get("finished_at")
            assert a and b, f"{rid}: no start/finish in the run view"
            out.append((a, b))
        assert overlapped(out), f"the runs did not overlap: {out}"
        return {"_report": True, "value": out, "run_id": run_ids[0]}
    s.step(J, "the runs overlapped", windows)

    def verify_all():
        shas = []
        for rid in run_ids:
            ok, sha = s.verify_cli(s.receipt_path(rid))
            assert ok, f"{rid}: metabot receipts verify did not say VALID"
            shas.append(sha)
        return {"_report": True, "value": shas, "run_id": run_ids[0], "receipt_sha256": shas[0]}
    s.step(J, "every receipt verifies", verify_all)

    def slot_back():
        status, body = s.api("POST", "/api/run", {"task": "after the rush", "engine": "openai",
                                                   "mode": "read-only", "cwd": str(s.job), "label": "c5"})
        assert status == 200, f"/api/run after the three ended: {status} {body.get('error')}"
        row = s.wait_run(body["name"])
        return row.get("run_id")
    s.step(J, "the slot comes back", slot_back)
