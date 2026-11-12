#!/usr/bin/env python3
"""MetaBot app server: chat with the coding agents on this PC, a real terminal
per desk over a WebSocket PTY, and the MetaBot side (runs, receipts, accounts,
the command catalogue) behind one token.

Serves a single-page UI and a small JSON/SSE/WS API. Chat reaches a desk by
typing into the CLI its terminal runs; an optional inbox adapter (``inbox.py``,
config ``inbox_module``) adds a chat service of the user's own. The MetaBot
routes run the configured ``metabot`` / ``orcha`` binaries and read MetaBot's
home through ``mbq.py``.

Config: ``METACHAT_CONFIG`` (env) else ``~/.config/metachat/config.json``;
every key has a default and loading never exits (contract section 2).
State: ``METACHAT_STATE`` (env) else the config's ``state_dir``.

Endpoints (contract section 4):
  GET  /                   the UI            GET  /vendor/<file>   vendored assets
  GET  /api/sessions       desk names        GET  /api/desks[?scope=metachat]
  GET  /api/history        chat history      GET  /api/stream      SSE of chat events
  GET  /api/meta           versions, limits  GET  /api/projects    desks and runs by project
  GET  /api/accounts       logins + folders  GET  /api/catalog     the command catalogue
  GET  /api/modules        module manifests  GET  /api/events      the history log
  GET  /api/mb/runs        live + done runs  GET  /api/mb/run/<id> one run's view
  GET  /api/mb/receipt/<id> receipt bytes    GET  /ws/term?desk=   PTY on fc-<desk>
  GET  /ws/vnc             noVNC bridge      GET  /api/aliases     desk display names
  GET  /api/tree           projects -> folders -> desks and runs; waiting asks; ended names
  GET  /tests/<file>       the parity self-test page and its cases (two files only)
  POST /api/send /api/pane /api/spawn /api/run /api/kill /api/accounts /api/alias
       /api/cli/read /api/cli/desk /api/modules/reload /api/mb/verify /api/sdk/verify
Every POST carries Content-Type: application/json and X-MetaChat: 1 (section 3).

Section 13 (the evening hotfixes, kept): ``deliver()`` routes one message by
the inbox adapter, the pane or the lane; pane and desk -> desk deliveries are local events
(``state/local-events.jsonl``) merged into /api/history and pushed to every
/api/stream subscriber with ``typed`` and ``via:"desk"``; ``bin/mc-send`` posts
``{text, target, from}``; aliases live in ``state/aliases.json``.
"""
import base64, errno, fcntl, hashlib, hmac, json, os, pty, queue, re, secrets, select, ssl
import shlex, shutil, signal, uuid, socket, struct, subprocess, sys, tempfile, termios, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from . import catalog, doctor, folders, instance, netaddrs, pairing, paths, procs, tlscert, tmuxcmd
from . import devices as devices_mod
from . import history as history_store
from . import inbox as inbox_mod
from . import mb

HERE = Path(__file__).resolve().parent
VENDOR = HERE / "vendor"
MODULES_DIR = HERE / "modules"
TESTS_DIR = HERE / "selftest"
# ops/F6: the tablet runs the parity self-test from the app's own origin; only
# these two files under tests/ are ever served, token-gated like everything else
TESTS_SERVED = frozenset({"verify-selftest.html", "fixtures/parity/cases.json"})
MIME = {".js": "application/javascript; charset=utf-8",
        ".mjs": "application/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".html": "text/html; charset=utf-8",
        ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon",
        ".gif": "image/gif", ".json": "application/json", ".mp3": "audio/mpeg",
        ".woff": "font/woff", ".woff2": "font/woff2", ".ttf": "font/ttf",
        ".md": "text/plain; charset=utf-8", ".txt": "text/plain; charset=utf-8"}
VNC_BACKEND = ("127.0.0.1", 5900)   # x11vnc on the virtual display (fc-gui)
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
APP, VERSION = "MetaBot", "0.1.0"     # section 23: the app is MetaBot; machine keys unchanged
BUILD = "dev"                        # 21.4: the short SHA of HERE, set by configure()
BODY_LIMIT = 65536
LOG_LIMIT = 2 * 1024 * 1024          # 21.4: server.log / ws.log rotate here, one .1 copy
LOCAL_KEEP = 300                     # local-events.jsonl is rewritten to its last 300 lines
SNAP_TTL = 1.0                       # 21.4: desks() reads tmux at most once a second
SSE_PING_S = 15                      # 21.4: ": ping" cadence on /api/stream
TERM_MAX_PER_DESK = 4                # 21.4: the 5th /ws/term on a desk closes 4008
TERM_IDLE_S = 30 * 60                # 21.4: a terminal with no input for 30 min closes 4000
TERM_PING_S = 30                     # ws ping cadence; two unanswered -> 1001

DEFAULTS = {
    "port": 8460,
    "bind": ["127.0.0.1"],
    "allowed_hosts": [],
    "state_dir": "~/.local/state/metachat",
    "inbox_module": "",                  # B1: an inbox adapter file (absolute path); "" = none
    "default_desk": "",
    "shared_desk": "main",               # B3: the tmux session /ws/term opens when no desk is named
    "tmux_socket": "metabot",            # A5: the app's own tmux server (-L); "" = the user's default server
    "claude_name_prefixes": ["claude"],
    "claude_homes": [".claude"],         # B2: Claude config folders under HOME that Resume and the mirror read
    "metabot_bin": "",                   # A2: "" = beside the app's interpreter, then PATH (paths.find_bin)
    "orcha_bin": "",
    "metabot_python": "",                # "" = the app's own interpreter
    "metabot_home": "",
    "projects": [],
    "project_roots": [],
    "default_folder": "",                # 25.3 B4: where claude/cursor desks open by default
    "max_desks": 24,
    "max_runs": 6,
    "spawn_per_minute": 10,
    "phone_listener": True,              # C2: the TLS listener phones pair with (needs openssl once)
    "phone_port": 8461,
    "phone_bind": ["0.0.0.0"],
    "phone_addresses": [],               # C6: addresses a pairing code advertises; [] = the host's own
}


def phone_cert():
    """The host's pinned certificate (C1), or None when openssl is missing: then the
    phone listener stays off and the setup page says so. Tried once per configure()."""
    if not _cert["tried"]:
        _cert["tried"] = True
        try:
            _cert["cert"] = tlscert.ensure(STATE)
        except tlscert.Unavailable as exc:
            slog(f"phone listener off: {exc}")
            _cert["cert"] = None
    return _cert["cert"]


def on_device():
    """The server runs inside the phone app (stream E): the runtime says so, or Python does."""
    return mb.on_device()


def invalidate_caps():
    _caps["t"], _caps["data"] = 0.0, None


def caps():
    """B8: what this host can do. The page branches on these, never on the OS.
    desks: tmux 3.2+ found. sandbox: bwrap found. display: the VNC backend answers.
    phone_listener: pairing is possible here (off on the phone itself). engines: each
    CLI engine whose binary is found ("cli") and the HTTP engines ("http"). service:
    how "keep running" is done. camera: never on a server. on_device: see on_device()."""
    now = time.time()
    if _caps["data"] is not None and now - _caps["t"] < CAPS_TTL:
        return _caps["data"]
    phone = on_device()
    engines = {name: "cli" for name, binary in CLI_ENGINES.items() if paths.find_bin(binary)}
    engines["openai"] = "http"
    tmux_ok = bool(tmuxcmd.found()) and tmuxcmd.new_enough(tmuxcmd.version())
    if phone:
        service = "android"
    elif sys.platform == "darwin":
        service = "launchd"
    elif paths.find_bin("systemctl"):
        service = "systemd"
    else:
        service = "none"
    data = {"desks": tmux_ok and not phone, "sandbox": bool(paths.find_bin("bwrap")) and not phone,
            "display": mb.vnc_up(VNC_BACKEND), "phone_listener": (not phone) and phone_info()["on"],
            "engines": engines, "camera": False, "service": service, "on_device": phone}
    _caps["t"], _caps["data"] = now, data
    return data


def live_connections(dev_id):
    """How many open connections a paired device has (C5)."""
    with _live_lock:
        return len(_live.get(dev_id, ()))


def _track(dev_id, sock):
    with _live_lock:
        _live.setdefault(dev_id, set()).add(sock)


def _untrack(sock):
    with _live_lock:
        for dev_id, socks in list(_live.items()):
            socks.discard(sock)
            if not socks:
                _live.pop(dev_id, None)


def cut_device(dev_id):
    """Close every open connection of a revoked device: its event stream, terminals
    and display relay end at once; the next request is refused by the token check."""
    with _live_lock:
        socks = list(_live.pop(dev_id, ()))
    for sock in socks:          # a bare shutdown: a close frame could interleave with a terminal's frames
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
    return len(socks)


def phone_port():
    return _phone_ports[0] if _phone_ports else int(CFG["phone_port"])


def phone_info():
    """The Phone block of /api/meta (C4): is the listener on, where, with which pin."""
    cert = phone_cert() if CFG["phone_listener"] else None
    return {"on": cert is not None, "port": phone_port(),
            "fingerprint": cert.fingerprint if cert else None,
            "devices": DEVICES.count("phone"), "pairing": PAIR.status()["live"]}


def pair_start():
    """POST /api/pair/start (desktop only) -> (status, reply): a fresh code and
    everything the QR and the typed path need."""
    cert = phone_cert() if CFG["phone_listener"] else None
    if cert is None:
        return 503, {"ok": False, "error": "phone listener off"}
    live = PAIR.start()
    addresses = netaddrs.candidates(CFG["phone_addresses"])
    port = phone_port()
    return 200, {"ok": True, "code": live["code"], "expires_in": live["expires_in"], "port": port,
                 "addresses": addresses, "fingerprint": cert.fingerprint,
                 "words": pairing.words(cert.fingerprint),
                 "payload": pairing.payload(addresses, port, live["code"], cert.fingerprint)}


def config_path():
    return Path(os.environ.get("METACHAT_CONFIG") or "~/.config/metachat/config.json").expanduser()


def raw_config(path=None, quiet=False):
    """The config file as written (a dict), or {} when it is missing or malformed
    (one line logged unless ``quiet``). An inbox adapter keeps its own keys here."""
    path = Path(path) if path else config_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        if not quiet:
            print(f"metachat: config {path} ignored: {type(exc).__name__}", file=sys.stderr)
        return {}
    if not isinstance(raw, dict):
        if not quiet:
            print(f"metachat: config {path} ignored: not an object", file=sys.stderr)
        return {}
    return raw


def load_config(path=None):
    """Defaults, overlaid with the file's keys of the right type. A missing file
    is defaults; a malformed one logs one line and is defaults. Never exits."""
    cfg = json.loads(json.dumps(DEFAULTS))
    raw = raw_config(path)
    for k, v in raw.items():
        if k in cfg and type(v) is type(cfg[k]):
            cfg[k] = v
    return cfg


# ---- globals the routes read; configure() sets them (tests call it again)
CFG = dict(DEFAULTS)
STATE = TOKEN_FILE = NAMES_DIR = WSLOG = None
DEVICES = None                       # C3: paired devices (phone-scope tokens)
PAIR = None                          # C4: the live pairing code
_live = {}                           # C5: device id -> {socket}: the open connections of each paired phone
_caps = {"t": 0.0, "data": None}     # B8: the host's capabilities, read at most every CAPS_TTL seconds
CAPS_TTL = 30.0
CLI_ENGINES = {"claude": "claude", "codex": "codex", "cursor": "cursor-agent"}   # engine -> binary
_live_lock = threading.Lock()
_cert = {"cert": None, "tried": False}   # C2: the host's pinned certificate, made once per state folder
_phone_ports = []                    # C2: the bound port of each TLS listener (tests bind port 0)
INBOX = inbox_mod.NullInbox()        # B1: the chat inbox adapter, NullInbox unless configured
INBOX_CTX = None
TOKEN = ""
DEFAULT_DESK = ""
SHARED_DESK = "main"
CLAUDE_PREFIXES = ("claude",)
CLAUDE_HOMES = (".claude",)
HIST = None
MB = None
MODULES = {"modules": [], "errors": []}


def claude_home_ok(h):
    """A ``claude_homes`` entry: a folder name under HOME (no absolute path, no ``..``)."""
    return (isinstance(h, str) and h and not h.startswith(("/", "~"))
            and ".." not in Path(h).parts and "\x00" not in h)


def names_dirs():
    """Every session-name folder: the app's own (STATE/names), then the inbox adapter's."""
    out = [NAMES_DIR]
    for d in INBOX.names_dirs():
        if d not in out:
            out.append(d)
    return out


