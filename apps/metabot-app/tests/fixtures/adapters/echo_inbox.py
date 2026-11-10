"""A test inbox adapter: the seam's contract with nothing behind it but files.

Loaded by the app from the config key ``inbox_module``. Everything it keeps lives in
``<state>/echo-inbox/``: ``armed.json`` (the desks that hold it), ``sent.jsonl`` (what
it was asked to publish), ``names/`` (its session-name folder). Arming types
``/echo-arm <desk>`` into the desk through the app's own paste rules, as a real
adapter would type its own command."""
import json
import time
from pathlib import Path


class EchoInbox:
    def __init__(self, ctx):
        self.ctx = ctx
        self.box = Path(ctx.state) / "echo-inbox"
        self.starts = 0

    # ---- files
    def _armed_names(self):
        try:
            v = json.loads((self.box / "armed.json").read_text())
        except (OSError, ValueError):
            return []
        return [n for n in v if isinstance(n, str)] if isinstance(v, list) else []

    def set_armed(self, names):
        self.box.mkdir(parents=True, exist_ok=True)
        (self.box / "armed.json").write_text(json.dumps(list(names)))

    def sent(self):
        try:
            return [json.loads(x) for x in (self.box / "sent.jsonl").read_text().splitlines()]
        except OSError:
            return []

    # ---- the protocol
    def meta(self):
        return {"name": "echo inbox", "arm_hint": "this desk's agent holds the echo inbox",
                "arm_command": "/echo-arm {desk}"}

    def start(self, ctx):
        self.starts += 1

    def arm(self, pane):
        ok, err = self.ctx.paste(pane, f"/echo-arm {self.ctx.desk_slug(pane)}")
        if ok:
            self.set_armed(sorted(set(self._armed_names()) | {pane}))
        return {"ok": ok, "error": err}

    def deliver(self, desk, text):
        if desk not in self._armed_names():
            return None
        return self.publish(desk, text)

    def publish(self, desk, text):
        self.box.mkdir(parents=True, exist_ok=True)
        with open(self.box / "sent.jsonl", "a") as f:
            f.write(json.dumps({"desk": desk, "text": text}) + "\n")
        self.ctx.emit({"via": "in", "time": time.time(), "title": "echo", "message": text,
                       "target": desk, "from": ""})
        return {"ok": True, "error": ""}

    def tree_bucket(self, rows):
        return {"label": "Echo", "desks": rows}

    def names_dirs(self):
        return [self.box / "names"]

    def armed(self):
        return {n: {"armed_at": 1, "n": 1} for n in self._armed_names()}

    def streams(self):
        return {"echo@local": True}

    def history(self):
        return [{"via": "in", "time": 1.0, "title": "echo", "message": m["text"], "target": m["desk"],
                 "from": ""} for m in self.sent()]


def make_inbox(ctx):
    return EchoInbox(ctx)
