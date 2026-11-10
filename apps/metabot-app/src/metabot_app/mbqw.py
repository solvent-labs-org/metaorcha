"""mbqw.py — the client of a warm MetaBot reader (speed, 2026-11-09).

Every MetaBot read used to start a fresh python that imported the whole
runtime (``mbq runs`` 9.7 s, ``mbq live`` 1.8 s on the PC), on every poll.
``mbq.py serve`` keeps one process warm and answers one JSON line per request;
this client sends one request at a time (a lock), waits at most ``timeout``
seconds, and on a timeout, a dead process or a garbled reply kills the
process group and starts a fresh worker on the next call. A worker older than
``max_age`` seconds is replaced, so an upgraded MetaBot is picked up.
Never raises: a failure is ``{"error": "mbq: ..."}``, the shape mbq.py uses."""
import json
import os
import selectors
import signal
import subprocess
import threading
import time


class Worker:
    def __init__(self, argv, env=None, cwd=None, timeout=20.0, max_age=900.0):
        self.argv, self.env, self.cwd = list(argv), env, cwd
        self.timeout, self.max_age = timeout, max_age
        self._p = None
        self._born = 0.0
        self._n = 0
        self._buf = b""
        self._lock = threading.Lock()

    @property
    def pid(self):
        return self._p.pid if self._p is not None and self._p.poll() is None else None

    def _start(self):
        self._p = subprocess.Popen(self.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, env=self.env, cwd=self.cwd,
                                   start_new_session=True)
        os.set_blocking(self._p.stdout.fileno(), False)
        self._born, self._buf = time.monotonic(), b""

    def _kill(self):
        p, self._p = self._p, None
        if p is None:
            return
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            p.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        for f in (p.stdin, p.stdout):
            try:
                f.close()
            except OSError:
                pass

    def _readline(self, deadline):
        sel = selectors.DefaultSelector()
        sel.register(self._p.stdout, selectors.EVENT_READ)
        try:
            while b"\n" not in self._buf:
                left = deadline - time.monotonic()
                if left <= 0 or not sel.select(left):
                    return None
                chunk = self._p.stdout.read(65536)
                if not chunk:               # EOF: the worker died
                    return b""
                self._buf += chunk
        finally:
            sel.close()
        line, _, self._buf = self._buf.partition(b"\n")
        return line

    def call(self, args, timeout=None):
        """One request; a worker that died between calls gets one retry on a fresh process."""
        out = self._call(args, timeout)
        if out == {"error": "mbq: worker gone"}:
            out = self._call(args, timeout)
        return out

    def _call(self, args, timeout=None):
        with self._lock:
            if self._p is not None and (self._p.poll() is not None
                                        or time.monotonic() - self._born > self.max_age):
                self._kill()
            if self._p is None:
                try:
                    self._start()
                except OSError as exc:
                    return {"error": f"mbq: cannot start ({type(exc).__name__})"}
            self._n += 1
            rid = self._n
            try:
                self._p.stdin.write((json.dumps({"id": rid, "args": list(args)}) + "\n").encode())
                self._p.stdin.flush()
            except OSError:
                self._kill()
                return {"error": "mbq: worker gone"}
            line = self._readline(time.monotonic() + (timeout or self.timeout))
            if line is None:
                self._kill()
                return {"error": "mbq: timeout"}
            if line == b"":
                self._kill()
                return {"error": "mbq: worker gone"}
            try:
                msg = json.loads(line)
            except ValueError:
                self._kill()
                return {"error": "mbq: bad reply"}
            if not isinstance(msg, dict) or msg.get("id") != rid:
                self._kill()
                return {"error": "mbq: bad reply"}
            if "error" in msg:
                return {"error": str(msg["error"])[:300]}
            return msg.get("result")

    def close(self):
        with self._lock:
            self._kill()