def configure(path=None):
    """Load config and state. Safe to call again (tests re-point it)."""
    global CFG, STATE, TOKEN_FILE, NAMES_DIR, WSLOG, INBOX, INBOX_CTX, TOKEN, DEFAULT_DESK, DEVICES, PAIR
    global CLAUDE_PREFIXES, CLAUDE_HOMES, HIST, MB, MODULES, LOCAL_LOG, ALIASES, SHARED_DESK
    CFG = load_config(path)
    STATE = Path(os.environ.get("METACHAT_STATE") or CFG["state_dir"]).expanduser()
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    TOKEN_FILE = STATE / "token"
    WSLOG = STATE / "ws.log"
    LOCAL_LOG = STATE / "local-events.jsonl"
    ALIASES = STATE / "aliases.json"
    _load_local()
    NAMES_DIR = STATE / "names"   # B2: the app's own session names (names/<id or desk> = desk name)
    DEFAULT_DESK = CFG["default_desk"]
    tmuxcmd.configure(CFG["tmux_socket"])
    sd = CFG["shared_desk"]
    SHARED_DESK = sd if re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", sd) and not sd.startswith("fc-") else "main"
    CLAUDE_PREFIXES = tuple(p for p in CFG["claude_name_prefixes"] if isinstance(p, str) and p)
    CLAUDE_HOMES = tuple(h for h in CFG["claude_homes"] if claude_home_ok(h)) or (".claude",)
    if not TOKEN_FILE.exists():
        TOKEN_FILE.write_text(secrets.token_urlsafe(18) + "\n")
        TOKEN_FILE.chmod(0o600)
    TOKEN = TOKEN_FILE.read_text().strip()
    DEVICES = devices_mod.Devices(STATE / "devices.json")
    PAIR = pairing.Pairing(DEVICES)
    invalidate_caps()
    _cert["cert"], _cert["tried"] = None, False
    _phone_ports.clear()
    HIST = history_store.Store(STATE / "history.sqlite")
    MB = mb.Ctx(CFG, STATE, HIST)
    _sess_cache[0] = 0.0
    with _SNAP_LOCK:
        _snap["t"], _snap["data"], _snap["tree"] = 0.0, None, None
    user_modules_dir()   # 25.3 B8: there before a desk opens in it
    old = INBOX
    INBOX_CTX = inbox_mod.InboxCtx(
        cfg=CFG, config=raw_config(path, quiet=True), state=STATE, home=Path.home(),
        emit=_fanout, sessions=lambda: sessions(), paste=lambda desk, text: paste_pane(desk, text),
        desk_slug=desk_slug, log=slog, default_desk=DEFAULT_DESK)
    INBOX = inbox_mod.load(CFG["inbox_module"], INBOX_CTX, slog)
    if old is not INBOX:
        old.close()
    MODULES = load_modules()
    global BUILD
    BUILD = mb.build_id(HERE)
    return CFG


def _rotate(path, limit=LOG_LIMIT, keep_lines=None):
    """21.4: when ``path`` is over ``limit`` bytes move it to ``path.1`` (one copy);
    with ``keep_lines`` the file is rewritten to its last N lines instead, so a
    reader that loads it whole stays O(N). Never raises."""
    try:
        if os.stat(path).st_size <= limit:
            return False
        if keep_lines:
            tail = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()[-keep_lines:]
            tmp = str(path) + ".tmp"
            Path(tmp).write_text("\n".join(tail) + "\n", encoding="utf-8")
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        else:
            os.replace(path, str(path) + ".1")
        return True
    except OSError:
        return False


def slog(msg):
    """One server.log line (rotated at LOG_LIMIT) and the same on stderr."""
    print(f"metachat: {msg}", file=sys.stderr)
    if STATE is None:
        return
    try:
        path = STATE / "server.log"
        _rotate(path)
        with open(path, "a") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


def user_modules_dir():
    """<state>/modules: the user modules folder (4.12), 0700."""
    d = STATE / "modules"
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    return d


def load_modules():
    return catalog.load_modules(MODULES_DIR, STATE / "modules")


# C0 and C1 controls, the bidi embeddings, overrides and isolates (they reorder
# the text around a label), and a lone surrogate (it cannot be written as UTF-8)
LABEL_CTRL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069\ud800-\udfff]")


def new_module(body):
    """25.3 B8: POST /api/modules/new {id, label} -> (status, reply). Writes
    <state>/modules/<id>.json (0600, never over a file or a link that is
    there) from catalog.starter_module, then reloads. A built-in module's id is
    refused: a user module with that id would replace the built-in."""
    global MODULES
    mid, label = body.get("id"), body.get("label")
    if not isinstance(mid, str) or not catalog.SLUG_RE.fullmatch(mid):
        return 400, {"ok": False, "error": "bad id"}
    if (not isinstance(label, str) or not 1 <= len(label) <= 32 or not label.strip()
            or LABEL_CTRL_RE.search(label)):
        return 400, {"ok": False, "error": "bad label"}
    if mid in catalog.builtin_ids(MODULES_DIR):
        return 409, {"ok": False, "error": "a built-in module has that id"}
    path = user_modules_dir() / f"{mid}.json"
    taken = {c["slash"] for m in MODULES["modules"] for c in m.get("commands", [])}
    text = json.dumps(catalog.starter_module(mid, label, taken), indent=2, ensure_ascii=False) + "\n"
    try:   # the bytes first: a label that cannot be written never makes the file
        data = text.encode("utf-8")
    except UnicodeEncodeError:
        return 400, {"ok": False, "error": "bad label"}
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return 409, {"ok": False, "error": "module exists"}
    except OSError as exc:
        return 502, {"ok": False, "error": f"cannot write the module: {type(exc).__name__}"}
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
    except OSError as exc:   # a half-written file would keep the id: it goes
        try:
            path.unlink()
        except OSError:
            pass
        return 502, {"ok": False, "error": f"cannot write the module: {type(exc).__name__}"}
    MODULES = load_modules()
    # module_report's rule: the new file is ok when it loads and is the file its id came from
    row = next((u for u in catalog.module_report(MODULES_DIR, STATE / "modules")["user"]
                if u["file"] == str(path)), None)
    err = row["error"] if row is not None else "not loaded"
    HIST.append("modules_reload", data={"new": mid, "modules": len(MODULES["modules"]),
                                        "errors": len(MODULES["errors"])})
    return 200, {"ok": True, "file": str(path), "error": err}


def extend(fresh=False):
    """25.3 B6: GET /api/extend. The MetaBot half (plugins, marketplaces,
    engines, errors) is mb.extend_info, cached 10 s unless ``fresh``; the
    modules (as the files are now) and serve are read on every request."""
    out = mb.extend_info(MB, fresh)
    out["modules"] = catalog.module_report(MODULES_DIR, STATE / "modules")
    running, desk = mb.serve_state(MB)
    out["serve"] = {"running": running, "desk": desk}
    return out


def history():
    """The inbox adapter's recent events and the local ones, last 24h, oldest first."""
    out = INBOX.history()
    with _local_lock:
        out.extend(dict(e) for e in _local if e.get("time", 0) > time.time() - 86400)
    out.sort(key=lambda e: e["time"])
    return out[-200:]


_SESS_TTL = 5.0
_sess_cache = [0.0, []]


def sessions():
    """Named desks, cached: an inbox adapter tags every message with them and
    must not stat the names dir every time."""
    if time.time() - _sess_cache[0] < _SESS_TTL:
        return list(_sess_cache[1])
    names = set()
    for nd in names_dirs():
        try:
            files = list(nd.iterdir()) if nd.is_dir() else []
        except OSError:
            files = []
        for f in files:
            try:
                n = f.read_text().strip()
            except Exception:
                continue
            if n and not n.startswith("session-"):
                names.add(n)
    out = sorted(names, key=lambda n: (n != DEFAULT_DESK, n.lower()))
    _sess_cache[0], _sess_cache[1] = time.time(), out
    return list(out)


# ---- local events (section 13): deliveries made here, not seen by the inbox
LOCAL_LOG = ALIASES = None      # configure() points them into STATE
_local = []                     # events made here (pane deliveries, desk -> desk)
_local_subs = set()             # SSE subscriber queues
_local_lock = threading.Lock()


def _load_local():
    """Re-read the last 300 local events from disk (start, and each configure)."""
    with _local_lock:
        del _local[:]
        try:
            lines = LOCAL_LOG.read_text().splitlines()[-300:]
        except OSError:
            return
        for line in lines:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if isinstance(ev, dict):
                _local.append(ev)


def local_event(message, target="", frm="", via="in", typed=True, **extra):
    ev = {"via": via, "time": time.time(), "title": "metachat",
          "message": message, "target": target, "from": frm, "typed": typed, **extra}
    with _local_lock:
        _local.append(ev)
        del _local[:-LOCAL_KEEP]
        for q in list(_local_subs):
            q.put(dict(ev, _local=True))
        try:
            _rotate(LOCAL_LOG, keep_lines=LOCAL_KEEP)
            with open(LOCAL_LOG, "a") as f:
                f.write(json.dumps(ev) + "\n")
            os.chmod(LOCAL_LOG, 0o600)
        except OSError:
            pass
    return ev


HANDOFF_TPL = ("[HANDOFF from {frm} via metachat] You are taking this work over. "
               "Read it, then carry it forward in this session:\n{text}\n"
               "When you have taken it, say so with: {reply_cmd} \"took handoff: <one line>\"")
# the mailbox the person reads: mail sent there lands in the sender's lane (mail_to_lane_once)
PERSON_BOX = "tablet"
MAIL_BODY_MAX = 4000
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")   # a colour or cursor sequence in a note's body


def reply_cmd(me, to):
    """How the desk ``me`` answers ``to`` (a desk, or PERSON_BOX for the person).
    With an inbox adapter the answer is the app's own send subcommand (11-12:
    the wheel ships it and the desk env carries its METACHAT_URL/METACHAT_STATE;
    bin/mc-send is a repo script — the 11-11 tablet finding named it to a desk
    that had none). Without an adapter (every plain install) the core's mail is
    the channel every install has."""
    if isinstance(INBOX, inbox_mod.NullInbox):
        return f"metabot mail send {to} --from {me} -m"
    return f"metabot-app send {to} -m"


def mail_to_lane_once():
    """One pass: every note in the person's mailbox (``<metabot home>/mail/tablet``,
    Code Desks' desk-mail shape, written by ``metabot mail send tablet``) lands in
    its sender's lane, untyped, and is moved to ``read/`` so it is read once.
    Only regular ``*.md`` files, oldest first; a note that is not mail is left."""
    home = MB.metabot_home if MB is not None else ""
    if not home:
        return
    box = Path(home) / "mail" / PERSON_BOX
    try:
        names = sorted(n for n in os.listdir(box) if n.endswith(".md"))
    except OSError:
        return
    for n in names:
        path = box / n
        if path.is_symlink() or not path.is_file():
            continue
        try:
            raw = path.read_bytes()[:MAIL_BODY_MAX + 4096].decode("utf-8", "replace")
        except OSError:
            continue
        head, _, body = raw.partition("\n\n")
        fields = dict(ln.split(":", 1) for ln in head.splitlines() if ":" in ln)
        frm = re.sub(r"[^A-Za-z0-9_.-]", "", fields.get("from", "").strip())[:40]
        if not frm or fields.get("to", "").strip() != PERSON_BOX:
            continue
        try:
            (box / "read").mkdir(mode=0o700, exist_ok=True)
            os.rename(path, box / "read" / n)
        except OSError:
            continue
        text = CTRL_RE.sub("", ANSI_RE.sub("", body.replace("\r\n", "\n"))).strip()[:MAIL_BODY_MAX]
        if text:
            local_event(text, frm, frm, via="desk", typed=False)
NO_CHAT_KINDS = {"run": "a run desk takes no chat — watch it in Runs",
                 "cli": "a command desk takes no chat — open its terminal"}
SIGNIN_NO_CHAT = "a sign-in desk takes no chat — use its sign-in card"


def no_chat_of(meta):
    """Why a desk's registry entry never takes typed chat, else None: a
    headless run, a catalogue command, a sign-in desk (25.3 B1: a chat line
    typed into a sign-in screen would answer it)."""
    if mb.is_signin(meta):
        return SIGNIN_NO_CHAT
    return NO_CHAT_KINDS.get((meta or {}).get("kind"))


def deliver(target, text, frm="", kind="message"):
    """One message to one desk by whatever reaches it now.
    kind: message | handoff (session -> session: the receiver carries it on).
    An inbox adapter is asked first (its deliver answers None for a desk that
    is not its own). Returns (ok, error, via, reach)."""
    if target == "tablet" and frm:   # a desk answering the person: its own lane
        local_event(text, frm, frm, via="desk", typed=False)
        return True, "", "lane", "tablet"
    frm = frm or "the tablet"
    if kind == "handoff":
        # the reply address is a desk name mc-send can route: "tablet" for the person
        body = HANDOFF_TPL.format(frm=frm, text=text,
                                  reply_cmd=reply_cmd(target, PERSON_BOX if frm == "the tablet" else frm))
    elif frm != "the tablet":   # desk -> desk: say who it is from and how to answer
        body = f"[from {frm} via metachat] {text}\n(reply: {reply_cmd(target, frm)} \"...\")"
    else:
        body = text
    if frm == "the tablet":
        frm = ""
    res = INBOX.deliver(target, body)
    if res is not None:
        if res["ok"] and frm:
            local_event(text, target, frm, via="desk", typed=False)
        return res["ok"], res["error"], "inbox", "inbox"
    if not target:
        return False, "choose a desk", "", "none"
    reach, row = reach_of(target)
    if reach == "pane":
        ok, err = paste_pane(target, body)
        if ok:
            local_event(text, target, frm, via="desk" if frm else "in")
        elif err.startswith(WAITING_ERR):   # nothing typed: a desk's message still lands in the lane
            if frm:
                local_event(text, target, frm, via="desk", typed=False)
                return False, f"{err} (the message is in its lane, not typed)", "lane", reach
            return False, err, "", reach
        return ok, err, "pane", reach
    if frm:   # a desk writing to a desk nobody runs: it still lands in that lane
        local_event(text, target, frm, via="desk", typed=False)
        return True, "", "lane", reach
    no_chat = no_chat_of((row or {}).get("meta"))
    if no_chat:   # server-flows-1: never typed into a headless run, a command or a sign-in desk
        return False, no_chat, "", reach
    if reach == "idle":
        return False, "no CLI running in this desk's terminal — start one", "", reach
    tip = arm_tip(target)
    return False, ("not armed and no terminal desk here — " + tip + " in that session"
                   if tip else "no terminal desk here"), "", reach


