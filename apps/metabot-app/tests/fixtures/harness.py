"""Test harness: an in-process MetaChat server on 127.0.0.1:0 with a temp HOME,
temp state dir, temp config, scratch METABOT_HOME and the fake binaries on
PATH. Nothing here touches the real state dir, the real MetaBot home, real
tmux or the network."""
import http.client
import json
import re
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "src" / "metabot_app"   # the package directory
FIX = Path(__file__).resolve().parent
ECHO_INBOX = FIX / "adapters" / "echo_inbox.py"   # the fixture inbox adapter (tests/test_inbox.py)
sys.path.insert(0, str(ROOT.parent))

from metabot_app import server  # noqa: E402  (after sys.path)

_ENV_KEYS = ("HOME", "METACHAT_CONFIG", "METACHAT_STATE", "METABOT_HOME", "PATH", "FAKE_LOG",
             "FAKE_TMUX_STATE", "FAKE_PANE_PID", "FAKE_SLEEP", "FAKE_EXIT", "FAKE_MBQ_FAIL",
             "FAKE_ACTIVE_UNITS", "FAKE_ORPHAN_PID_FILE",
             "ANTHROPIC_API_KEY", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_COWORK_PLUGINS")


def CFG_INBOX(env):
    return json.loads(env.config.read_text()).get("inbox_module", "")


class Env:
    """One temp world. Use as a context manager or call start()/stop()."""

    def __init__(self, config=None, phone=False, serve=True):
        self.config_extra = config or {}
        self.phone = phone          # C2: also start the TLS phone listener on 127.0.0.1:0
        self.serve = serve          # A6: False = the world only; a test starts its own server

    def start(self):
        self._saved = {k: os.environ.get(k) for k in _ENV_KEYS}
        self.root = Path(tempfile.mkdtemp(prefix="metachat-test-"))
        self.home = self.root / "home"
        self.state = self.root / "state"
        self.mbhome = self.root / "mbhome"
        self.bin = self.root / "bin"
        self.projects = self.root / "projects"
        for d in (self.home, self.state, self.mbhome, self.bin, self.projects / "alpha",
                  self.projects / "beta", self.home / ".claude"):
            d.mkdir(parents=True, exist_ok=True)
        os.chmod(self.state, 0o700)
        os.symlink(FIX / "fake-tmux", self.bin / "tmux")
        os.symlink(FIX / "fake-systemctl", self.bin / "systemctl")   # is-active answers, never acts
        self.log = self.root / "fake.log"
        self.tmux_state = self.root / "tmux.json"
        self.tmux_state.write_text(json.dumps({"sessions": {}}))
        cfg = {
            "port": 0, "bind": ["127.0.0.1"], "state_dir": str(self.state),
            "metabot_bin": str(FIX / "fake-metabot"), "orcha_bin": str(FIX / "fake-orcha"),
            "metabot_python": str(FIX / "fake-mbpython"), "metabot_home": str(self.mbhome),
            "default_desk": "desk-a", "claude_name_prefixes": ["claude", "desk-"],
            "projects": [{"name": "alpha", "paths": [str(self.projects / "alpha")]}],
            "project_roots": [str(self.projects)],
            "max_desks": 24, "max_runs": 6, "spawn_per_minute": 10,
        }
        cfg.update(self.config_extra)
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps(cfg))
        for k in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_COWORK_PLUGINS",
                  "FAKE_SLEEP", "FAKE_EXIT", "FAKE_MBQ_FAIL", "FAKE_PANE_PID", "FAKE_ACTIVE_UNITS",
                  "FAKE_ORPHAN_PID_FILE"):
            os.environ.pop(k, None)
        os.environ.update({"HOME": str(self.home), "METACHAT_CONFIG": str(self.config),
                           "METACHAT_STATE": str(self.state), "METABOT_HOME": str(self.mbhome),
                           "PATH": f"{self.bin}:{self._saved['PATH']}", "FAKE_LOG": str(self.log),
                           "FAKE_TMUX_STATE": str(self.tmux_state)})
        server.configure()
        server.Handler._fail.clear()
        server.invalidate()
        self.token = server.TOKEN
        # ops/F7: the VNC backend is a port of ours that nothing listens on, so
        # /api/meta and /ws/vnc never reach a real x11vnc on 127.0.0.1:5900
        self._saved_vnc = server.VNC_BACKEND
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        closed_port = probe.getsockname()[1]
        probe.close()
        server.VNC_BACKEND = ("127.0.0.1", closed_port)
        self.srv = self.tls = None
        if self.serve:
            self.srv = server.Server(("127.0.0.1", 0), server.Handler)
            self.port = self.srv.server_address[1]
            self.host = f"127.0.0.1:{self.port}"
            self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
            self.thread.start()
        if self.phone:
            cert = server.phone_cert()
            assert cert is not None, "openssl is needed for the TLS tests"
            self.fingerprint = cert.fingerprint
            self.tls = server.TLSServer(("127.0.0.1", 0), server.Handler, cert)
            self.tls_port = self.tls.server_address[1]
            self.tls_host = f"127.0.0.1:{self.tls_port}"
            self.tls_thread = threading.Thread(target=self.tls.serve_forever, daemon=True)
            self.tls_thread.start()
        return self

    def stop(self):
        if self.tls is not None:
            self.tls.shutdown()
            self.tls.server_close()
        if self.srv is not None:
            self.srv.shutdown()
            self.srv.server_close()
        server.INBOX.close()   # an adapter's readers stop with the world they read
        server.mb.close_reader()
        server.VNC_BACKEND = self._saved_vnc
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.root, ignore_errors=True)

    __enter__ = start

    def __exit__(self, *a):
        self.stop()

    # ---- the fixture inbox adapter
    def arm(self, *names):
        """Arm desks through the fixture inbox adapter (loaded first when the
        config has none): the seam's armed() then names them."""
        if not str(CFG_INBOX(self)).endswith("echo_inbox.py"):
            cfg = json.loads(self.config.read_text())
            cfg["inbox_module"] = str(ECHO_INBOX)
            self.config.write_text(json.dumps(cfg))
            server.configure()
        server.INBOX.inner.set_armed(names)
        server.invalidate()

    # ---- fakes
    def calls(self, binary=None):
        out = []
        try:
            for line in self.log.read_text().splitlines():
                rec = json.loads(line)
                if binary is None or rec["bin"] == binary:
                    out.append(rec["argv"])
        except OSError:
            pass
        return out

    def clear_calls(self):
        self.log.write_text("")

    def tmux_sessions(self):
        return json.loads(self.tmux_state.read_text())["sessions"]

    def add_session(self, name, pid=4000000, cwd="/", pane=""):
        st = json.loads(self.tmux_state.read_text())
        st["sessions"][name] = {"pid": pid, "cwd": cwd, "pane": pane}
        self.tmux_state.write_text(json.dumps(st))

    def set_pane(self, name, text):
        st = json.loads(self.tmux_state.read_text())
        st["sessions"][name]["pane"] = text
        self.tmux_state.write_text(json.dumps(st))

    # ---- git
    def _git_env(self):
        """The environment ``git`` runs in: the HOME the tests started with, so the
        machine's own git config applies. No identity variable is set, and no
        repo-location variable leaks in from the tree that is running the tests."""
        env = {k: v for k, v in os.environ.items()
               if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
                            "GIT_OBJECT_DIRECTORY")}
        if self._saved.get("HOME"):
            env["HOME"] = self._saved["HOME"]
        # A test can point git at an empty global file so a machine that has an
        # identity still exercises the no-identity path. The process HOME stays.
        blank = getattr(self, "_git_config_global", None)
        if blank:
            env["GIT_CONFIG_GLOBAL"] = blank
            env["GIT_CONFIG_NOSYSTEM"] = "1"
            env.pop("GIT_CONFIG_COUNT", None)
            for key in [k for k in env if k.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))]:
                env.pop(key, None)
        return env

    def git(self, *args, cwd):
        """Run git in ``cwd``. No user.* flag is passed."""
        return subprocess.run(["git", *args], cwd=str(cwd), env=self._git_env(), check=True,
                              capture_output=True, text=True, timeout=60).stdout.strip()

    def _machine_identity(self):
        """Whether the machine's global git config can author a commit. A runner
        and a new machine have neither a name nor an email."""
        for key in ("user.name", "user.email"):
            got = subprocess.run(["git", "config", "--global", "--get", key], env=self._git_env(),
                                 capture_output=True, text=True)
            if got.returncode != 0 or not got.stdout.strip():
                return False
        return True

    def git_checkout(self):
        """A git checkout of its own under this Env's root, for a test that reads a
        build id (a `git archive` export of this tree has no .git): `git init`, one
        empty commit, a branch worktree. -> (repo, worktree, sha).

        The commit uses the machine's identity when it has one. When it does not,
        the fixture repository holds its own, and the machine's config is not written."""
        repo, wt = self.root / "git-repo", self.root / "git-worktree"
        repo.mkdir()
        self.git("init", "-q", cwd=repo)
        if not self._machine_identity():
            self.git("config", "user.name", "Build Fixture", cwd=repo)
            self.git("config", "user.email", "fixture@example.com", cwd=repo)
        self.git("commit", "-q", "--allow-empty", "--no-verify", "--no-gpg-sign", "-m", "build id fixture",
                 cwd=repo)
        self.git("worktree", "add", "-q", "-b", "fixture-wt", str(wt), cwd=repo)
        return repo, wt, self.git("rev-parse", "HEAD", cwd=repo)

    def tmux_loaded(self):
        """[(buffer, bytes)] of each ``load-buffer -b NAME -``, in order: the
        bytes tmux read on stdin."""
        st = json.loads(self.tmux_state.read_text())
        return [(r["buffer"], bytes.fromhex(r["hex"])) for r in st.get("loaded", [])]

    def tmux_pasted(self):
        """[(buffer, target, bracketed, bytes)] of each ``paste-buffer``, in order."""
        st = json.loads(self.tmux_state.read_text())
        return [(r["buffer"], r["target"], r["bracketed"], bytes.fromhex(r["hex"]))
                for r in st.get("pasted", [])]

    def set_tmux_global_env(self, env):
        """What the fake tmux answers to `show-environment -g` (the server's
        global environment, which every new session inherits)."""
        st = json.loads(self.tmux_state.read_text())
        st["global_env"] = dict(env)
        self.tmux_state.write_text(json.dumps(st))

    # ---- http
    def request(self, method, path, body=None, headers=None, token=True, raw=None, host=None):
        if token and not re.search(r"[?&]t=", path):
            path += ("&" if "?" in path else "?") + "t=" + self.token
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Host": host or self.host}
        if method == "POST":
            h.update({"Content-Type": "application/json", "X-MetaChat": "1"})
        h.update(headers or {})
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        for k, v in h.items():
            if v is not None:
                c.putheader(k, v)
        if data is not None and "Content-Length" not in h:
            c.putheader("Content-Length", str(len(data)))
        c.endheaders()
        if data is not None:
            c.send(data)
        try:
            r = c.getresponse()
            payload = r.read()
            r.close()
        finally:
            c.close()
        try:
            parsed = json.loads(payload)
        except ValueError:
            parsed = None
        return r.status, parsed, payload, r.getheaders()

    def get(self, path, **kw):
        s, j, _, _ = self.request("GET", path, **kw)
        return s, j

    def post(self, path, body=None, **kw):
        s, j, _, _ = self.request("POST", path, body=body, **kw)
        return s, j

    # ---- the TLS phone listener (C2)
    def pair_device(self, name="Test phone"):
        """A paired phone without the pairing dance: (token, row)."""
        return server.DEVICES.add(name, "phone")

    @staticmethod
    def tls_ctx():
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE      # the tests pin by fingerprint, as the phone does
        return ctx

    def tls_socket(self, timeout=10):
        raw = socket.create_connection(("127.0.0.1", self.tls_port), timeout=timeout)
        return self.tls_ctx().wrap_socket(raw, server_hostname="metabot")

    def tls_request(self, method, path, body=None, headers=None, token=None, raw=None, host=None):
        """Like request(), over TLS. ``token`` is the phone token to append (None = none)."""
        if token and not re.search(r"[?&]t=", path):
            path += ("&" if "?" in path else "?") + "t=" + token
        c = http.client.HTTPSConnection("127.0.0.1", self.tls_port, timeout=10, context=self.tls_ctx())
        h = {"Host": host or self.tls_host}
        if method == "POST":
            h.update({"Content-Type": "application/json", "X-MetaChat": "1"})
        h.update(headers or {})
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        for k, v in h.items():
            if v is not None:
                c.putheader(k, v)
        if data is not None and "Content-Length" not in h:
            c.putheader("Content-Length", str(len(data)))
        c.endheaders()
        if data is not None:
            c.send(data)
        try:
            r = c.getresponse()
            payload = r.read()
            r.close()
        finally:
            c.close()
        try:
            parsed = json.loads(payload)
        except ValueError:
            parsed = None
        return r.status, parsed, payload, r.getheaders()

    def tls_ws(self, path, token, origin=None, timeout=10):
        """Open a WS over the TLS listener: (socket, status, rest)."""
        if token and not re.search(r"[?&]t=", path):
            path += ("&" if "?" in path else "?") + "t=" + token
        s = self.tls_socket(timeout)
        hdr = (f"GET {path} HTTP/1.1\r\nHost: {self.tls_host}\r\nUpgrade: websocket\r\n"
               f"Connection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
               f"Sec-WebSocket-Version: 13\r\n")
        if origin:
            hdr += f"Origin: {origin}\r\n"
        s.sendall((hdr + "\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = s.recv(4096)
            if not chunk:
                break
            head += chunk
        status = int(head.split(b" ", 2)[1]) if head.startswith(b"HTTP/") else 0
        rest = head.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in head else b""
        return s, status, rest

    # ---- websocket (raw)
    def ws(self, path, origin=None, timeout=10):
        """Open a WS: returns the socket after a 101, or raises with the status."""
        if not re.search(r"[?&]t=", path):
            path += ("&" if "?" in path else "?") + "t=" + self.token
        s = socket.create_connection(("127.0.0.1", self.port), timeout=timeout)
        hdr = (f"GET {path} HTTP/1.1\r\nHost: {self.host}\r\nUpgrade: websocket\r\n"
               f"Connection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
               f"Sec-WebSocket-Version: 13\r\n")
        if origin:
            hdr += f"Origin: {origin}\r\n"
        s.sendall((hdr + "\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = s.recv(4096)
            if not chunk:
                break
            head += chunk
        status = int(head.split(b" ", 2)[1]) if head.startswith(b"HTTP/") else 0
        rest = head.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in head else b""
        return s, status, rest


def ws_read_frame(sock, pre=b"", timeout=10):
    """Read one server frame: (opcode, payload)."""
    import struct
    sock.settimeout(timeout)
    buf = pre
    while True:
        if len(buf) >= 2:
            op, ln = buf[0] & 0x0F, buf[1] & 0x7F
            idx = 2
            if ln == 126 and len(buf) >= 4:
                ln, idx = struct.unpack(">H", buf[2:4])[0], 4
            elif ln == 127 and len(buf) >= 10:
                ln, idx = struct.unpack(">Q", buf[2:10])[0], 10
            if len(buf) >= idx + ln:
                return op, buf[idx:idx + ln], buf[idx + ln:]
        chunk = sock.recv(65536)
        if not chunk:
            raise ConnectionError("closed")
        buf += chunk


def ws_read_close(sock, pre=b"", timeout=10):
    """Skip data/ping frames until a close frame; returns (code, reason)."""
    import struct
    buf = pre
    while True:
        op, payload, buf = ws_read_frame(sock, buf, timeout)
        if op == 0x8:
            return struct.unpack(">H", payload[:2])[0], payload[2:].decode("utf-8", "replace")