def arm_tip(name):
    """"type <the adapter's arm command> " for a desk, or "" with no inbox."""
    cmd = INBOX.meta().get("arm_command")
    if not isinstance(cmd, str) or not cmd:
        return ""
    return "type " + cmd.replace("{desk}", desk_slug(name))


def load_aliases():
    try:
        a = json.loads(ALIASES.read_text())
        return {str(k): str(v) for k, v in a.items()} if isinstance(a, dict) else {}
    except (OSError, ValueError, AttributeError):
        return {}


_ALIAS_LOCK = threading.Lock()


def set_alias(name, alias):
    """Display name for a desk (48 chars, control chars stripped); an empty
    alias removes it. The wire name never changes. One read-modify-write at a
    time, through a unique temp file (review server-concurrency-4); raises
    OSError when the state dir cannot be written."""
    name = (name or "").strip()[:120]
    alias = CTRL_RE.sub("", (alias or "")).strip()[:48]
    if not name:
        return False
    with _ALIAS_LOCK:
        a = load_aliases()
        if alias:
            a[name] = alias
        else:
            a.pop(name, None)
        fd, tmp = tempfile.mkstemp(prefix="aliases.", suffix=".tmp", dir=str(ALIASES.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps(a, indent=1))
            os.chmod(tmp, 0o600)
            os.replace(tmp, ALIASES)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    return True


def desk_slug(raw):
    """Rail name -> tmux session suffix (fc-<slug>). Same rule as /ws/term."""
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", raw or "").strip("-.")[:40]
    if slug != raw:   # long/exotic names: hash keeps tmux lanes distinct
        slug = slug[:32] + "-" + hashlib.md5((raw or "").encode("utf-8", "surrogatepass")).hexdigest()[:6]
    return slug


CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
CLI_START = {"claude": "claude", "cursor": "cursor-agent"}


def reach_of(name):
    """How a message can get to this desk right now:
    inbox = the desk holds the inbox adapter; pane = its CLI runs in a tmux desk but
    nobody is armed (we paste into the CLI); idle = the desk's pane is a bare
    shell; none = no desk here and not armed."""
    for d in desks()["desks"]:
        if d["name"] == name:
            return d["reach"], d
    return "none", None


def tmux_ok(*argv, timeout=5):
    try:
        r = subprocess.run(tmuxcmd.argv(*argv), capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, (r.stderr or "").strip()
    except Exception as e:
        return False, str(e)


_PASTE_LOCK = threading.Lock()


def no_chat_reason(name, snap=None):
    """The refusal for a desk whose registry kind never takes typed chat (a
    headless run, a catalogue command), else None (review server-flows-1)."""
    reg = (snap or snapshot())["reg"]
    return no_chat_of(reg.get(name))


def tmux_load(buf, text):
    """Load ``text`` into tmux buffer ``buf`` on ``load-buffer``'s stdin, never as
    an argument: tmux's argument parser ends a command at an argument whose last
    character is ";" (and drops it), and turns a trailing backslash-semicolon into
    ";" (tmux 3.4), so text arrives byte for byte only this way. (ok, error)."""
    try:
        data = text.encode("utf-8")
    except UnicodeEncodeError:
        return False, "the text is not UTF-8"
    try:
        r = subprocess.run(tmuxcmd.argv("load-buffer", "-b", buf, "-"), input=data, capture_output=True,
                           timeout=5)
        return r.returncode == 0, r.stderr.decode("utf-8", "replace").strip()
    except Exception as e:
        return False, str(e)


def paste_text(target, text):
    """Paste ``text`` into a pane as a person's terminal pastes it (bracketed when
    the program asked for that), then Enter: loaded on stdin into a buffer of its
    own, pasted and deleted, one paste at a time (review server-flows-11)."""
    buf = "metachat-" + secrets.token_hex(4)
    with _PASTE_LOCK:
        ok, err = tmux_load(buf, text)
        if not ok:
            return False, err or "load-buffer failed"
        ok, err = tmux_ok("paste-buffer", "-d", "-p", "-b", buf, "-t", target)
        if not ok:
            tmux_ok("delete-buffer", "-b", buf)
            return False, err or "paste failed"
        time.sleep(0.2)
        return tmux_ok("send-keys", "-t", target, "Enter")


# Review 1.2 (11-09, found live): a paste is followed by Enter, and in a Claude
# permission dialog with "1. Yes" highlighted that Enter approves the tool call;
# in a full-screen picker (/resume) the text is swallowed. A desk that waits on
# an ask is never typed into.
WAITING_ERR = "the desk is waiting on a question — answer it first"
LINK_RE = re.compile(r"\S*://\S*|\bwww\.\S+", re.I)


def waiting_refusal(name):
    """WAITING_ERR (with the ask line: links out, cut to 120 characters) when
    the desk's pane, read now, waits on an ask (mb.needs_info); else None."""
    info = mb.needs_info(MB, desk_slug(name), fresh=True)
    if not info.get("needs") or info.get("ask") == "text":   # a text ask is answered by the chat line
        return None
    line = CTRL_RE.sub("", LINK_RE.sub("(link)", info.get("ask_line") or "")).strip()[:120].rstrip()
    return f"{WAITING_ERR}: {line}" if line else WAITING_ERR


def paste_pane(name, text):
    """Type text into the desk's running CLI: bracketed paste, then Enter (paste_text).
    Refuses when the pane is a bare shell (chat text must never hit bash) and
    when the registry says the desk is a run or a command desk: a sniffed
    ``claude`` there is the headless engine, and the bytes would run as a shell
    command once it exits (review server-flows-1). Each paste uses its own tmux
    buffer and the pastes are serialised (review server-flows-11)."""
    snap = snapshot()
    reason = no_chat_reason(name, snap)
    if reason:
        return False, reason
    sniff = snap["panes"].get(desk_slug(name))
    if sniff not in ("claude", "cursor"):
        return False, "no CLI running in this desk's terminal"
    text = CTRL_RE.sub("", text.replace("\r\n", "\n")).strip()
    if not text:
        return False, "empty"
    if len(text) > 4000:
        return False, "too long for the terminal (4000 chars)"
    waiting = waiting_refusal(name)
    if waiting:
        wslog(f"pane-send desk={name} chars={len(text)} refused=waiting")
        return False, waiting
    target = "=fc-" + desk_slug(name) + ":"   # exact session, its window: pane commands need the colon
    ok, err = paste_text(target, text)
    wslog(f"pane-send desk={name} chars={len(text)} ok={ok}")
    return ok, err


ARM_NEEDS_CLAUDE = "arm needs Claude running in this desk"

# ---- resume (11-09): an ended Claude session reopens in a desk, so chat reaches it again
SID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
RESUME_BUSY_S = 90      # a transcript written this recently may be open in another terminal
SAVED_TTL = 20.0
_saved = {"t": 0.0, "key": None, "data": {}}
_saved_lock = threading.Lock()


def find_transcript(sid):
    """(transcript path, mtime, config dir or None for the default) of a Claude session id, newest
    first, from the config folders ``claude_homes`` names."""
    best = None
    for base in CLAUDE_HOMES:
        root = Path.home() / base
        for t in (root / "projects").glob(f"*/{sid}.jsonl"):
            try:
                m = t.stat().st_mtime
            except OSError:
                continue
            if best is None or m > best[1]:
                best = (t, m, None if base == ".claude" else str(root))
    return best


def saved_sessions(fresh=False, reg=None):
    """name -> (session id, transcript path, mtime, config dir or None): the
    newest Claude transcript for that name, from the names dirs (id -> name:
    the app's own, then the inbox adapter's) and, for desks the app started,
    the registry's ``session_id`` (which wins). Cached SAVED_TTL; ``fresh``
    re-reads."""
    dirs = names_dirs()
    key = "|".join(map(str, dirs)) + "|" + "|".join(CLAUDE_HOMES)
    with _saved_lock:
        if not fresh and _saved["key"] == key and time.time() - _saved["t"] < SAVED_TTL:
            return _saved["data"]
        out = {}
        files = []
        for nd in dirs:
            try:
                files += list(nd.iterdir()) if nd.is_dir() else []
            except OSError:
                pass
        for f in files:
            if not SID_RE.fullmatch(f.name):
                continue
            try:
                name = f.read_text().strip()
            except OSError:
                continue
            if not name or name.startswith("session-"):
                continue
            hit = find_transcript(f.name)
            if hit and (name not in out or hit[1] > out[name][2]):
                out[name] = (f.name, hit[0], hit[1], hit[2])
        for name, meta in (reg if reg is not None else mb.registry(MB)).items():
            sid = meta.get("session_id") if isinstance(meta, dict) else None
            if meta.get("kind") == "claude" and isinstance(sid, str) and SID_RE.fullmatch(sid):
                hit = find_transcript(sid)
                if hit:
                    out[name] = (sid, hit[0], hit[1], hit[2])
        _saved.update(t=time.time(), key=key, data=out)
        return out


def transcript_cwd(path, max_lines=200):
    """The first ``cwd`` a Claude transcript records, or ""."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i >= max_lines:
                    break
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and isinstance(row.get("cwd"), str) and row["cwd"]:
                    return row["cwd"]
    except OSError:
        pass
    return ""


def resume_pane(name):
    """POST /api/pane resume -> (ok, error, desk name). ``claude --resume <id>``
    in the session's own folder: typed into the desk's bare shell when the desk
    exists, else in a new desk named as the session. Never for a run, command,
    sign-in or account desk, a desk whose CLI runs, or a transcript written in
    the last RESUME_BUSY_S (it may be open elsewhere)."""
    snap = snapshot(fresh=True)
    reason = no_chat_reason(name, snap)
    if reason:
        return False, reason, None
    if (snap["reg"].get(name) or {}).get("account"):
        return False, "resume works for sessions on the PC's own Claude sign-in", None
    slug = desk_slug(name)
    sniff = snap["panes"].get(slug)
    if sniff in ("claude", "cursor"):
        return False, f"{sniff} already runs in this desk — just send", None
    s = saved_sessions(fresh=True, reg=snap["reg"]).get(name)
    if not s:
        return False, "no saved Claude session under this name", None
    sid, path, mtime, conf = s
    age = time.time() - mtime
    if age < RESUME_BUSY_S:
        tip = arm_tip(name)
        return False, (f"this session was written {int(age)}s ago — it may be open in another "
                       "terminal" + (f"; {tip} there" if tip else "")), None
    cwd = mb.cwd_ok(MB, transcript_cwd(path))
    if not cwd:
        return False, "its folder is not one of the project folders", None
    if sniff == "shell":
        target = "=fc-" + slug + ":"
        env = f"CLAUDE_CONFIG_DIR={shlex.quote(conf)} " if conf else ""
        ok, err = tmux_ok("send-keys", "-t", target, "-l", f"cd -- {shlex.quote(cwd)} && {env}claude --resume {sid}")
        if ok:
            ok, err = tmux_ok("send-keys", "-t", target, "Enter")
        wslog(f"resume desk={name} in=pane ok={ok}")
        return ok, err, name if ok else None
    label = name if mb.LABEL_RE.fullmatch(name) and name not in snap["reg"] else None
    status, reply = mb.spawn_desk(MB, {"kind": "claude", "cwd": cwd, "label": label}, NAMES_DIR,
                                  extra_argv=["--resume", sid], session_id=sid,
                                  env_add={"CLAUDE_CONFIG_DIR": conf} if conf else None)
    if status == 200:
        invalidate()
        if reply.get("name") != name:   # a name tmux cannot carry: the new desk shows the session's name
            try:
                set_alias(reply["name"], name)
            except OSError:
                pass
    wslog(f"resume desk={name} in=new ok={status == 200}")
    return bool(reply.get("ok")), reply.get("error") or "", reply.get("name")


def arm_pane(name):
    """POST /api/pane arm: the inbox adapter arms the desk's Claude (it types
    its own command through ctx.paste, so paste_pane's rules hold: never a
    run, command or sign-in desk, never a desk that waits on an ask). A desk
    whose pane does not run claude is refused; with no adapter, nothing arms."""
    snap = snapshot()
    reason = no_chat_reason(name, snap)
    if reason:
        return False, reason
    if snap["panes"].get(desk_slug(name)) != "claude":
        return False, ARM_NEEDS_CLAUDE
    res = INBOX.arm(name)
    if res["ok"]:
        invalidate()
    return res["ok"], res["error"]


def start_cli(name, kind):
    """Start claude / cursor-agent in a desk whose pane is a bare shell."""
    cmd = CLI_START.get(kind)
    if not cmd:
        return False, "unknown cli"
    snap = snapshot()
    reason = no_chat_reason(name, snap)
    if reason:
        return False, reason
    sniff = snap["panes"].get(desk_slug(name))
    if sniff is None:
        return False, "no terminal desk here"
    if sniff != "shell":
        return False, f"{sniff} already runs here"
    target = "=fc-" + desk_slug(name) + ":"   # exact session, its window: pane commands need the colon
    sid = None
    if kind == "claude":
        # 11-10: name the session (names/<id> = desk), so Resume finds it later
        sid = str(uuid.uuid4())
        cmd = f"{cmd} --session-id {sid}"
    ok, err = tmux_ok("send-keys", "-t", target, "-l", cmd)
    if ok:
        ok, err = tmux_ok("send-keys", "-t", target, "Enter")
    if ok and sid:
        try:
            NAMES_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
            (NAMES_DIR / sid).write_text(name)
            _saved["t"] = 0.0
        except OSError:
            pass
    wslog(f"start-cli desk={name} cli={kind} ok={ok}")
    return ok, err


_pane_cwd = {}
_pane_cli_pid = {}      # desk -> pid of the claude / cursor process its pane runs (cli_of_panes)
# SEC-9: a CLI is recognised by its executable only (argv[0]'s basename), never
# by a bare word elsewhere on a command line ("ssh claude", "less claude").
# cursor-agent's launcher execs node with argv[0] = the path it was invoked as,
# which is ~/.local/bin/cursor-agent or its installer-made alias ~/.local/bin/agent.
CLI_EXE = {"claude": "claude", "cursor-agent": "cursor", "agent": "cursor"}


def cli_of_args(args):
    """claude | cursor | None from one ps ``args`` string, by argv[0] alone."""
    exe = (args or "").split(None, 1)[0] if (args or "").strip() else ""
    return CLI_EXE.get(exe.rsplit("/", 1)[-1])


def cli_of_panes():
    """fc-<desk> -> which CLI its pane is running: claude | cursor | shell.

    pane_current_command just says "bash" (TUIs are node), so walk each pane
    pid's process tree and sniff the executables. The same call also keeps
    each pane's current path in _pane_cwd for /api/desks."""
    panes = {}
    try:
        out = subprocess.run(tmuxcmd.argv("list-panes", "-a", "-F", "#S #{pane_pid} #{pane_current_path}"),
                             capture_output=True, text=True, timeout=3).stdout
    except Exception:
        return panes
    pane_pid, cwd_of = {}, {}
    for line in out.splitlines():
        f = line.split(None, 2)
        if len(f) >= 2 and (f[0].startswith("fc-") or f[0] == SHARED_DESK) and f[1].isdigit():
            pane_pid[f[0]] = int(f[1])
            cwd_of[f[0]] = f[2].strip() if len(f) == 3 else ""
    # the cwd map is the panes that exist now, nothing older (review server-concurrency-9)
    _pane_cwd.clear()
    _pane_cwd.update({(s[3:] if s.startswith("fc-") else s): c for s, c in cwd_of.items()})
    if not pane_pid:
        return panes
    tbl = procs.table()
    if not tbl:
        return panes
    children = procs.children(tbl)
    args_of = {pid: row[1] for pid, row in tbl.items()}
    cli_pid = {}
    for sess, root in pane_pid.items():
        desk = sess[3:] if sess.startswith("fc-") else (SHARED_DESK if sess == SHARED_DESK else None)
        if desk is None:
            continue
        stack, seen, cli = [root], set(), "shell"
        while stack:
            pid = stack.pop()
            if pid in seen:
                continue
            seen.add(pid)
            found = cli_of_args(args_of.get(pid, ""))
            if found:
                cli = found
                cli_pid[desk] = pid
                break
            stack.extend(children.get(pid, []))
        panes[desk] = cli
    _pane_cli_pid.clear()
    _pane_cli_pid.update(cli_pid)
    return panes


_SNAP_LOCK = threading.Lock()
_snap = {"t": 0.0, "data": None, "tree": None}


def invalidate():
    """Forget the desks snapshot and the names cache (after a spawn or a kill)."""
    _sess_cache[0] = 0.0
    _saved["t"] = 0.0
    with _SNAP_LOCK:
        _snap["t"] = 0.0


def snapshot(fresh=False):
    """21.4: one shared view of tmux, the names dir, the armed desks, the registry
    and needs_you, built at most once per SNAP_TTL under a lock; concurrent
    callers wait for the one build and share it. ``fresh`` forces a rebuild
    (kill reads the armed desks as they are now). A zeroed ``_sess_cache[0]``
    (how the code and the tests say "the names changed") also forces one."""
    with _SNAP_LOCK:
        now = time.time()
        if (not fresh and _snap["data"] is not None and now - _snap["t"] < SNAP_TTL
                and _sess_cache[0] != 0.0):
            return _snap["data"]
        data = _build_snapshot()
        _snap["t"], _snap["data"], _snap["tree"] = time.time(), data, None
        return data


def desks(scope=None, fresh=False, viewer="desktop"):
    """Orchestrator view: one row per named session (desk), armed = the desk
    holds the inbox adapter (INBOX.armed()). Phase 1
    adds the registry entry (meta), project, cwd and needs_you. Served from the
    shared snapshot; rows are copies. ``viewer`` is the requester's scope: a
    sign-in row's ``where`` says where its link opens — "here" in this machine's
    own browser, "pc" for a phone (11-12 D1: the login's callback lands on this
    machine, so on the phone that started it the link opens nowhere). The field
    is per-request, never in the shared snapshot."""
    snap = snapshot(fresh)
    rows = [dict(d) for d in snap["rows"]]
    where = "pc" if viewer == "phone" else "here"
    for d in rows:
        if d.get("signin") is not None:
            d["signin"] = {**d["signin"], "where": where}
    if scope == "metachat":
        rows = [d for d in rows if d["name"] in snap["reg"]]
    return {"desks": rows, "streams": dict(snap["streams"])}


def _build_snapshot():
    out = {}
    for name, w in INBOX.armed().items():
        out[name] = {"name": name, "armed": True, "armed_at": w["armed_at"], "n": w["n"]}
    for n in sessions():
        out.setdefault(n, {"name": n, "armed": False, "armed_at": 0, "n": 0})
    reg = mb.registry(MB)
    for n in reg:
        out.setdefault(n, {"name": n, "armed": False, "armed_at": 0, "n": 0})
    panes = cli_of_panes()
    cwds = dict(_pane_cwd)
    saved = saved_sessions(reg=reg)
    needs = {}
    for d in out.values():
        slug = desk_slug(d["name"])
        sniff = panes.get(slug)
        meta = reg.get(d["name"], {})
        d["pane"] = sniff is not None
        # a run desk's sniffed claude is the headless engine, a command desk
        # runs a metabot subcommand and a sign-in desk shows a sign-in screen:
        # none takes typed chat (server-flows-1, 25.3 B1)
        chatty = sniff in ("claude", "cursor") and not no_chat_of(meta)
        d["reach"] = ("inbox" if d["armed"] else "pane" if chatty
                      else "idle" if sniff else "none")
        # an ended Claude session with a saved transcript: one tap reopens it (resume_pane)
        d["resume"] = (d["reach"] in ("none", "idle") and sniff in (None, "shell")
                       and d["name"] in saved and not meta.get("account") and not no_chat_of(meta))
        cli = sniff
        if sniff == "shell" and CLAUDE_PREFIXES and d["name"].startswith(CLAUDE_PREFIXES):
            cli = "claude"   # an idle pane in a Claude desk: the ⌘ sheet shows Claude keys
        if not cli:
            # nothing attached yet — guess from the desk name
            cli = ("claude" if CLAUDE_PREFIXES and d["name"].startswith(CLAUDE_PREFIXES)
                   else "cursor" if d["name"].startswith("cursor")
                   else "shell" if d["name"].startswith("shell") else "")
        d["cli"] = cli
        d["meta"] = meta
        d["cwd"] = (cwds.get(slug) or "") if sniff is not None else ""   # a dead pane has no cwd
        d["project"] = mb.project_of(MB, d["cwd"] or meta.get("cwd") or "")
        # every desk with a pane, registered or not (11-09: an older hand-made desk sat in a
        # picker, paste_pane refused chat into it, and its row said nothing waits)
        info = (mb.needs_info(MB, slug, meta or None) if sniff is not None
                else {"needs": False, "cls": None, "ask": None, "ask_line": None, "since": None})
        d["needs_you"] = bool(info["needs"])
        # follow-ups A and B: what a waiting desk asks, and its class chip; null when nothing waits
        for k in ("cls", "ask", "ask_line"):
            d[k] = info.get(k) if d["needs_you"] else None
        if meta.get("kind") == "cli":   # review 1.2: a catalogue desk's exit status, or null
            d["exit"] = info.get("exit")
        if mb.is_signin(meta):   # 25.3 B1: which sign-in screen; a dead pane shows none
            d["signin"] = info.get("signin") or mb.signin_view(meta, [])
        needs[d["name"]] = info
    streams = INBOX.streams()
    order = lambda d: (not d["armed"], d["name"] != DEFAULT_DESK,
                       d["name"].lower())
    return {"rows": sorted(out.values(), key=order), "streams": streams, "reg": reg,
            "panes": panes, "needs": needs}


def _waiting_row(name, info):
    """One /api/tree waiting row from a desk's needs_info: its class chip, what
    it asks and the line it asks on (follow-up A), since when."""
    return {"desk": name, "run": None, "cls": info.get("cls") or None,   # no ask read: the terminal (review 1.2)
            "ask": info.get("ask") or None, "ask_line": info.get("ask_line"),
            "since": info.get("since")}


def tree():
    """GET /api/tree (22.3): waiting asks, projects -> folders -> desks and runs,
    other folders, and the names with no desk here: the inbox adapter's bucket
    (``inbox``, when it offers one) and ``ended`` (the rest). Computed from the
    desks snapshot, the registry, /api/mb/runs and the names dir; cached with
    the snapshot."""
    snap = snapshot()
    with _SNAP_LOCK:
        if _snap["tree"] is not None and _snap["data"] is snap:
            return _snap["tree"]
    rows = [dict(d) for d in snap["rows"]]
    reg, needs = snap["reg"], snap["needs"]
    projects = [{"name": str(p.get("name") or "other"),
                 "folders": [{"path": os.path.expanduser(str(x)).rstrip("/") or "/", "desks": [], "runs": []}
                             for x in (p.get("paths") or [])]}
                for p in CFG["projects"] if isinstance(p, dict)]
    folders = sorted(((f["path"], f) for p in projects for f in p["folders"]),
                     key=lambda t: len(t[0]), reverse=True)   # the longest matching path wins

    def place(folder):
        for path, f in folders:
            if folder == path or folder.startswith(path + "/"):
                return f
        return None

    other, loose, waiting = {}, [], {}

    def other_slot(folder):
        return other.setdefault(folder, {"path": folder, "desks": [], "runs": []})

    for d in rows:
        if not d["pane"] and d["name"] not in reg:
            loose.append(d)          # a name with no tmux desk here and no registry entry
            continue
        folder = d["cwd"] or (d["meta"].get("cwd") or "")
        slot = place(folder) or other_slot(folder)
        slot["desks"].append(d)
        if d["needs_you"]:
            waiting[d["name"]] = _waiting_row(d["name"], needs.get(d["name"], {}))
    runs = mb.runs(MB, 200)
    for r in runs.get("runs") or []:
        row = r.get("row") if isinstance(r.get("row"), dict) else {}
        folder = r.get("cwd") or row.get("cwd") or ""
        if not folder and isinstance(row.get("meta"), dict):
            folder = row["meta"].get("cwd") or ""
        if not isinstance(folder, str):
            folder = ""
        item = {"run_id": r.get("run_id"), "state": r.get("state"),
                "receipt_state": r.get("receipt_state"), "label": _run_label(r, row)}
        slot = place(folder) or other_slot(folder)
        slot["runs"].append(item)
        if r.get("state") == "live" and r.get("needs_you") and r.get("desk"):
            if r["desk"] not in waiting:   # runs() read the pane just now: its 2 s cache answers
                info = needs.get(r["desk"]) or {}
                waiting[r["desk"]] = _waiting_row(r["desk"], info if info.get("needs")
                                                  else mb.needs_info(MB, r["desk"]))
            waiting[r["desk"]]["run"] = r.get("run_id")
    bucket = INBOX.tree_bucket([dict(d) for d in loose])
    claimed = {d.get("name") for d in bucket["desks"]} if bucket else set()
    out = {"waiting": sorted(waiting.values(), key=lambda w: (w["since"] is None, w["since"] or 0)),
           "projects": projects,
           "other": [other[k] for k in sorted(other)],
           "ended": [d for d in loose if d["name"] not in claimed]}
    if bucket:
        out["inbox"] = bucket
    with _SNAP_LOCK:
        if _snap["data"] is snap:
            _snap["tree"] = out
    return out


def _run_label(r, row):
    """A run's one-line label: an index field (MetaBot flattens the run meta
    into the row) or the same key under ``meta``, else the run id."""
    meta = row.get("meta") if isinstance(row, dict) and isinstance(row.get("meta"), dict) else {}
    for key in ("label", "name", "task", "prompt"):
        for src in (row if isinstance(row, dict) else {}, meta):
            v = src.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip().splitlines()[0][:60]
    return str(r.get("run_id") or "")


_inbox_started = {"inbox": None}


def ensure_inbox():
    """Start the inbox adapter once (its readers are shared by every page,
    however many reconnect); a reconfigure's new adapter is started anew."""
    with _local_lock:
        if _inbox_started["inbox"] is INBOX:
            return
        _inbox_started["inbox"] = INBOX
    INBOX.start(INBOX_CTX)


def _fanout(ev):
    """Hand one inbox event to every /api/stream subscriber (InboxCtx.emit)."""
    if not isinstance(ev, dict):
        return
    with _local_lock:
        for q in list(_local_subs):
            q.put(dict(ev))


# ---------------------------------------------------------------- terminal

def ws_accept(key):
    return base64.b64encode(
        hashlib.sha1((key + WS_GUID).encode()).digest()).decode()


def ws_send(sock, payload, opcode=0x2, lock=None):
    """One unmasked server frame. ``lock`` serialises frames on a socket two
    threads write (the terminal thread and a kill's close frame, review
    server-concurrency-10): sendall of a large frame is several send() calls."""
    ln = len(payload)
    hdr = bytes([0x80 | opcode])
    if ln < 126:
        hdr += bytes([ln])
    elif ln < 65536:
        hdr += b"\x7e" + struct.pack(">H", ln)
    else:
        hdr += b"\x7f" + struct.pack(">Q", ln)
    if lock is None:
        sock.sendall(hdr + payload)
    else:
        with lock:
            sock.sendall(hdr + payload)


def ws_close(sock, code, reason, lock=None):
    """A close frame with a status code, then the socket is shut."""
    try:
        ws_send(sock, struct.pack(">H", code) + reason.encode()[:120], opcode=0x8, lock=lock)
    except OSError:
        pass
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


def set_winsize(master, cols, rows):
    cols = max(20, min(int(cols), 500))
    rows = max(5, min(int(rows), 200))
    fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def ws_drain(buf, sink, sock, ctl=None, pong=None, lock=None):
    """Consume every complete client frame in buf; sink() gets data payloads,
    ctl() any NUL-prefixed JSON control message, pong() is told of each pong
    frame. Returns (rest, closed)."""
    while True:
        if len(buf) < 2:
            return buf, False
        b1, b2 = buf[0], buf[1]
        opcode, masked, ln = b1 & 0x0F, b2 & 0x80, b2 & 0x7F
        idx = 2
        if ln == 126:
            if len(buf) < 4:
                return buf, False
            ln, idx = struct.unpack(">H", buf[2:4])[0], 4
        elif ln == 127:
            if len(buf) < 10:
                return buf, False
            ln, idx = struct.unpack(">Q", buf[2:10])[0], 10
        mask = b""
        if masked:
            if len(buf) < idx + 4:
                return buf, False
            mask, idx = buf[idx:idx + 4], idx + 4
        if len(buf) < idx + ln:
            return buf, False
        payload, buf = buf[idx:idx + ln], buf[idx + ln:]
        if masked:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        if opcode == 0x8:
            return buf, True
        if opcode == 0x9:
            ws_send(sock, payload, opcode=0xA, lock=lock)
        elif opcode == 0xA:
            if pong:
                pong()
        elif opcode in (0x1, 0x2):
            if ctl and payload[:1] == b"\x00":
                try:
                    ctl(json.loads(payload[1:].decode()))
                except Exception:
                    pass
            else:
                sink(payload)


def wslog(msg):
    try:
        _rotate(WSLOG)
        with open(WSLOG, "a") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
    except OSError:
        pass


TERM_SOCKS = {}            # desk -> {socket: send lock} of open /ws/term sockets (for kill: 4001)
TERM_LOCK = threading.Lock()
# /ws/term close codes (21.4): 1000 client closed · 1001 peer gone (two pings unanswered) ·
# 1011 the PTY or tmux could not be started · 4000 idle TERM_IDLE_S · 4001 killed ·
# 4004 no such desk · 4008 too many terminals on one desk


def close_terms(desk, code=4001, reason="killed"):
    with TERM_LOCK:
        socks = list(TERM_SOCKS.get(desk, {}).items())
    for s, lock in socks:
        ws_close(s, code, reason, lock)
    return len(socks)


def tls_pending(sock):
    """Bytes already decrypted inside a TLS socket: select() cannot see them (C2)."""
    return isinstance(sock, ssl.SSLSocket) and sock.pending() > 0


def peer_gone(conn):
    """An EventSource never writes after its request, so a readable socket is EOF.
    Plain: peek one byte. TLS: a non-blocking read of one record (EOF, a close
    alert or nothing yet); a byte the client did send is dropped, which is fine."""
    if isinstance(conn, ssl.SSLSocket):
        if conn.pending():
            return False
        r, _, _ = select.select([conn], [], [], 0)
        if not r:
            return False
        conn.setblocking(False)
        try:
            return not conn.recv(1)
        except ssl.SSLWantReadError:
            return False
        except (OSError, ssl.SSLError):
            return True
        finally:
            conn.setblocking(True)
    r, _, _ = select.select([conn], [], [], 0)
    return bool(r) and not conn.recv(1, socket.MSG_PEEK)


def term_session(sock, desk="", peer="?", create=True):
    """Bridge one WebSocket to a PTY on this desk's own tmux session.

    Every desk gets a persistent session (fc-<desk>; the shared default is
    config ``shared_desk``, "main") so state, cwd and running jobs survive
    tablet reconnects.
    ``create`` is False for a legacy session that exists but is not in the
    registry: it is attached, never made.
    """
    wslog(f"open desk={desk or SHARED_DESK} peer={peer}")
    key = desk or SHARED_DESK
    lock = threading.Lock()
    with TERM_LOCK:
        socks = TERM_SOCKS.setdefault(key, {})
        full = len(socks) >= TERM_MAX_PER_DESK
        if not full:
            socks[sock] = lock
    if full:   # 21.4: at most TERM_MAX_PER_DESK sockets per desk
        wslog(f"refuse desk={key}: too many terminals")
        ws_close(sock, 4008, "too many terminals", lock)
        return

    def drop():
        with TERM_LOCK:
            TERM_SOCKS.get(key, {}).pop(sock, None)

    master, slave = pty.openpty()

    def preexec():  # new session + slave as controlling tty -> job control
        os.setsid()
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)

    env = dict(os.environ, TERM="xterm-256color", COLORTERM="truecolor")
    if tmuxcmd.found():
        session = SHARED_DESK if not desk else f"fc-{desk}"
        # create-if-missing, then attach. Session-scoped options keep the
        # pane clean: no green status bar, mouse OFF so drags are xterm
        # selections (copy-on-select), not tmux copy-mode. Other tmux
        # sessions on this machine are untouched. A wedged tmux server is a
        # 1011 close, not a parked thread (review server-concurrency-7).
        try:
            if create:
                subprocess.run(tmuxcmd.argv("new-session", "-d", "-s", session),
                               capture_output=True, timeout=10)  # fine if it already exists
            for opt in (("status", "off"), ("mouse", "off")):
                subprocess.run(tmuxcmd.argv("set-option", "-t", session, *opt),
                               capture_output=True, timeout=10)
        except (subprocess.TimeoutExpired, OSError) as e:
            wslog(f"spawn FAILED desk={key}: tmux {type(e).__name__}")
            os.close(slave)
            os.close(master)
            drop()
            ws_close(sock, 1011, "tmux did not answer", lock)
            return
        cmd = tmuxcmd.argv("attach-session", "-t", session)
    else:
        cmd = ["bash", "-l"]
    try:
        p = subprocess.Popen(cmd, stdin=slave, stdout=slave, stderr=slave,
                             preexec_fn=preexec, close_fds=True,
                             cwd=str(Path.home()), env=env)
    except Exception as e:
        wslog(f"spawn FAILED desk={key}: {e}")
        os.close(slave)
        os.close(master)
        drop()
        ws_close(sock, 1011, "spawn failed", lock)
        return
    wslog(f"spawn ok desk={key} pid={p.pid} cmd={cmd[0]}")
    os.close(slave)
    set_winsize(master, 120, 36)
    buf = b""
    state = {"input": time.time(), "unanswered": 0}
    next_ping = time.time() + TERM_PING_S
    close = None

    def on_input(payload):
        state["input"] = time.time()
        os.write(master, payload)

    def on_ctl(c):
        state["input"] = time.time()
        if c.get("type") == "resize":
            set_winsize(master, c["cols"], c["rows"])

    def on_pong():
        state["unanswered"] = 0

    try:
        while True:
            now = time.time()
            idle = now - state["input"]
            if idle >= TERM_IDLE_S:
                close = (4000, "idle")
                break
            if now >= next_ping:
                if state["unanswered"] >= 2:
                    close = (1001, "peer gone")
                    break
                ws_send(sock, b"", opcode=0x9, lock=lock)  # keepalive ping
                state["unanswered"] += 1
                next_ping = now + TERM_PING_S
            wait = max(0.05, min(next_ping - now, TERM_IDLE_S - idle, 30.0))
            pend = tls_pending(sock)
            r, _, _ = select.select([sock, master], [], [], 0 if pend else wait)
            if not r and not pend:
                if p.poll() is not None:
                    break
                continue
            if sock in r or pend:
                data = sock.recv(65536)
                if not data:
                    break
                buf += data
                buf, closed = ws_drain(buf, on_input, sock, ctl=on_ctl, pong=on_pong, lock=lock)
                if closed:
                    break
            if master in r:
                try:
                    out = os.read(master, 65536)
                except OSError:
                    break
                if not out:
                    break
                ws_send(sock, out, lock=lock)
    except (BrokenPipeError, ConnectionResetError, OSError) as e:
        wslog(f"io error desk={key}: {type(e).__name__} {e}")
    finally:
        drop()
        if close:
            ws_close(sock, close[0], close[1], lock)
        wslog(f"close desk={key} peer={peer} child_rc={p.poll()}"
              + (f" code={close[0]} {close[1]}" if close else ""))
        try:
            os.close(master)
        except OSError:
            pass
        _end_child(p)  # ends the tmux *client*; the session lives on


def _end_child(p, grace=2.0):
    """Terminate a child and wait for it, so no process or zombie is left behind."""
    try:
        p.terminate()
        p.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        try:
            p.kill()
            p.wait(timeout=grace)
        except Exception:
            pass
    except Exception:
        pass


def vnc_bridge(sock, peer):
    """Bridge one WebSocket (noVNC client) to the local VNC server.

    The virtual display (:99, fc-gui.service) is the computer-use harness:
    today the human drives it from the tablet; an agent can drive the same
    display later (xdotool/scrot on :99). Raw RFB bytes pass through
    untouched — this is a dumb, fast pipe."""
    wslog(f"vnc open peer={peer}")
    try:
        tcp = socket.create_connection(VNC_BACKEND, timeout=5)
        tcp.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError as e:
        wslog(f"vnc backend down: {e}")
        try:
            ws_send(sock, b"", opcode=0x8)
        except OSError:
            pass
        return
    buf = b""
    try:
        while True:
            pend = tls_pending(sock)
            r, _, _ = select.select([sock, tcp], [], [], 0 if pend else 30)
            if not r and not pend:
                ws_send(sock, b"", opcode=0x9)  # keepalive
                continue
            if sock in r or pend:
                data = sock.recv(65536)
                if not data:
                    break
                buf += data
                buf, closed = ws_drain(buf, tcp.sendall, sock)
                if closed:
                    break
            if tcp in r:
                try:
                    data = tcp.recv(65536)
                except OSError:
                    break
                if not data:
                    break
                ws_send(sock, data)
    except (BrokenPipeError, ConnectionResetError, OSError) as e:
        wslog(f"vnc io error peer={peer}: {type(e).__name__} {e}")
    finally:
        wslog(f"vnc close peer={peer}")
        try:
            tcp.close()
        except OSError:
            pass


# ---------------------------------------------------------------- http

def meta():
    v = mb.versions(MB)
    return {"app": APP, "version": VERSION, "build": BUILD,
            "metabot": v["metabot"], "orcha": v["orcha"],
            "display": {"vnc": mb.vnc_up(VNC_BACKEND), "unit": mb.active_unit(MB)},
            "default_desk": DEFAULT_DESK, "default_folder": mb.default_folder(MB),
            "limits": {"max_desks": CFG["max_desks"], "max_runs": CFG["max_runs"]},
            "modules": len(MODULES["modules"]), "catalog": len(catalog.ENTRIES),
            "inbox": INBOX.meta() or None}


def projects():
    """GET /api/projects: configured projects plus "other", with their desks
    and the count of index rows whose cwd sits under a path."""
    rows = [{"name": str(p.get("name") or "other"),
             "paths": [str(x) for x in (p.get("paths") or [])], "desks": [], "runs": 0}
            for p in CFG["projects"] if isinstance(p, dict)]
    rows.append({"name": "other", "paths": [], "desks": [], "runs": 0})
    by = {r["name"]: r for r in rows}
    for d in desks()["desks"]:
        by.get(d["project"], by["other"])["desks"].append(d["name"])
    q = mb.mbq(MB, "runs")
    for row in (q.get("rows") if isinstance(q, dict) else None) or []:
        if not isinstance(row, dict):
            continue
        cwd = row.get("cwd")
        if not isinstance(cwd, str) and isinstance(row.get("meta"), dict):
            cwd = row["meta"].get("cwd")
        name = mb.project_of(MB, cwd if isinstance(cwd, str) else "")
        by.get(name, by["other"])["runs"] += 1
    return {"projects": rows}


def kill_desk(body):
    """POST /api/kill -> (status, reply). A registered desk whose tmux session
    is already gone is still killed: its registry entry and the names-dir file
    MetaChat wrote are swept so the name and the slot come free (review F11)."""
    name = body.get("name")
    # a registered name is killable as it is, even when desk_slug would shorten
    # it (an older over-long run label, review server-flows-4)
    if not isinstance(name, str) or not name or not (
            desk_slug(name) == name or (re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", name)
                                        and mb.reg_read(MB, name) is not None)):
        return 400, {"ok": False, "error": "bad name"}
    if body.get("confirm") is not True:
        return 400, {"ok": False, "error": "confirm"}
    if DEFAULT_DESK and name in (DEFAULT_DESK, desk_slug(DEFAULT_DESK)):
        return 403, {"ok": False, "error": "default desk"}
    for d in desks(fresh=True)["desks"]:   # the armed desks as they are now, not a second-old view
        if d["armed"] and desk_slug(d["name"]) == name:
            return 403, {"ok": False, "error": "armed"}
    entry = mb.reg_read(MB, name)     # read before kill_session drops it
    alive = mb.desk_alive(MB, name, entry)
    if not alive and entry is None:
        return 404, {"ok": False, "error": "no such desk"}
    if alive:
        ok, err = mb.kill_desk_any(MB, name, entry)
        if not ok:
            return 502, {"ok": False, "error": err or "kill failed"}
    mb.forget_desk(MB, name, NAMES_DIR, entry=entry)
    invalidate()
    closed = close_terms(name)
    HIST.append("kill", desk=name, data={"terms_closed": closed, "session_gone": not alive})
    wslog(f"kill desk={name} terms={closed} session_gone={not alive}")
    return 200, {"ok": True}


# 25.3 B3: the keys a tablet button may press, as tmux key names
KEY_NAMES = {"enter": "Enter", "esc": "Escape", "up": "Up", "down": "Down", "tab": "Tab",
             "1": "1", "2": "2", "3": "3", "y": "y", "n": "n", "q": "q"}
KEYS_MAX = 4
CODE_MAX = 512
CODE_CTRL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\ud800-\udfff]")   # controls, and lone surrogates (they are not UTF-8)


def send_keys(body):
    """25.3 B3: POST /api/keys -> (status, reply). ``key`` or ``keys`` (1-4,
    in order) from KEY_NAMES go to any live desk in one ``send-keys``;
    ``text`` (a sign-in code, 1-512 characters, no control characters) goes
    only to a sign-in desk whose screen, read now, asks for the code, never
    to the default desk: pasted as chat is (paste_text: on load-buffer's
    stdin, so tmux's argument parser never sees it), then Enter. The text
    is never logged."""
    name = body.get("name")
    if not isinstance(name, str) or not name or desk_slug(name) != name:
        return 400, {"ok": False, "error": "bad name"}
    given = [k for k in ("key", "keys", "text") if body.get(k) is not None]
    if len(given) != 1:
        return 400, {"ok": False, "error": "send one of key, keys or text"}
    what = given[0]
    val = body[what]
    if what == "key":
        if not isinstance(val, str) or val not in KEY_NAMES:
            return 400, {"ok": False, "error": "bad key"}
        keys = [val]
    elif what == "keys":
        if (not isinstance(val, list) or not 1 <= len(val) <= KEYS_MAX
                or not all(isinstance(k, str) and k in KEY_NAMES for k in val)):
            return 400, {"ok": False, "error": "bad keys"}
        keys = list(val)
    elif not isinstance(val, str) or not 1 <= len(val) <= CODE_MAX or CODE_CTRL_RE.search(val):
        return 400, {"ok": False, "error": "bad text"}
    shown = body.get("ask_line")
    if shown is not None and (not isinstance(shown, str) or len(shown) > 200):
        return 400, {"ok": False, "error": "bad ask_line"}
    entry = mb.reg_read(MB, name)
    if not mb.has_session(MB, name):
        if entry is None:
            return 404, {"ok": False, "error": "no such desk"}
        return 409, {"ok": False, "error": "desk not running"}
    target = f"=fc-{name}:"   # a pane command: the colon names the session's window
    if what != "text":
        # review 1.2: a key answers only an ask that waits now (the pane read fresh,
        # never the 2 s cache), and only the ask the tablet showed when it names one
        now = mb.needs_info(MB, name, entry, fresh=True)
        if not now.get("needs"):
            return 409, {"ok": False, "error": "nothing waits"}
        if shown is not None and shown != (now.get("ask_line") or ""):
            return 409, {"ok": False, "error": "the ask changed"}
        ok, err = tmux_ok("send-keys", "-t", target, *[KEY_NAMES[k] for k in keys])
        with MB.lock:
            MB._needs.pop(name, None)   # the next read sees what the keys did,
            MB._needs_since.pop(name, None)   # and a next ask is a new one (its own since)
        invalidate()
        wslog(f"keys desk={name} keys={','.join(keys)} ok={ok}")
        if not ok:
            return 502, {"ok": False, "error": err or "send-keys failed"}
        return 200, {"ok": True}
    if DEFAULT_DESK and name in (DEFAULT_DESK, desk_slug(DEFAULT_DESK)):
        return 403, {"ok": False, "error": "default desk"}
    view = mb.needs_info(MB, name, entry, fresh=True).get("signin") if mb.is_signin(entry) else None
    if not view or view.get("step") != "code":
        return 409, {"ok": False, "error": "not at the code step"}
    # the code goes as chat does: on load-buffer's stdin, where tmux's argument parser
    # never sees it, pasted as a person's terminal pastes it, then Enter
    ok, err = paste_text(target, val)
    wslog(f"keys desk={name} text=1 ok={ok}")
    if not ok:
        return 502, {"ok": False, "error": err or "paste failed"}
    return 200, {"ok": True}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    disable_nagle_algorithm = True   # speed plan 5: a small reply on a reused connection waited ~40 ms for the delayed ACK
    timeout = 30            # socket timeout; SSE and WS handlers clear it after upgrade

    def log_message(self, *a):
        pass

    def finish(self):
        try:
            super().finish()
        finally:
            _untrack(self.connection)

    def _qs(self):
        p = urlparse(self.path)
        return p.path, parse_qs(p.query)

    _fail = {}          # (scope, ip, kind) -> (count, blocked_until) — token / pairing-code backoff
    _FAIL_LOCK = threading.Lock()

    def _allowed_hosts(self):
        if CFG["allowed_hosts"]:
            return {h.lower() for h in CFG["allowed_hosts"] if isinstance(h, str)}
        port = self.server.server_address[1]
        hosts = {f"{a}:{port}".lower() for a in CFG["bind"] if isinstance(a, str)}
        hosts.add(f"localhost:{port}")
        return hosts

    def _desktop_only(self):
        """Pairing and device management belong to the desktop scope; a phone gets 403."""
        if self.server.scope == "phone":
            self._err(403, "desktop only")
            return False
        return True

    def _fail_key(self, kind="token"):
        """The lockout is per listener, address and kind. A phone's stale token (which over
        a USB reverse tunnel, or on a shared host, arrives from 127.0.0.1) never locks the
        desktop CLI out of its own loopback listener, and a revoked phone's retries with
        its old token are not pairing-code guesses: it pairs again at once."""
        return (self.server.scope, self.client_address[0], kind)

    def _fail_note(self, ip=None):
        """One more failed pairing-code guess from this address: the capped lockout."""
        key = self._fail_key("code")
        with self._FAIL_LOCK:
            cnt, _ = self._fail.get(key, (0, 0.0))
            cnt += 1
            self._fail[key] = (cnt, time.time() + min(5 * cnt, 60))

    def _host_ok(self):
        if self.server.scope == "phone":    # C2: the pinned certificate and the device token are the gate
            return True
        return self.headers.get("Host", "").strip().lower() in self._allowed_hosts()

    def _ok(self):
        self._setcookie = False       # per request, before any early return (SEC-1)
        ip = self._fail_key()
        now = time.time()
        with self._FAIL_LOCK:
            cnt, until = self._fail.get(ip, (0, 0.0))
            if now < until:
                return False
        _, qs = self._qs()
        presented = qs.get("t", [""])[0]
        from_query = bool(presented)
        self._token_presented = presented
        self._setcookie = False       # per request; never carried over (SEC-1)
        if not presented:
            for c in self.headers.get("Cookie", "").split(";"):
                c = c.strip()
                if c.startswith("fc_t="):
                    presented = c[5:]
                    break
        self.device = None
        if self.server.scope == "phone":    # C3: a paired device's own token, never the desktop one
            self.device = DEVICES.match(presented, "phone")
            good = self.device is not None
            if good:
                _track(self.device["id"], self.connection)   # C5: a revoke can cut this socket
        else:
            good = hmac.compare_digest(presented.encode(), TOKEN.encode())
        with self._FAIL_LOCK:
            if good:
                self._fail.pop(ip, None)
                if from_query:
                    self._setcookie = True   # a GOOD query token pins the cookie; never a bad one
            elif presented:
                # wrong-token lockout, capped at 60s: brute force on a 108-bit
                # token dies here, but a stale app recovers within a minute
                cnt += 1
                self._fail[ip] = (cnt, now + min(5 * cnt, 60))
        return good

    def _csrf_ok(self):
        ct = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if ct != "application/json":
            return False
        if self.headers.get("X-MetaChat", "").strip() != "1":
            return False
        origin = self.headers.get("Origin", "")
        if origin:
            host = re.sub(r"^https?://", "", origin.strip()).split("/")[0].lower()
            if host != self.headers.get("Host", "").strip().lower():
                return False
        sfs = self.headers.get("Sec-Fetch-Site")
        if sfs is not None and sfs.strip().lower() not in ("same-origin", "none"):
            return False
        return True

    def _common(self):
        if getattr(self, "_setcookie", False):
            tok = TOKEN
            flags = ""
            if self.server.scope == "phone":
                tok, flags = self._token_presented, "; Secure"
            self.send_header("Set-Cookie", f"fc_t={tok}; Path=/; HttpOnly; SameSite=Strict{flags}")
        if self.close_connection:   # say so: the client must not pipeline onto a closing socket
            self.send_header("Connection", "close")

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self._common()
        self.end_headers()
        self.wfile.write(body)

    def _err(self, status, reason):
        self._json({"ok": False, "error": reason}, status)

    def _bytes(self, body, mime, status=200):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self._common()
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path, mime):
        self._bytes(path.read_bytes(), mime)

    def _static(self, root, rel, allow=None):
        """One file under ``root``: resolved, contained, in the MIME table and
        (when ``allow`` is given) named in it. Else 404."""
        try:
            f = (root / rel).resolve()
        except (OSError, RuntimeError, ValueError):
            self._err(404, "not found")
            return
        if (allow is not None and rel not in allow) or root not in f.parents:
            self._err(404, "not found")
            return
        if f.is_file() and f.suffix in MIME:
            self._file(f, MIME[f.suffix])
        else:
            self._err(404, "not found")

    def do_GET(self):
        self._setcookie = False          # one Handler serves many keep-alive requests
        path, qs = self._qs()
        if "\x00" in self.path:          # SEC-10: never a Path() on a NUL
            self._err(400, "bad path")
            return
        if not self._host_ok():
            self._err(403, "bad host")
            return
        if not self._ok():
            self._err(403, "bad token")
            return
        if path == "/":
            self._file(HERE / "index.html", "text/html; charset=utf-8")
        elif path.startswith("/vendor/"):
            self._static(VENDOR, path[len("/vendor/"):])
        elif path.startswith("/tests/"):
            self._static(TESTS_DIR, path[len("/tests/"):], allow=TESTS_SERVED)
        elif path == "/api/sessions":
            self._json({"sessions": sessions()})
        elif path == "/api/desks":
            self._json(desks(qs.get("scope", [""])[0] or None, viewer=self.server.scope))
        elif path == "/api/aliases":
            self._json({"aliases": load_aliases()})
        elif path == "/api/history":
            self._json({"messages": history()})
        elif path == "/api/stream":
            self._stream()
        elif path == "/api/meta":
            self._json({**meta(), "scope": self.server.scope, "phone": phone_info(),
                        "device": self.device, "caps": caps()})
        elif path == "/api/doctor":
            self._json(doctor.report(MB, on_device()))
        elif path == "/api/dirs":                 # B6: the folder picker
            status, reply = folders.list_dirs(qs.get("under", [""])[0], str(STATE), qs.get("all", [""])[0] == "1")
            self._json(reply, status)
        elif path == "/api/devices":
            if self._desktop_only():
                self._json({"devices": DEVICES.list()})
        elif path == "/api/projects":
            self._json(projects())
        elif path == "/api/tree":
            self._json(tree())
        elif path == "/api/accounts":
            self._json(mb.accounts(MB))
        elif path == "/api/catalog":
            self._json(catalog.listing())
        elif path == "/api/modules":
            self._json(MODULES)
        elif path == "/api/extend":
            self._json(extend(fresh=qs.get("fresh", [""])[0] == "1"))
        elif path == "/api/events":
            self._json({"events": HIST.recent(_int(qs.get("limit", ["100"])[0], 100, 1, 1000))})
        elif path == "/api/mb/runs":
            limit = _int(qs.get("limit", ["50"])[0], 50, 1, 200)
            project = qs.get("project", [""])[0] or None
            self._json(mb.runs(MB, limit, project))
        elif path.startswith("/api/mb/runlog/"):
            status, reply = mb.runlog(MB, path[len("/api/mb/runlog/"):])
            self._json(reply, status)
        elif path.startswith("/api/mb/run/"):
            rid = path[len("/api/mb/run/"):]
            if not catalog.is_safe_id(rid):
                self._err(400, "bad id")
                return
            status, view = mb.run_view(MB, rid)
            if status != 200:
                self._json(view, status)
                return
            st = mb.mbq(MB, "state", rid)
            self._json({"ok": True, "run": view,
                        "receipt_state": st.get("state") if isinstance(st, dict) else None,
                        "desk": mb.live_desk(MB, rid)})
        elif path.startswith("/api/mb/receipt/"):
            rid = path[len("/api/mb/receipt/"):]
            if not catalog.is_safe_id(rid):
                self._err(400, "bad id")
                return
            f = mb.receipt_path(MB, rid)
            if f is None:
                self._err(404, "no such receipt")
                return
            self._bytes(f.read_bytes(), "application/json")
        elif path == "/ws/term":
            self._term_ws()
        elif path == "/ws/vnc":
            self._vnc_ws()
        else:
            self._err(404, "not found")

    def _ws_handshake(self):
        """Common WS checks; returns the key or None after replying."""
        key = self.headers.get("Sec-WebSocket-Key", "")
        if not key or "websocket" not in self.headers.get("Upgrade", "").lower():
            self._err(400, "not a websocket upgrade")
            return None
        # browsers send Origin on WS: refuse cross-site hijack attempts
        origin = self.headers.get("Origin", "")
        if origin:
            host = re.sub(r"^https?://", "", origin).split("/")[0].lower()
            if host != self.headers.get("Host", "").lower():
                self._err(403, "bad origin")
                return None
        return key

    def _upgrade(self, key, protocol=None):
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", ws_accept(key))
        if protocol:
            self.send_header("Sec-WebSocket-Protocol", protocol)
        self.end_headers()
        self.wfile.flush()
        try:
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            # a half-open peer (tablet asleep, WiFi gone) errors in ~60 s, not the
            # kernel's 15 min retransmit window (review server-concurrency-5)
            self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            for opt, val in (("TCP_KEEPIDLE", 30), ("TCP_KEEPINTVL", 10), ("TCP_KEEPCNT", 3)):
                if hasattr(socket, opt):
                    self.connection.setsockopt(socket.IPPROTO_TCP, getattr(socket, opt), val)
        except OSError:
            pass
        self.connection.settimeout(None)
        self.close_connection = True  # we own the socket from here

    def _term_ws(self):
        key = self._ws_handshake()
        if key is None:
            return
        _, qs = self._qs()
        desk = desk_slug(qs.get("desk", [""])[0])
        create = not desk          # only the shared default session is made bare
        if desk and not mb.has_session(MB, desk):
            # SEC-4: a registered desk whose session died comes back through
            # its registry entry (kind, cwd, account env, -u list, script),
            # never through a bare new-session; anything else is 4004.
            ok, err = mb.respawn(MB, desk) if desk in mb.registry(MB) else (False, "no such desk")
            if not ok:
                self._upgrade(key)
                wslog(f"refuse desk={desk}: {err or 'no such desk'}")
                ws_close(self.connection, 4004, "no such desk")
                return
            wslog(f"respawn desk={desk}")
        self._upgrade(key)
        term_session(self.connection, desk, self.client_address[0], create=create)

    def _vnc_ws(self):
        key = self._ws_handshake()
        if key is None:
            return
        proto = "binary" if "binary" in self.headers.get("Sec-WebSocket-Protocol", "") else None
        self._upgrade(key, proto)  # noVNC needs the protocol echoed
        vnc_bridge(self.connection, self.client_address[0])

    def _stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        self.connection.settimeout(None)
        q = queue.Queue()
        ensure_inbox()
        with _local_lock:
            _local_subs.add(q)
        conn = self.connection
        try:
            # 21.4: one hello event, then ": ping" every SSE_PING_S; a client that
            # misses two pings reconnects. The hello is a named event so an
            # onmessage-only client ignores it.
            hello = json.dumps({"app": APP, "build": BUILD, "version": VERSION})
            self.wfile.write(b": connected\n\n" + f"event: hello\ndata: {hello}\n\n".encode())
            self.wfile.flush()
            next_ping = time.time() + SSE_PING_S
            while True:
                # an EventSource never writes after its request: a readable
                # socket is EOF (the tablet is gone) — stop within a second
                # instead of after two failed writes (review server-concurrency-1)
                if peer_gone(conn):
                    break
                try:
                    ev = q.get(timeout=max(0.05, min(1.0, next_ping - time.time())))
                except queue.Empty:
                    if time.time() >= next_ping:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        next_ping = time.time() + SSE_PING_S
                    continue
                ev.pop("_local", None)
                payload = json.dumps({"via": ev.get("via"), "time": ev.get("time", 0),
                                      "title": ev.get("title", ""),
                                      "message": ev.get("message", ""),
                                      "target": ev.get("target", ""),
                                      "from": ev.get("from", ""),
                                      "typed": bool(ev.get("typed")),
                                      **({"claim": True} if ev.get("claim") is True else {}),
                                      **({"mirror": True} if ev.get("mirror") else {}),
                                      **({"run": ev["run"]} if isinstance(ev.get("run"), dict) else {})})
                self.wfile.write(f"data: {payload}\n\n".encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with _local_lock:
                _local_subs.discard(q)

    def _read_body(self):
        """Section 3 body rules; returns the object or None after replying."""
        raw = self.headers.get("Content-Length")
        try:
            length = int(raw)
        except (TypeError, ValueError):
            self.close_connection = True   # the unread body must not become the next request line
            self._err(400, "bad length")
            return None
        if length < 0:
            self.close_connection = True
            self._err(400, "bad length")
            return None
        if length > BODY_LIMIT:
            self.close_connection = True
            self._err(413, "too large")
            return None
        try:
            body = json.loads(self.rfile.read(length))
        except Exception:
            self._err(400, "bad json")
            return None
        if not isinstance(body, dict):
            self._err(400, "bad json")
            return None
        return body

    def _refuse(self, status, reason):
        """A refusal before the body is read: a sane body is drained so the
        keep-alive connection stays usable for the next request; anything else
        closes it (review server-concurrency-2)."""
        try:
            n = int(self.headers.get("Content-Length"))
        except (TypeError, ValueError):
            n = -1
        if 0 <= n <= BODY_LIMIT:
            try:
                self.rfile.read(n)
            except OSError:
                self.close_connection = True
        else:
            self.close_connection = True
        self._err(status, reason)

    def do_POST(self):
        self._setcookie = False
        path, _ = self._qs()
        if "\x00" in self.path:
            self._refuse(400, "bad path")
            return
        if path == "/api/pair":                 # C4: a phone claims a code; no token yet, so no cookie/CSRF
            if self.server.scope != "phone":
                self._refuse(403, "phone only")
                return
            ip = self._fail_key("code")
            with self._FAIL_LOCK:
                _, until = self._fail.get(ip, (0, 0.0))
            if time.time() < until:
                self._refuse(429, "slow down")
                return
            body = self._read_body()
            if body is None:
                return
            status, reply = PAIR.claim(body.get("code"), body.get("name"))
            if status == 200:
                with self._FAIL_LOCK:   # the code proved this phone: its old-token lockout ends too
                    self._fail.pop(ip, None)
                    self._fail.pop(self._fail_key("token"), None)
                slog(f"paired a phone: {reply['name']} ({reply['id']})")
                HIST.append("device_paired", data={"id": reply["id"], "name": reply["name"]})
            else:
                self._fail_note(ip)
            self._json(reply, status)
            return
        if not self._host_ok():
            self._refuse(403, "bad host")
            return
        if not self._ok():
            self._refuse(403, "bad token")
            return
        if not self._csrf_ok():
            self._refuse(403, "csrf")
            return
        body = self._read_body()
        if body is None:
            return
        if path == "/api/send":
            text, target, frm = body.get("text", ""), body.get("target", ""), body.get("from", "")
            if not all(isinstance(v, str) for v in (text, target, frm)):
                self._err(400, "bad json")
                return
            if frm and not any(d["name"] == frm for d in desks()["desks"]):
                frm = re.sub(r"[^A-Za-z0-9_.-]", "", frm)[:40]   # an unlisted shell
            if not text.strip():
                self._err(400, "empty")
                return
            if frm and frm == target:
                self._err(400, "that is this desk")
                return
            kind = "handoff" if body.get("kind") == "handoff" else "message"
            if kind == "handoff":
                text = "HANDOFF: " + text
            ok, err, via, reach = deliver(target, text, frm, kind)
            # a desk waiting on an ask is a refusal (409), even when the message went to its lane
            code = 200 if ok else (409 if not via or err.startswith(WAITING_ERR) else 502)
            self._json({"ok": ok, "error": err, "via": via, "reach": reach}, code)
            return
        if path == "/api/alias":
            name, alias = body.get("name", ""), body.get("alias", "")
            if not isinstance(name, str) or not isinstance(alias, str):
                self._err(400, "bad json")
                return
            try:
                ok = set_alias(name, alias)
            except OSError as exc:
                self._err(502, f"cannot write aliases: {type(exc).__name__}".lower())
                return
            self._json({"ok": ok, "error": "" if ok else "no name"}, 200 if ok else 400)
            return
        if path == "/api/pane":
            target, action = body.get("target", ""), body.get("action", "")
            if not isinstance(target, str):
                self._err(400, "bad json")
                return
            if action == "arm":
                ok, err = arm_pane(target)
            elif action in ("start-claude", "start-cursor"):
                ok, err = start_cli(target, action[6:])
            elif action == "resume":
                ok, err, desk = resume_pane(target)
                self._json({"ok": ok, "error": err, **({"name": desk} if ok else {})}, 200 if ok else 409)
                return
            else:
                ok, err = False, "unknown action"
            self._json({"ok": ok, "error": err}, 200 if ok else 409)
            return
        if path == "/api/spawn":
            status, reply = mb.spawn_desk(MB, body, NAMES_DIR)
            if status == 200:
                invalidate()
                wslog(f"spawn desk={reply['name']} kind={body.get('kind')}")
            self._json(reply, status)
            return
        if path == "/api/pair/start":
            if self._desktop_only():
                status, reply = pair_start()
                self._json(reply, status)
            return
        if path == "/api/devices/revoke":
            if not self._desktop_only():
                return
            dev_id = body.get("id")
            if not isinstance(dev_id, str) or not DEVICES.revoke(dev_id):
                self._err(404, "no such device")
                return
            cut = cut_device(dev_id)
            slog(f"revoked device {dev_id}; {cut} connection(s) closed")
            HIST.append("device_revoked", data={"id": dev_id, "closed": cut})
            self._json({"ok": True})
            return
        if path == "/api/run":
            status, reply = mb.start_run(MB, body)
            if status == 200:
                invalidate()
                wslog(f"run desk={reply['name']} engine={body.get('engine')}")
            self._json(reply, status)
            return
        if path == "/api/kill":
            status, reply = kill_desk(body)
            self._json(reply, status)
            return
        if path == "/api/keys":
            status, reply = send_keys(body)
            self._json(reply, status)
            return
        if path == "/api/accounts":
            status, reply = mb.add_account(MB, body)
            self._json(reply, status)
            return
        if path == "/api/cli/read":
            status, reply = mb.cli_read(MB, body)
            self._json(reply, status)
            return
        if path == "/api/cli/desk":
            status, reply = mb.cli_desk(MB, body)
            if status == 200:
                invalidate()
                wslog(f"cli desk={reply['name']} id={body.get('id')}")
            self._json(reply, status)
            return
        if path == "/api/modules/new":
            status, reply = new_module(body)
            self._json(reply, status)
            return
        if path == "/api/modules/reload":
            global MODULES
            MODULES = load_modules()
            HIST.append("modules_reload", data={"modules": len(MODULES["modules"]),
                                                "errors": len(MODULES["errors"])})
            self._json({"ok": True, **MODULES})
            return
        if path == "/api/folders":                # B6: add or remove a project root
            status, reply = folders.edit(body, CFG, config_path(), raw_config(quiet=True), str(STATE),
                                         log=lambda kind, **data: HIST.append(kind, data=data))
            if status == 200:
                invalidate()
            self._json(reply, status)
            return
        if path == "/api/mb/verify":
            rid = body.get("id")
            if not catalog.is_safe_id(rid):
                self._err(400, "bad id")
                return
            status, reply = mb.verify(MB, rid)
            self._json(reply, status)
            return
        if path == "/api/sdk/verify":
            status, reply = mb.sdk_verify(MB, body)
            self._json(reply, status)
            return
        self._err(404, "not found")


def _int(raw, default, lo, hi):
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return default
    return max(lo, min(v, hi))


class Server(ThreadingHTTPServer):
    daemon_threads = True   # SSE/WS handler threads never block shutdown
    allow_reuse_address = True
    scope = "desktop"       # the desktop token opens it; loopback by default

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        if self.scope == "desktop":
            # 11-12 (D2): the desks it spawns reach it here — `metabot-app send`
            # reads METACHAT_URL from the desk env (create_session). The bound
            # port, never the config's wish (A6 takes a free one when it must)
            os.environ["METACHAT_URL"] = f"http://127.0.0.1:{self.server_address[1]}"


class TLSServer(Server):
    """C2: the listener phones pair with. TLS with the host's pinned certificate
    (C1), phone-scope device tokens only (C3). The handshake runs in the handler
    thread with a timeout, so a slow or plain-HTTP client never holds the
    accept loop; a failed handshake is dropped without a traceback."""
    scope = "phone"
    HANDSHAKE_S = 10

    def __init__(self, addr, handler, cert):
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        self.ctx.load_cert_chain(cert.cert, cert.key)
        self.fingerprint = cert.fingerprint
        super().__init__(addr, handler)
        _phone_ports.append(self.server_address[1])

    def get_request(self):
        sock, addr = self.socket.accept()
        return self.ctx.wrap_socket(sock, server_side=True, do_handshake_on_connect=False), addr

    def finish_request(self, request, client_address):
        try:
            request.settimeout(self.HANDSHAKE_S)
            request.do_handshake()
        except (OSError, ssl.SSLError):
            return
        super().finish_request(request, client_address)


def bind_address(addr, port, cert=None):
    """One bound listener (TLS when ``cert`` is given), or the OSError."""
    return TLSServer((addr, port), Handler, cert) if cert else Server((addr, port), Handler)


def serve_bound(srv, cert=None):
    if cert:
        slog(f"phone listener on {srv.server_address[0]}:{srv.server_address[1]} (fingerprint {cert.fingerprint[:16]}...)")
    else:
        slog(f"on {srv.server_address[0]}:{srv.server_address[1]} (token in {TOKEN_FILE})")
    srv.serve_forever()


def serve_address(addr, port, cert=None):
    """Bind one listener (TLS when ``cert`` is given); retried every 5 s in a thread when it fails."""
    while True:
        try:
            srv = bind_address(addr, port, cert)
        except OSError as exc:
            print(f"metachat: cannot bind {addr}:{port} ({exc}); retrying in 5 s", file=sys.stderr)
            time.sleep(5)
            continue
        serve_bound(srv, cert)
        return


# ---- replies in the chat (11-09): a Claude desk the app types into answers in its
# terminal; the PC reads that Claude's own transcript and posts each new assistant
# text block into the desk's lane. Armed desks answer through the inbox adapter instead.
MIRROR_EVERY = 1.5
MIRROR_TEXT_MAX = 8000
MIRROR_READ_MAX = 1 << 20
MIRROR_FRESH_S = 1800         # a session named on the command line counts while written this recently
MIRROR_SNAP_MAX_AGE = 10.0    # the mirror reuses the page's desk snapshot; it rebuilds one at most this often
_mirror = {}                  # desk -> {"path": str, "off": int}
_mirror_lock = threading.Lock()


def _proc_claude(pid, desk=None):
    """(argv, cwd, CLAUDE_CONFIG_DIR or None) of a running process; None when it is gone.
    With /proc: its cmdline, cwd and only CLAUDE_CONFIG_DIR from its environment.
    Without /proc (macOS): argv from the ps table, the folder from the desk's pane and
    the config folder from the desk's registry entry (A4)."""
    if procs.PROC.is_dir():
        try:
            argv = [a.decode("utf-8", "replace") for a in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0") if a]
            cwd = os.readlink(f"/proc/{pid}/cwd")
        except OSError:
            return None
        conf = None
        try:
            for kv in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0"):
                if kv.startswith(b"CLAUDE_CONFIG_DIR="):
                    conf = kv[len(b"CLAUDE_CONFIG_DIR="):].decode("utf-8", "replace") or None
        except OSError:
            pass
        return argv, cwd, conf
    argv = procs.argv(pid)
    if argv is None:
        return None
    cwd = _pane_cwd.get(desk, "") if desk else ""
    conf = None
    if desk:
        entry = mb.registry(MB).get(desk) or {}
        env_add = entry.get("env_add") if isinstance(entry.get("env_add"), dict) else {}
        conf = env_add.get("CLAUDE_CONFIG_DIR") or None
    return argv, cwd, conf


def claude_project_dir(base, cwd):
    """Where Claude Code keeps the transcripts of a folder: every non-alphanumeric character becomes '-'."""
    return Path(base) / "projects" / re.sub(r"[^A-Za-z0-9]", "-", cwd)


def desk_transcript(name, pid, saved, claimed):
    """The transcript a desk's running Claude writes: the session named on its command
    line (--session-id / --resume) or kept for the desk, while written in the last
    MIRROR_FRESH_S; else the newest transcript of its folder from the last day that no
    other desk has claimed. None when nothing fits."""
    pc = _proc_claude(pid, desk=name)
    if not pc:
        return None
    argv, cwd, conf = pc
    bases = [Path(conf)] if conf else [Path.home() / b for b in CLAUDE_HOMES]
    sid = None
    for i, a in enumerate(argv[:-1]):
        if a in ("--session-id", "--resume", "-r") and SID_RE.fullmatch(argv[i + 1]):
            sid = argv[i + 1]
    if not sid and name in saved:
        sid = saved[name][0]
    now = time.time()
    if sid:
        for base in bases:
            t = claude_project_dir(base, cwd) / f"{sid}.jsonl"
            try:
                if now - t.stat().st_mtime < MIRROR_FRESH_S:
                    return str(t)
            except OSError:
                continue
    best = None
    for base in bases:
        for t in claude_project_dir(base, cwd).glob("*.jsonl"):
            try:
                m = t.stat().st_mtime
            except OSError:
                continue
            if now - m < 86400 and str(t) not in claimed and (best is None or m > best[1]):
                best = (str(t), m)
    return best[0] if best else None


def _assistant_texts(raw):
    """The assistant text blocks in complete transcript lines (no tools, thinking or subagents)."""
    out = []
    for ln in raw.splitlines():
        try:
            row = json.loads(ln)
        except ValueError:
            continue
        if not isinstance(row, dict) or row.get("type") != "assistant" or row.get("isSidechain"):
            continue
        content = (row.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str):
                t = b["text"].strip()
                if t:
                    if len(t) > MIRROR_TEXT_MAX:
                        t = t[:MIRROR_TEXT_MAX].rstrip() + "\n\n… (cut here: the full answer in the terminal)"
                    out.append(t)
    return out


def mirror_once():
    """One pass: post new Claude answers of every unarmed pane desk into its lane."""
    with _SNAP_LOCK:
        data, age = _snap["data"], time.time() - _snap["t"]
    if data is None or age > MIRROR_SNAP_MAX_AGE:
        data = snapshot()
    rows = [d for d in data["rows"] if d.get("reach") == "pane" and not d.get("armed")]
    saved = saved_sessions(reg=data.get("reg"))
    with _mirror_lock:
        live, claimed = set(), set()
        todo = []
        for d in sorted(rows, key=lambda d: d["name"]):
            slug = desk_slug(d["name"])
            pid = _pane_cli_pid.get(slug)
            if not pid or data["panes"].get(slug) != "claude":
                continue
            todo.append((d["name"], pid))
        # desks whose session is on the command line claim their file first
        todo.sort(key=lambda x: 0 if any(a in ("--session-id", "--resume", "-r")
                                         for a in ((_proc_claude(x[1]) or ([],))[0])) else 1)
        for name, pid in todo:
            path = desk_transcript(name, pid, saved, claimed)
            if not path:
                continue
            claimed.add(path)
            live.add(name)
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            st = _mirror.get(name)
            if st is None or st["path"] != path or size < st["off"]:
                _mirror[name] = {"path": path, "off": size}   # first sight: nothing old is replayed
                continue
            if size == st["off"]:
                continue
            try:
                with open(path, "rb") as fh:
                    fh.seek(st["off"])
                    data_b = fh.read(MIRROR_READ_MAX)
            except OSError:
                continue
            end = data_b.rfind(b"\n")
            if end < 0:
                if len(data_b) >= MIRROR_READ_MAX:   # one line longer than a read: skip it
                    st["off"] += len(data_b)
                continue
            st["off"] += end + 1
            for text in _assistant_texts(data_b[:end + 1].decode("utf-8", "replace")):
                local_event(text, name, name, via="desk", typed=False, mirror=True)
        for gone in [n for n in _mirror if n not in live]:
            del _mirror[gone]


# A run started from the app tells its own lane how it ended: the answer (or why
# it failed) and a short receipt summary, once (11-09 tablet walk: the lane said
# only "run started"). Desks older than the window are left alone, so a deploy
# does not replay old runs.
RUN_REPORT_WINDOW = 3600
RUN_ROW_GRACE = 10.0   # a linked run whose desk has exited but has no row yet
_run_exit_seen = {}


def _run_summary(row, run_id):
    keys = ("status", "steps", "tool_calls", "tokens", "cost_musd", "cost_source", "engine", "model", "login")
    run = {"id": run_id, "receipt_state": row.get("state")}
    run.update({k: row.get(k) for k in keys})
    return run


KEY_REFUSED_RE = re.compile(r"\banswered (401|403)\b")


def _run_message(row):
    final = row.get("final") if isinstance(row.get("final"), str) else ""
    if row.get("status") not in (None, "ok"):
        why = row.get("error") if isinstance(row.get("error"), str) and row.get("error") else final or str(row.get("status"))
        text = "The run failed: " + why
        if KEY_REFUSED_RE.search(why):   # a hosted engine refused the key: say where to fix it
            text += " Check this engine's key in Settings → Accounts."
    else:
        text = final.strip() or "The run ended with no answer."
    return text if len(text) <= MIRROR_TEXT_MAX else text[:MIRROR_TEXT_MAX] + "…"


def run_report_once():
    """One pass over the app's recent, untold run desks."""
    reg = mb.registry(MB)
    now = time.time()
    todo = {n: e for n, e in reg.items() if e.get("kind") == "run" and not e.get("reported")
            and now - (e.get("created") or 0) < RUN_REPORT_WINDOW}
    for gone in [n for n in _run_exit_seen if n not in todo]:
        del _run_exit_seen[gone]
    if not todo:
        return
    unlinked = {n for n, e in todo.items() if not e.get("run_id")}
    if unlinked:
        for desk, rid in mb.live_run_desks(MB, unlinked, reg).items():
            if mb.reg_update(MB, desk, run_id=rid):
                todo[desk]["run_id"] = rid
    ends = {}
    for n, e in todo.items():
        code, rid = mb.run_pane_end(MB, n)
        if code is None:
            continue
        ends[n] = code
        if not e.get("run_id") and rid:
            if mb.reg_update(MB, n, run_id=rid):
                e["run_id"] = rid
    linked = {e["run_id"]: n for n, e in todo.items() if e.get("run_id")}
    rows = {}
    if linked:
        q = mb.mbq(MB, "runs")
        for row in (q.get("rows") if isinstance(q, dict) else None) or []:
            if isinstance(row, dict) and row.get("run_id") in linked:
                rows[row["run_id"]] = row
    for n, e in todo.items():
        rid = e.get("run_id")
        if rid and rid in rows:
            if mb.reg_update(MB, n, reported=True):
                local_event(_run_message(rows[rid]), n, n, via="desk", typed=False, mirror=True,
                            run=_run_summary(rows[rid], rid))
            continue
        if n not in ends:
            continue
        if rid:   # it exited, its run has no row yet: wait a little before saying so
            first = _run_exit_seen.setdefault(n, now)
            if now - first < RUN_ROW_GRACE:
                continue
        if mb.reg_update(MB, n, reported=True):
            where = "log" if mb.is_process_run(e) else "terminal"
            local_event(f"The run ended (exit {ends[n]}) with no receipt; its {where} says why.", n, "",
                        via="alert", typed=False)


def mirror_loop():
    while True:
        try:
            mirror_once()
        except Exception as exc:   # never take the server down over a reply mirror
            slog(f"mirror: {type(exc).__name__}: {exc}"[:200])
            time.sleep(10)
        try:
            run_report_once()
        except Exception as exc:   # nor over a run report
            slog(f"run report: {type(exc).__name__}: {exc}"[:200])
            time.sleep(10)
        try:
            mail_to_lane_once()
        except Exception as exc:   # nor over the person's mailbox
            slog(f"mail: {type(exc).__name__}: {exc}"[:200])
            time.sleep(10)
        time.sleep(MIRROR_EVERY)


def warm_reader():
    """Import MetaBot once in the warm worker and fill its receipt memo."""
    t0 = time.time()
    try:
        mb.mbq(MB, "live")
        mb.mbq(MB, "runs", timeout=60)
    except Exception as exc:   # never take the server down over a warm-up
        slog(f"warm reader: {type(exc).__name__}")
        return
    slog(f"warm reader ready in {time.time() - t0:.1f}s")


def main():
    """Start the server; 0 when it ran (or when one already runs on this state: A6)."""
    configure()
    inst = instance.Instance(STATE)
    if not inst.acquire():
        other = inst.running()
        if other:
            print(f"metabot-app: already running on 127.0.0.1:{other['port']} (pid {other['pid']})")
        else:
            print("metabot-app: another server is starting on this state folder")
        return 0
    slog(f"start build={BUILD} version={VERSION}")
    swept = mb.sweep_prompts(MB)
    if swept:
        slog(f"swept {swept} stale prompt file(s)")
    # speed: start the warm MetaBot reader now, so the first page load is not the cold one
    threading.Thread(target=warm_reader, daemon=True).start()
    threading.Thread(target=mirror_loop, daemon=True).start()
    binds = [a for a in CFG["bind"] if isinstance(a, str) and a] or ["127.0.0.1"]
    want = int(CFG["port"])
    # A6: the first address decides the port. A configured port taken by another program
    # is left alone: a free one is taken instead and recorded in STATE/server.json.
    try:
        first = bind_address(binds[0], want)
    except OSError as exc:
        if want == 0 or exc.errno != errno.EADDRINUSE:
            print(f"metabot-app: cannot bind {binds[0]}:{want} ({exc})", file=sys.stderr)
            inst.release()
            return 1
        print(f"metabot-app: port {want} is taken by another program; using a free one", file=sys.stderr)
        first = bind_address(binds[0], 0)
    port = first.server_address[1]
    inst.record(port, BUILD)
    threads = [threading.Thread(target=serve_bound, args=(first,), daemon=True)]
    threads[0].start()
    for addr in binds[1:]:
        th = threading.Thread(target=serve_address, args=(addr, port), daemon=True)
        th.start()
        threads.append(th)
    cert = phone_cert() if CFG["phone_listener"] else None
    for addr in [a for a in CFG["phone_bind"] if isinstance(a, str) and a] if cert else []:
        th = threading.Thread(target=serve_address, args=(addr, int(CFG["phone_port"]), cert), daemon=True)
        th.start()
        threads.append(th)

    def _term(*_):          # a service stop or the shell's close: end like Ctrl-C, releasing the record
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _term)
    try:
        while any(t.is_alive() for t in threads):
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        mb.close_reader()
        inst.release()
    return 0


if __name__ == "__main__":
    sys.exit(main())
