"""mb.py — MetaChat's MetaBot side: desks, runs, accounts, receipts.

Everything here is driven by a ``Ctx`` the server builds from its config, so
tests point it at temp dirs and fake binaries. No shell string ever carries
client text: tmux gets a fixed script and client values only as argv elements
after the word ``metachat`` (contract section 6).

The ``-u`` list (names unset for every spawned desk) is MetaBot's own rule,
copied here so this file imports nothing from MetaBot: ``DROPPED_ENV`` from
``metabot/engines/base.py`` plus every ``ANTHROPIC_*`` variable and the
sign-in ``CLAUDE_CODE_*`` names as ``ClaudeEngine.sign_in_variable`` in
``metabot/engines/claude.py`` decides them. ``tests/test_env_rules.py`` diffs
the copies against the MetaBot source tree.

A tmux session does not inherit MetaChat's environment: it gets the tmux
server's global environment (``tmux show-environment -g``), and ``bash -l``
then re-sources the profile (review SEC-3). So the rule is applied three
ways: ``unset_list()`` names every match in this process, in the tmux
server's global environment and in ``KNOWN_SIGN_IN``; and the fixed scripts
start with ``UNSET_PRELUDE``, which drops ``DROPPED_ENV`` and every
``ANTHROPIC_*`` / sign-in ``CLAUDE_CODE_*`` name again after the login shell
has read its files. The final ``exec bash -l`` re-reads the profile once
more, which nothing here can prevent; the owner's dotfiles export none of
these names today.

The receipt digest in a MetaBot index row is the field ``receipt_sha256``
(``metabot/receipts.py``, ``seal_and_store`` -> ``_append_index``): the sha256
hex of the receipt text as written, which is the file's bytes.
"""
import base64
import binascii
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import stat
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
from pathlib import Path

from . import catalog, paths, procs, tmuxcmd

# ---- MetaBot's env rule (copied; see the module docstring)
DROPPED_ENV = frozenset({
    "CLAUDECODE",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_PID",
    "CLAUDE_CODE_SESSION_ATTENDED",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_EXECPATH",
    "CLAUDE_EFFORT",
})
SIGN_IN_WORDS = frozenset({
    "KEY", "TOKEN", "AUTH", "OAUTH", "AUTHENTICATE", "CRED", "CREDS", "CREDMAN",
    "IDENTITY", "SECRET", "URL", "SOCKET", "PROVIDER", "HELPER",
})
NOT_A_PROVIDER = frozenset({
    "CLAUDE_CODE_USE_COWORK_PLUGINS",
    "CLAUDE_CODE_USE_POWERSHELL_TOOL",
    "CLAUDE_CODE_USE_NATIVE_FILE_SEARCH",
})
CURSOR_UNSET = ("CURSOR_API_KEY", "CURSOR_AUTH_TOKEN")
# Sign-in names the rule drops whether or not any environment here carries
# them now (every one satisfies sign_in_variable; a test checks that).
KNOWN_SIGN_IN = frozenset({
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_CUSTOM_HEADERS",
    "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_API_KEY_HELPER_TTL_MS",
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_SKIP_BEDROCK_AUTH", "CLAUDE_CODE_SKIP_VERTEX_AUTH", "CLAUDE_CODE_SKIP_FOUNDRY_AUTH",
})


def sign_in_variable(name):
    """ClaudeEngine.sign_in_variable, byte for byte in meaning."""
    if name.startswith("ANTHROPIC_"):
        return True
    if not name.startswith("CLAUDE_CODE_"):
        return False
    if name.startswith("CLAUDE_CODE_USE_"):
        return name not in NOT_A_PROVIDER
    if name.startswith("CLAUDE_CODE_SKIP_"):
        return True
    return not SIGN_IN_WORDS.isdisjoint(name.split("_")[2:])


def unset_names(environ=None, extra=()):
    """The sorted ``env -u`` list for a spawn: DROPPED_ENV, every sign-in name
    present in the server's environment, plus ``extra``."""
    environ = os.environ if environ is None else environ
    names = set(DROPPED_ENV) | {k for k in environ if sign_in_variable(k)} | set(extra)
    return sorted(names)


# ---- fixed scripts: client values are only ever "$@"
# UNSET_PRELUDE is sign_in_variable() in bash, run after the login shell has
# sourced its files: DROPPED_ENV by name; every ANTHROPIC_*; CLAUDE_CODE_USE_*
# unless NOT_A_PROVIDER; CLAUDE_CODE_SKIP_*; any other CLAUDE_CODE_* whose
# words after the prefix include a SIGN_IN_WORDS entry.
UNSET_PRELUDE = (
    "unset " + " ".join(sorted(DROPPED_ENV)) + "; "
    'for v in "${!ANTHROPIC_@}" "${!CLAUDE_CODE_@}"; do case "$v" in '
    + "|".join(sorted(NOT_A_PROVIDER)) + ") ;; "
    'ANTHROPIC_*|CLAUDE_CODE_USE_*|CLAUDE_CODE_SKIP_*) unset "$v";; '
    '*) case "_${v#CLAUDE_CODE_}_" in '
    + "|".join(f"*_{w}_*" for w in sorted(SIGN_IN_WORDS)) + ') unset "$v";; esac;; '
    "esac; done; "
)
FIXED_SCRIPT = UNSET_PRELUDE + ("printf '$'; printf ' %q' \"$@\"; echo; \"$@\"; s=$?; echo; "
                                "echo \"[metachat] exit $s\"; exec bash -l")
FIXED_RUN_SCRIPT = UNSET_PRELUDE + ("f=$1; shift; exec 3<\"$f\"; rm -f -- \"$f\"; printf '$'; "
                                    "printf ' %q' \"$@\"; echo ' - <prompt>'; \"$@\" <&3; s=$?; "
                                    "exec 3<&-; echo; echo \"[metachat] exit $s\"; exec bash -l")
# A finished catalogue desk that comes back (its session was closed): the argv is
# shown as a note and never run again (review server-flows-2); a shell follows.
FIXED_NOTE_SCRIPT = UNSET_PRELUDE + ("printf '$'; printf ' %q' \"$@\"; "
                                     "echo ' (finished earlier; not run again)'; exec bash -l")

KIND_ARGV = {"claude": ["claude"], "cursor": ["cursor-agent"], "shell": ["bash", "-l"]}
# 25.3 B1: a sign-in desk runs the CLI's own sign-in on one account, in
# <state>/signin (0700, kept empty). Its row says which screen the CLI shows,
# read from the last SIGNIN_LINES pane lines; the patterns are case-sensitive
# ("Not logged in" is not "Logged in") and listed in the order a sign-in walks
# them, so on a tie (two on one line) the later screen wins.
SIGNIN_ARGV = {"claude": ["claude"], "cursor": ["cursor-agent", "login"]}
SIGNIN_LINES = 40
# Follow-up C: FIXED_SCRIPT's line once the sign-in CLI has ended (bash's status
# is 0-255); a step of every engine, so it wins when it is the latest match.
SIGNIN_ENDED_RE = re.compile(r"^\[metachat\] exit (\d{1,3})\s*$")
SIGNIN_STEPS = {
    "claude": (("style", re.compile(r"Choose the text style")),
               ("method", re.compile(r"Select login method")),
               ("code", re.compile(r"Paste code here if prompted")),
               ("success", re.compile(r"Login successful|Logged in as")),
               ("notes", re.compile(r"Security notes|Press Enter to continue")),
               ("trust", re.compile(r"trust the files in this folder|Yes, I trust this folder")),
               ("ended", SIGNIN_ENDED_RE)),
    "cursor": (("link", re.compile(r"Waiting for browser authentication|loginDeepControl")),
               ("success", re.compile(r"Logged in|Login successful|Authentication successful")),
               ("ended", SIGNIN_ENDED_RE)),
}
# the steps where the CLI waits for the person (a key, a code, the browser);
# cursor-agent exits after "Logged in", so its success waits for nothing
SIGNIN_WAITING = {"claude": frozenset({"style", "method", "code", "success", "notes", "trust"}),
                  "cursor": frozenset({"link"})}
SIGNIN_HOSTS = frozenset({"claude.com", "claude.ai", "platform.claude.com", "console.anthropic.com",
                          "cursor.com"})
SIGNIN_URL_MAX = 4096
URL_CHARS_RE = re.compile(r"[A-Za-z0-9%._~:/?#\[\]@!$&'()*+,;=-]+")
# MetaBot 0.4.0 logins.SIGN_IN_FILES: the file a signed-in login home holds
SIGN_IN_FILES = {"claude": ".credentials.json", "codex": "auth.json"}
CURSOR_AUTH = ("config", "cursor", "auth.json")   # under a Cursor folder (XDG_CONFIG_HOME=<folder>/config)
LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
ACCOUNT_RE = re.compile(r"^[a-z][a-z0-9-]{0,23}$")
ENGINE_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
MAX_COST_RE = re.compile(r"^[0-9]{1,5}(\.[0-9]{1,2})?$")
# a model name as engines spell them: letters, digits, . _ : / + - and bracket overrides; never a flag
MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+\[\]=,-]{0,79}")
TASK_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
OUT_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
VERSION_RE = re.compile(r"(\d+\.\d+\.\d+[0-9A-Za-z.+-]*)")
# Section 21.4: what a waiting prompt looks like on the last pane lines, per
# CLI: MetaBot ("allow? type", "type 'yes'", "(y/N)", "install it? [y/N]"),
# Claude Code ("Do you want to proceed", "Allow ...?", "Esc to cancel"),
# cursor-agent ("Run this command", "[y/N]"), codex ("Allow command"), sudo
# ("[sudo] password"); 25.3 B5 adds the sign-in screens of Claude Code 2.1.319
# and cursor-agent 2026.11.05.
NEEDS_RE = re.compile(
    r"do you want to proceed|allow\? type|type 'yes'|\(y/n\)|\[y/n\]|esc to cancel"
    r"|run this command|allow command|\[sudo\] password|\ballow\b[^\n]*\?"
    r"|select login method|paste code here|press enter to continue|choose the text style"
    r"|waiting for browser authentication", re.I)
SHELL_PROMPT_RE = re.compile(r"[$#]\s*$")      # a bare shell again: nothing waits
COMMAND_LINE_RE = re.compile(r"^\s*(?:\S*[$#]|>)\s")   # "$ cmd", "user@host:~$ cmd", "> cmd": typed text, not a dialog
NEEDS_LINES = 6
# the class of a waiting ask is read off the whole captured tail (review 1.2,
# 11-09): a Claude Code 2.1.319 Bash dialog puts its command 7 non-empty lines
# above the footer. A class read too high only takes the one-tap keys away.
CLASS_LINES = 40
# a line that is only key hints ("Esc to cancel · Tab to amend") is never the ask line
HINT_ONLY_RE = re.compile(r"^\s*(?:(?:esc|enter|tab|space|ctrl\+\w+|shift\+\w+|↑/↓|↑↓)\s+to\s+\w+(?:\s+\w+)?"
                          r"\s*(?:[·•|]\s*)?)+$", re.I)
# the class chip of a waiting ask (22.3): destructive > write > read, from the same lines
DESTRUCTIVE_RE = re.compile(r"\brm\s+-[a-z]*r|\bdrop\b|\btruncate\b|\bdelete\b|\bdestructive\b"
                            r"|\bforce\b|--hard\b|\bwipe\b|\bformat\b", re.I)
WRITE_RE = re.compile(r"\b(write|edit|create|bash|run this command|allow command|mkdir|mv|cp|chmod|"
                      r"install|commit|push|checkout|reset|sudo)\b", re.I)
# Follow-up A: what kind of answer a waiting desk wants, read off the line it
# asks on: the latest of its last NEEDS_LINES lines that asks (NEEDS_RE or
# MENU_RE, or the last line when one of ASK_RES matches it), so an older ask
# still on the screen loses to the newer one. The first of ASK_RES that matches
# that line wins; a line none matches reads as a menu. A sign-in desk that waits
# asks "signin" before all of them. secret: a password, a passphrase or a PIN
# (sudo, su, ssh); yes: MetaBot 0.4.0's destructive ask (approvals.py
# TtyApprover: "allow? type 'yes' to allow, anything else declines: "); line: a
# y/n, or MetaBot's other ask ("allow? type 'y' ..."); select: a Claude Code or
# cursor-agent menu.
SECRET_RE = re.compile(r"\[sudo\] password|password(?: for [^:]*)?:|passphrase|enter pin", re.I)
ASK_RES = (("secret", SECRET_RE),
           # 11-11 (found on the tablet): a line the person types a sentence into (Cursor's
           # "Tell the agent what to do instead (Enter to send ...)"); chat answers it, see
           # server.waiting_refusal. Only the entry line: the menu that offers it ("Skip & tell
           # the agent what to do instead") is still a menu, and typed words there pick an option.
           ("text", re.compile(r"enter to send", re.I)),
           ("yes", re.compile(r"type 'yes'|type yes", re.I)),
           ("line", re.compile(r"\[y/n\]|\(y/n\)|allow\? type", re.I)),
           ("select", re.compile(r"esc to cancel|enter to confirm|do you want to proceed"
                                 r"|^\s*❯\s*\d+\.", re.I)))
# a menu NEEDS_RE has no words for (its confirm line, or the option under its
# pointer) also waits, and so does a secret prompt on the line the cursor waits on
MENU_RE = re.compile(r"enter to confirm|^\s*❯\s*\d+\.", re.I)
ASK_LINE_MAX = 200
ASK_CTRL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
OUT_CAP = 64 * 1024
TASK_LIMIT = 20000
RAW_LIMIT = 60000
PROMPT_MAX_AGE = 3600
CLAUDE_SEED = ("settings.json", "CLAUDE.md", "plugins", "skills")


class Ctx:
    """What mb.py needs from the server: config values and the state dir."""

    def __init__(self, cfg, state, hist=None):
        self.cfg = cfg
        self.state = Path(state)
        self.hist = hist
        self.read_timeout = 60
        self.spawn_times = []
        self.lock = threading.Lock()
        self.spawn_lock = threading.Lock()   # caps, name allocation and creation, as one step
        self._needs = {}
        self._needs_since = {}
        self._panes = [0.0, {}]
        self._versions = None
        self._unit = [0.0, None]
        self._extend = [0.0, None]
        self.extend_lock = threading.Lock()
        self.here = Path(__file__).resolve().parent

    @property
    def metabot(self):
        return paths.resolve(self.cfg.get("metabot_bin"), "metabot")

    @property
    def orcha(self):
        return paths.resolve(self.cfg.get("orcha_bin"), "orcha")

    @property
    def metabot_python(self):
        """The interpreter mbq.py runs under: config ``metabot_python``, else the app's own."""
        return paths.resolve(self.cfg.get("metabot_python"), None, fallback=sys.executable)

    @property
    def metabot_home(self):
        raw = self.cfg.get("metabot_home") or os.environ.get("METABOT_HOME")
        return str(Path(raw).expanduser().absolute()) if raw else str(Path.home() / ".metabot")

    def catalog_ctx(self):
        return {"metabot": self.metabot, "orcha": self.orcha, "metabot_home": self.metabot_home,
                "project_roots": self.cfg.get("project_roots") or [],
                "state_dir": str(self.state),
                "also_ok": [str(self.state / "modules")]}   # 25.3 B8: a desk may open there

    def desks_dir(self):
        d = self.state / "desks"
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
        return d

    def prompts_dir(self):
        d = self.state / "prompts"
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
        return d

    def runs_dir(self):
        """Logs of the runs the app spawned itself (no tmux / on the phone): 0700."""
        d = self.state / "runs"
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
        return d

    def accounts_dir(self, engine):
        d = self.state / "accounts" / engine
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
        return d

    def signin_dir(self):
        """25.3 B1: the folder every sign-in desk opens in: 0700, nothing is
        ever written to it (Claude asks to trust it; there is nothing to trust)."""
        d = self.state / "signin"
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(d, 0o700)
        return d

    def log(self, event, **kw):
        if self.hist is not None:
            try:
                self.hist.append(event, desk=kw.pop("desk", None), run_id=kw.pop("run_id", None),
                                 data=kw)
            except Exception:
                pass


# ---------------------------------------------------------------- tmux

def tmux_arg(a):
    """One tmux argument as tmux 3.4 must see it: an argument whose last
    character is ";" ends a tmux command there (even after ``--``) and the
    words after it run as a second one (``run-shell`` runs a shell command),
    so that ";" goes as "\\;", which tmux turns back into ";" and keeps in the
    argument. Review 1.2 (11-09): a catalogue value, a folder or a model name
    is client text."""
    return a[:-1] + "\\;" if isinstance(a, str) and a.endswith(";") else a


def tmux(ctx, *argv, timeout=5, inp=None):
    """Run one tmux command; (rc, stdout, stderr). Never raises. Every
    argument goes through tmux_arg."""
    try:
        r = subprocess.run(tmuxcmd.argv(*map(tmux_arg, argv)), capture_output=True, text=True,
                           timeout=timeout, input=inp)
        return r.returncode, r.stdout, (r.stderr or "").strip()
    except (OSError, subprocess.SubprocessError) as e:
        return 255, "", str(e)


def run_captured(argv, timeout, inp=None, **kw):
    """``subprocess.run(capture_output=True)`` in its own session: on a timeout
    the whole process group is killed, so a hung grandchild (a network check,
    an engine probe) dies with the command instead of running on as an orphan
    holding the pipes and MetaBot's locks (review server-concurrency-6).
    Raises subprocess.TimeoutExpired after the kill; OSError when it cannot start."""
    p = subprocess.Popen(argv, stdin=subprocess.PIPE if inp is not None else subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True, **kw)
    try:
        out, err = p.communicate(inp, timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            p.communicate(timeout=5)
        except (subprocess.TimeoutExpired, OSError, ValueError):
            pass
        raise
    return subprocess.CompletedProcess(argv, p.returncode, out, err)


def sessions(ctx):
    rc, out, _ = tmux(ctx, "list-sessions", "-F", "#S")
    return set(out.split()) if rc == 0 else set()


def has_session(ctx, name):
    rc, _, _ = tmux(ctx, "has-session", "-t", f"=fc-{name}")
    return rc == 0


def pane_pids(ctx):
    """{desk: pane_pid} for every fc-* session, cached 5 s (one list-panes
    call instead of a display per desk)."""
    now = time.time()
    with ctx.lock:
        if now - ctx._panes[0] < 5.0:
            return dict(ctx._panes[1])
    rc, out, _ = tmux(ctx, "list-panes", "-a", "-F", "#S #{pane_pid}")
    found = {}
    if rc == 0:
        for line in out.splitlines():
            f = line.split()
            if len(f) == 2 and f[0].startswith("fc-") and f[1].isdigit():
                found[f[0][3:]] = int(f[1])
    with ctx.lock:
        ctx._panes = [now, found]
    return dict(found)


def tmux_global_env(ctx):
    """Names in the tmux server's global environment (what a new session
    inherits). ``NAME=value`` lines; a ``-NAME`` line is a removed name."""
    rc, out, _ = tmux(ctx, "show-environment", "-g")
    names = set()
    if rc == 0:
        for line in out.splitlines():
            if line and not line.startswith("-") and "=" in line:
                names.add(line.split("=", 1)[0])
    return names


def unset_list(ctx, extra=()):
    """The sorted ``env -u`` list for a spawn: MetaBot's rule over this
    process's environment, the tmux server's global environment and the
    known sign-in names, plus ``extra`` (review SEC-3)."""
    merged = dict.fromkeys(os.environ, "")
    merged.update(dict.fromkeys(tmux_global_env(ctx), ""))
    merged.update(dict.fromkeys(KNOWN_SIGN_IN, ""))
    return unset_names(merged, extra)


def spawn_argv(name, cwd, home, argv, extra_env=None, extra_unset=(), script=FIXED_SCRIPT,
               environ=None, names=None):
    """The exact tmux argv of section 6.1. The session is named with ``-s
    fc-<name>`` (tmux 3.4 keeps a literal ``=`` in a new-session name; ``=``
    is target syntax and every later ``-t`` uses ``=fc-<name>``). ``names`` is
    the finished ``-u`` list; without it the rule runs over ``environ``."""
    cmd = ["new-session", "-d", "-s", f"fc-{name}", "-c", str(cwd), "-e", f"METABOT_HOME={home}",
           "-e", f"METABOT_MAILBOX={name}"]   # 11-11: the desk's mailbox (metabot mail), for its hooks
    for k, v in (extra_env or {}).items():
        cmd += ["-e", f"{k}={v}"]
    cmd += ["--", "env"]
    for n in (names if names is not None else unset_names(environ, extra_unset)):
        cmd += ["-u", n]
    cmd += ["bash", "-lc", script, "metachat", *argv]
    return cmd


def create_session(ctx, name, cwd, argv, extra_env=None, extra_unset=(), script=FIXED_SCRIPT):
    rc, _, err = tmux(ctx, *spawn_argv(name, cwd, ctx.metabot_home, argv, extra_env,
                                       extra_unset, script, names=unset_list(ctx, extra_unset)),
                      timeout=15)
    if rc != 0:
        return False, err or "tmux new-session failed"
    for opt in (("status", "off"), ("mouse", "off")):
        tmux(ctx, "set-option", "-t", f"=fc-{name}", *opt)
    return True, ""


# ---------------------------------------------------------------- runs without tmux (T2/E3)
# On a host with no tmux, and on the phone, a run is the app's own child process: the
# prompt file on stdin, stdout and stderr into <state>/runs/<name>.log, the environment
# scrubbed with the same names a desk drops, its own session so one killpg ends the tree.
# The registry entry carries ``pid`` and ``log``; ``exit`` once it has ended. Journeys stay
# index-based, so nothing else changes for them.

_PROCS = {}            # (state dir, desk) -> Popen, for this server's lifetime
_PROCS_LOCK = threading.Lock()
RUNLOG_TAIL = 4000
RUNLOG_READ = 65536
INTERRUPTED_EXIT = 255
DESK_NAME_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")


def on_device():
    """The server runs inside the phone app (stream E): the runtime says so, or Python does."""
    return os.environ.get("METABOT_ON_DEVICE") == "1" or hasattr(sys, "getandroidapilevel")


def run_in_process(ctx):
    """A run is the app's own child when nothing can hold a desk for it."""
    return on_device() or not tmuxcmd.found()


def pid_alive(pid):
    """A process with this pid that has not exited (a zombie counts as gone)."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            state = f.read().rpartition(b")")[2].split()
    except OSError:
        return True
    return not state or state[0] not in (b"Z", b"X")


def _proc_key(ctx, name):
    return (str(ctx.state), name)


def spawn_run_process(ctx, name, cwd, argv, prompt, extra_env=None, extra_unset=()):
    """Start ``argv`` as the app's own child. The prompt file is its stdin and is
    unlinked once the child holds the descriptor (as the desk script's ``exec 3<``
    then ``rm`` does); stdout and stderr go to the run's log (0600). ``extra_env``
    and ``extra_unset`` are an account's additions, as for a desk. Returns the
    Popen; raises OSError when it cannot start."""
    drop = set(unset_list(ctx, extra_unset))
    env = {k: v for k, v in os.environ.items() if k not in drop}
    env["METABOT_HOME"] = ctx.metabot_home
    env.update(extra_env or {})
    log = ctx.runs_dir() / f"{name}.log"
    fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as out, open(prompt, "rb") as stdin:
        p = subprocess.Popen(argv, cwd=cwd, stdin=stdin, stdout=out, stderr=subprocess.STDOUT,
                             env=env, start_new_session=True, close_fds=True)
    with _PROCS_LOCK:
        _PROCS[_proc_key(ctx, name)] = p
    return p


def _log_path(ctx, entry):
    return ctx.runs_dir() / str(entry.get("log") or "")


def _log_tail(ctx, entry, limit=RUNLOG_READ):
    try:
        with open(_log_path(ctx, entry), "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - limit))
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


def _log_append(ctx, entry, text):
    try:
        with open(_log_path(ctx, entry), "ab") as f:
            f.write(text.encode("utf-8"))
    except OSError:
        pass


def run_proc_end(ctx, name, entry):
    """run_pane_end for a run the app spawned itself: (exit code, run id or None)
    once it has ended, (None, None) while it runs. The exit line is written into
    the log as the desk script writes it into the pane; a run whose handle this
    server never held (the app restarted mid-run) and whose pid is gone ended
    interrupted."""
    code = entry.get("exit")
    if code is None:
        with _PROCS_LOCK:
            p = _PROCS.get(_proc_key(ctx, name))
        if p is not None:
            code = p.poll()
            if code is None:
                return None, None
            with _PROCS_LOCK:
                _PROCS.pop(_proc_key(ctx, name), None)
            code = code if code >= 0 else 128 - code
            _log_append(ctx, entry, f"\n[metachat] exit {code}\n")
        elif pid_alive(entry.get("pid")):
            return None, None
        elif RUN_RECEIPT_LINE_RE.search(_log_tail(ctx, entry)):
            code = 0   # it ended while the app was away, and left its receipt
            _log_append(ctx, entry, f"\n[metachat] exit {code}\n")
        else:
            code = INTERRUPTED_EXIT
            _log_append(ctx, entry, f"\n[metachat] exit {code} (interrupted: the app stopped mid-run)\n")
        reg_update(ctx, name, exit=code)
    ids = RUN_RECEIPT_LINE_RE.findall(_log_tail(ctx, entry))
    return code, (ids[-1] if ids else None)


def reap_run_processes():
    """Poll every run handle this server holds; drop the finished ones (their exit
    is recorded by run_proc_end when the run is next looked at). Returns the names
    still running."""
    running = []
    with _PROCS_LOCK:
        for key, p in list(_PROCS.items()):
            if p.poll() is None:
                running.append(key[1])
    return running


def is_process_run(entry):
    return isinstance(entry, dict) and isinstance(entry.get("pid"), int)


def live_process_runs(reg):
    """Names of the registry's runs that are the app's own live children."""
    reap_run_processes()
    return {n for n, d in reg.items() if d.get("kind") == "run" and is_process_run(d)
            and d.get("exit") is None and pid_alive(d["pid"])}


def desk_alive(ctx, name, entry=None):
    """A desk is alive while its tmux session exists or its own child process runs."""
    if entry is None:
        entry = reg_read(ctx, name)
    if is_process_run(entry):
        return pid_alive(entry["pid"])
    return has_session(ctx, name)


def kill_run_process(ctx, name, entry, grace=1.5):
    """End a run the app spawned: SIGTERM to its process group, SIGKILL after
    ``grace`` seconds; the registry entry is removed. (ok, err)."""
    pid = entry.get("pid")
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pid, sig)
        except ProcessLookupError:
            break
        except OSError:
            pass
        end = time.time() + grace
        while time.time() < end and pid_alive(pid):
            time.sleep(0.05)
        if not pid_alive(pid):
            break
    with _PROCS_LOCK:
        p = _PROCS.pop(_proc_key(ctx, name), None)
    if p is not None:
        try:
            p.wait(timeout=1)
        except (subprocess.TimeoutExpired, OSError):
            pass
    reg_delete(ctx, name)
    return not pid_alive(pid), "" if not pid_alive(pid) else "the run did not stop"


def kill_desk_any(ctx, name, entry=None):
    """kill_session for a desk, or kill_run_process for the app's own child."""
    if entry is None:
        entry = reg_read(ctx, name)
    if is_process_run(entry):
        return kill_run_process(ctx, name, entry)
    return kill_session(ctx, name)


def runlog(ctx, name):
    """GET /api/mb/runlog/<name> -> (status, reply): the tail of the log of a run
    the app spawned itself, with its pid, whether it still runs and its exit."""
    if not isinstance(name, str) or not DESK_NAME_RE.fullmatch(name):
        return 400, {"ok": False, "error": "bad name"}
    entry = reg_read(ctx, name)
    if not is_process_run(entry) or not entry.get("log"):
        return 404, {"ok": False, "error": "no such run"}
    code, _ = run_proc_end(ctx, name, entry)
    tail = _log_tail(ctx, entry)[-RUNLOG_TAIL:]
    return 200, {"ok": True, "name": name, "pid": entry["pid"], "alive": pid_alive(entry["pid"]),
                 "exit": code, "log": tail}


# ---------------------------------------------------------------- registry

def reg_read(ctx, name):
    try:
        return json.loads((ctx.desks_dir() / f"{name}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def registry(ctx):
    out = {}
    for f in sorted(ctx.desks_dir().glob("*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(d, dict) and d.get("name") == f.stem:
            out[f.stem] = d
    return out


def reg_write(ctx, entry):
    p = ctx.desks_dir() / f"{entry['name']}.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(entry) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)


def reg_delete(ctx, name):
    try:
        (ctx.desks_dir() / f"{name}.json").unlink()
    except OSError:
        pass


def reg_update(ctx, name, **fields):
    """Set fields on a desk's registry entry if it still exists (a desk killed
    meanwhile is not brought back). True when written."""
    with ctx.spawn_lock:
        entry = reg_read(ctx, name)
        if not isinstance(entry, dict) or entry.get("name") != name:
            return False
        entry.update(fields)
        reg_write(ctx, entry)
        return True


def taken_names(ctx):
    names = {s[3:] for s in sessions(ctx) if s.startswith("fc-")}
    return names | set(registry(ctx))


def alloc_name(base, taken):
    """As today: <base>, then <base>-2, <base>-3, ..."""
    name, n = base, 1
    while name in taken:
        n += 1
        name = f"{base}-{n}"
    return name


def alloc_run_name(taken):
    n = 1
    while f"run-{n}" in taken:
        n += 1
    return f"run-{n}"


def cap_check(ctx, kind):
    """Caps of 4.6 / 4.7: registry size, spawns per minute, live run desks."""
    reg = registry(ctx)
    if kind == "run":
        # a run desk whose run has ended (its pane shows the exit line) is a shell, not a
        # run: it holds no slot, or six finished runs would stop the seventh from starting
        live = sessions(ctx)
        procs_live = live_process_runs(reg)
        running = sum(1 for n, d in reg.items() if d.get("kind") == "run"
                      and (n in procs_live or (f"fc-{n}" in live and not is_process_run(d)
                                                and run_pane_end(ctx, n)[0] is None)))
        if running >= int(ctx.cfg["max_runs"]):
            return "cap"
    if len(reg) >= int(ctx.cfg["max_desks"]):
        return "cap"
    now = time.time()
    with ctx.lock:
        ctx.spawn_times = [t for t in ctx.spawn_times if now - t < 60]
        if len(ctx.spawn_times) >= int(ctx.cfg["spawn_per_minute"]):
            return "cap"
    return None


def note_spawn(ctx):
    with ctx.lock:
        ctx.spawn_times.append(time.time())


# ---------------------------------------------------------------- projects, cwd

def project_of(ctx, cwd):
    if not cwd:
        return "other"
    for p in ctx.cfg.get("projects") or []:
        for path in p.get("paths") or []:
            root = os.path.expanduser(str(path)).rstrip("/")
            if cwd == root or cwd.startswith(root + "/"):
                return str(p.get("name") or "other")
    return "other"


def cwd_ok(ctx, raw):
    """Section 2 rules for a tablet-chosen cwd; the resolved path or None."""
    p = catalog.path_ok(raw, ctx.catalog_ctx())
    if p is None or not os.path.isdir(p):
        return None
    return p


# ---------------------------------------------------------------- spawn, run, cli

def _label(body, key="label"):
    raw = body.get(key)
    if raw in (None, ""):
        return None, None
    if not isinstance(raw, str) or not LABEL_RE.fullmatch(raw):
        return None, "bad label"
    return raw, None


LAST_FOLDER = "last-folder"
LAST_FOLDER_MAX = 4096


def last_folder(ctx):
    """The folder the last claude, cursor or shell desk opened in
    (``<state>/last-folder``), when it still passes section 2's rules; else
    None. The file is never followed through a link and never read past
    LAST_FOLDER_MAX bytes."""
    try:
        fd = os.open(ctx.state / LAST_FOLDER, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        raw = os.read(fd, LAST_FOLDER_MAX + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    if len(raw) > LAST_FOLDER_MAX:
        return None
    try:
        text = raw.decode("utf-8").strip("\n")
    except UnicodeDecodeError:
        return None
    return _project_folder(ctx, text) if text and "\n" not in text else None


def _project_folder(ctx, raw):
    """cwd_ok, less MetaChat's own folders (the modules folder passes cwd_ok so
    a desk can open there; it is never where the next agent desk should open)."""
    p = cwd_ok(ctx, raw)
    if p is None:
        return None
    try:
        own = ctx.state.resolve()
    except (OSError, RuntimeError):
        return None
    return None if Path(p) == own or own in Path(p).parents else p


def note_folder(ctx, cwd):
    """Keep ``cwd`` as the last folder used (0600, replaced whole), when it passes
    section 2's rules (a shell in home leaves the last one as it was)."""
    p = _project_folder(ctx, cwd) if isinstance(cwd, str) else None
    if p is None:
        return
    path = ctx.state / LAST_FOLDER
    tmp = ctx.state / f".{LAST_FOLDER}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(p.encode("utf-8") + b"\n")
        os.replace(tmp, path)
    except (OSError, UnicodeEncodeError):
        try:
            os.unlink(tmp)
        except OSError:
            pass


def default_folder(ctx):
    """25.3 B4: where a claude or cursor desk opens when the tablet names no
    folder: config ``default_folder`` when it passes section 2's rules, else
    the last folder a claude, cursor or shell desk opened in (last_folder),
    else the first ``projects[*].paths[*]`` that
    passes; None when none does."""
    raw = ctx.cfg.get("default_folder")
    if isinstance(raw, str) and raw:
        p = cwd_ok(ctx, os.path.expanduser(raw))
        if p:
            return p
    p = last_folder(ctx)
    if p:
        return p
    for proj in ctx.cfg.get("projects") or []:
        if isinstance(proj, dict) and isinstance(proj.get("paths"), list):
            for raw in proj["paths"]:
                if isinstance(raw, str) and raw:
                    p = cwd_ok(ctx, os.path.expanduser(raw))
                    if p:
                        return p
    return None


def _cwd_from(ctx, body, kind=None):
    """The cwd a body names (section 2 rules). Without one: a claude or cursor
    desk opens in the default folder (25.3 B4), anything else in home."""
    raw = body.get("cwd")
    if raw in (None, ""):
        if kind in ("claude", "cursor"):
            p = default_folder(ctx)
            return (p, None) if p else (None, "choose a project folder")
        return str(Path.home()), None
    p = cwd_ok(ctx, raw)
    return (p, None) if p else (None, "bad cwd")


def account_env(ctx, kind, account, acc=None):
    """Section 6.2: the ``-e`` pairs and extra ``-u`` names an account adds.
    Returns (extra_env, extra_unset, account, (status, error) or None).
    ``acc`` is an accounts() reply the caller already has."""
    if account is None:
        return {}, (), None, None
    if not isinstance(account, str) or not ACCOUNT_RE.fullmatch(account):
        return {}, (), None, (400, "bad account")
    acc = accounts(ctx) if acc is None else acc
    if kind == "claude":
        match = [a for a in acc["claude"] if a["name"] == account]
        if not match:
            return {}, (), None, (404, "no such account")
        if match[0].get("engine") != "claude":   # follow-up D: a codex login's folder is not Claude's
            return {}, (), None, (400, "not a claude login")
        return {"CLAUDE_CONFIG_DIR": match[0]["home"]}, (), account, None
    if kind == "cursor":
        match = [a for a in acc["cursor"] if a["name"] == account]
        if not match:
            return {}, (), None, (404, "no such account")
        home = str(Path.home())
        return ({"XDG_CONFIG_HOME": match[0]["folder"] + "/config",
                 "GH_CONFIG_DIR": home + "/.config/gh",
                 "CURSOR_CONFIG_DIR": home + "/.cursor"}, CURSOR_UNSET, account, None)
    return {}, (), None, None   # a shell has no account


def spawn_desk(ctx, body, names_dir=None, extra_argv=(), session_id=None, env_add=None):
    """POST /api/spawn -> (status, reply). ``extra_argv`` (server-made, never
    client text) follows the kind's argv: resume passes ``--resume <id>`` with
    its ``session_id`` (and ``env_add``: the config folder that session lives
    in). A plain claude desk gets ``--session-id <new uuid>``, recorded in the
    registry, so the desk can be resumed once its Claude exits (11-09)."""
    signin = body.get("signin")
    if signin is not None and not isinstance(signin, bool):
        return 400, {"ok": False, "error": "bad signin"}
    if signin:
        return spawn_signin(ctx, body)
    kind = body.get("kind")
    if kind not in KIND_ARGV:
        return 400, {"ok": False, "error": "bad kind"}
    label, err = _label(body)
    if err:
        return 400, {"ok": False, "error": err}
    cwd, err = _cwd_from(ctx, body, kind)
    if err:
        return 400, {"ok": False, "error": err}
    extra_env, extra_unset, account, bad = account_env(ctx, kind, body.get("account") or None)
    if bad:
        return bad[0], {"ok": False, "error": bad[1]}
    argv = list(KIND_ARGV[kind]) + list(extra_argv)
    if kind == "claude" and not extra_argv:
        session_id = str(uuid.uuid4())
        argv += ["--session-id", session_id]
    if env_add:
        extra_env = {**extra_env, **env_add}
    with ctx.spawn_lock:   # SEC-12: caps, the name and the session are one step
        cap = cap_check(ctx, kind)
        if cap:
            return 409, {"ok": False, "error": cap}
        taken = taken_names(ctx)
        if label:
            if label in taken:
                return 409, {"ok": False, "error": "name taken"}
            name = label
        else:
            name = alloc_name(kind, taken)
        ok, err = create_session(ctx, name, cwd, argv, extra_env, extra_unset)
        if not ok:
            return 502, {"ok": False, "error": err}
        note_spawn(ctx)
        reg_write(ctx, {"name": name, "kind": kind, "account": account, "engine": None,
                        "cwd": cwd, "label": label, "created": int(time.time()),
                        "catalog": None, "argv": argv, "prompt_file": None,
                        "session_id": session_id if kind == "claude" else None,
                        "env_add": dict(env_add or {})})
    if names_dir is not None:
        try:   # the app's names folder: the rail shows it immediately
            names_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            (names_dir / name).write_text(name + "\n")
        except OSError:
            pass
    note_folder(ctx, cwd)
    ctx.log("spawn", desk=name, kind=kind, account=account, cwd=cwd)
    return 200, {"ok": True, "name": name}


NAMED_KINDS = ("claude", "cursor", "shell")   # the kinds spawn_desk registers in the names dir


def is_signin(entry):
    """A registry entry of a sign-in desk (25.3 B1)."""
    return (isinstance(entry, dict) and entry.get("signin") is True
            and entry.get("kind") in SIGNIN_ARGV)


def spawn_signin(ctx, body):
    """25.3 B1: POST /api/spawn {kind, account, signin: true} -> (status, reply).
    The CLI's own sign-in on one named account (the default sign-ins are never
    replaced from the tablet), in <state>/signin, never in the names
    folder. One live sign-in desk per engine and account; the entry of
    one whose session ended is dropped so its name comes free. A body ``cwd``
    is ignored: the folder is always the sign-in folder."""
    kind = body.get("kind")
    if kind not in SIGNIN_ARGV:
        return 400, {"ok": False, "error": "bad kind"}
    account = body.get("account")
    if account in (None, "", "default"):
        return 400, {"ok": False, "error": "sign-in needs an account"}
    label, err = _label(body)
    if err:
        return 400, {"ok": False, "error": err}
    if not isinstance(account, str) or not ACCOUNT_RE.fullmatch(account):
        return 400, {"ok": False, "error": "bad account"}
    acc = accounts(ctx)
    rows = [a for a in acc[kind] if a["name"] == account]
    if not rows:
        return 404, {"ok": False, "error": "no such account"}
    if kind == "claude" and rows[0].get("engine") != "claude":
        return 400, {"ok": False, "error": "not a claude login"}
    extra_env, extra_unset, account, bad = account_env(ctx, kind, account, acc)
    if bad:
        return bad[0], {"ok": False, "error": bad[1]}
    home = rows[0]["home"] if kind == "claude" else rows[0]["folder"]
    argv = list(SIGNIN_ARGV[kind])
    folder = str(ctx.signin_dir())
    with ctx.spawn_lock:   # SEC-12
        live = sessions(ctx)
        for n, e in registry(ctx).items():
            if is_signin(e) and e.get("kind") == kind and e.get("account") == account:
                if f"fc-{n}" in live:
                    return 409, {"ok": False, "error": "already signing in"}
                reg_delete(ctx, n)
        cap = cap_check(ctx, kind)
        if cap:
            return 409, {"ok": False, "error": cap}
        taken = taken_names(ctx)
        if label:
            if label in taken:
                return 409, {"ok": False, "error": "name taken"}
            name = label
        else:
            name = alloc_name(f"signin-{kind}-{account}", taken)
        ok, err = create_session(ctx, name, folder, argv, extra_env, extra_unset)
        if not ok:
            return 502, {"ok": False, "error": err}
        note_spawn(ctx)
        reg_write(ctx, {"name": name, "kind": kind, "account": account, "engine": None,
                        "cwd": folder, "label": label, "created": int(time.time()),
                        "catalog": None, "argv": argv, "prompt_file": None,
                        "signin": True, "signin_home": home})
    ctx.log("spawn", desk=name, kind=kind, account=account, cwd=folder, signin=True)
    return 200, {"ok": True, "name": name}


def respawn(ctx, name):
    """Re-create a registered desk whose tmux session is gone, from its own
    registry entry: the same kind, cwd, account environment, -u list and script
    (review SEC-4). A run cannot come back (its prompt file is gone). (ok, err)."""
    entry = reg_read(ctx, name)
    if not isinstance(entry, dict) or entry.get("name") != name:
        return False, "no such desk"
    kind, argv, cwd = entry.get("kind"), entry.get("argv"), entry.get("cwd")
    if kind not in (*NAMED_KINDS, "cli"):
        return False, "cannot respawn a run"
    if not (isinstance(argv, list) and argv and all(isinstance(a, str) for a in argv)):
        return False, "no argv recorded"
    if is_signin(entry):   # 25.3 B1: its own folder, made again when it went
        if cwd != str(ctx.state / "signin"):
            return False, "cwd gone"
        ctx.signin_dir()
    elif not isinstance(cwd, str) or not (cwd == str(Path.home()) or cwd_ok(ctx, cwd)):
        return False, "cwd gone"
    extra_env, extra_unset, account, bad = account_env(ctx, kind, entry.get("account") or None)
    if bad:
        return False, bad[1]
    # a catalogue desk ran a command (workflow run, plugin remove, login remove):
    # it is never run a second time by opening the terminal; the person gets a
    # shell in the same folder with the argv shown as a note (server-flows-2)
    script = FIXED_NOTE_SCRIPT if kind == "cli" else FIXED_SCRIPT
    with ctx.spawn_lock:
        if has_session(ctx, name):
            return True, ""
        # review 1.2: what was read above may be gone or changed by now (a kill,
        # a sign-in for the same account that dropped this entry and opened its
        # own): read it again under the lock, and come back only if it is the same
        again = reg_read(ctx, name)
        if not isinstance(again, dict) or again.get("name") != name:
            return False, "no such desk"
        if again != entry:
            return False, "desk changed"
        if is_signin(entry):   # one live sign-in desk per engine and account, as spawn_signin keeps
            live = sessions(ctx)
            for n, e in registry(ctx).items():
                if (n != name and is_signin(e) and e.get("kind") == kind
                        and e.get("account") == entry.get("account") and f"fc-{n}" in live):
                    return False, "already signing in"
        ok, err = create_session(ctx, name, cwd, list(argv), extra_env, extra_unset, script=script)
    if ok:
        ctx.log("spawn", desk=name, kind=kind, account=account, cwd=cwd, respawn=True,
                note_only=(kind == "cli"))
    return ok, err


def write_prompt(ctx, task):
    name = secrets.token_hex(16) + ".txt"
    p = ctx.prompts_dir() / name
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(task)
    return p


def sweep_prompts(ctx, max_age=PROMPT_MAX_AGE):
    """Remove prompt files older than an hour (a desk unlinks its own)."""
    now, gone = time.time(), 0
    for f in ctx.prompts_dir().glob("*.txt"):
        try:
            if now - f.stat().st_mtime > max_age:
                f.unlink()
                gone += 1
        except OSError:
            pass
    return gone


def start_run(ctx, body):
    """POST /api/run -> (status, reply)."""
    task = body.get("task")
    if not isinstance(task, str):
        return 400, {"ok": False, "error": "bad task"}
    task = TASK_CTRL_RE.sub("", task.replace("\r\n", "\n"))
    if not (1 <= len(task) <= TASK_LIMIT):
        return 400, {"ok": False, "error": "bad task"}
    engine = body.get("engine")
    if not isinstance(engine, str) or not ENGINE_RE.fullmatch(engine):
        return 400, {"ok": False, "error": "bad engine"}
    mode = body.get("mode", "read-only") or "read-only"
    if mode not in ("read-only", "ask"):
        return 403, {"ok": False, "error": "mode not allowed"}
    max_cost = body.get("max_cost")
    if max_cost not in (None, ""):
        if not isinstance(max_cost, str) or not MAX_COST_RE.fullmatch(max_cost):
            return 400, {"ok": False, "error": "bad max_cost"}
    else:
        max_cost = None
    model = body.get("model")
    if model in (None, ""):
        model = None
    elif not isinstance(model, str) or not MODEL_RE.fullmatch(model):
        return 400, {"ok": False, "error": "bad model"}
    raw_cwd = body.get("cwd")
    cwd = cwd_ok(ctx, raw_cwd) if isinstance(raw_cwd, str) else None
    if cwd is None:
        return 400, {"ok": False, "error": "bad cwd"}
    label, err = _label(body)
    if err:
        return 400, {"ok": False, "error": err}
    if label and len(f"run-{label}") > 40:   # desk_slug keeps 40 chars: a longer name has no terminal and no kill
        return 400, {"ok": False, "error": "bad label"}
    account = body.get("account") or None
    account_used = False
    run_env, run_unset = {}, ()
    if account is not None:
        if not isinstance(account, str) or not ACCOUNT_RE.fullmatch(account):
            return 400, {"ok": False, "error": "bad account"}
        if engine in ("claude", "codex"):
            known = [a for a in accounts(ctx)["claude"]
                     if a["name"] == account and a.get("engine", "claude") == engine]
            if not known:
                return 404, {"ok": False, "error": "no such account"}
            account_used = True
        elif engine == "cursor":
            # 11-11: a Cursor account is a folder, not a core login; the run gets the
            # environment a Cursor desk gets, so a run from the phone picks its account
            run_env, run_unset, _, err = account_env(ctx, "cursor", account)
            if err:
                return err[0], {"ok": False, "error": err[1]}
            account_used = True
        else:
            account = None
    argv = [ctx.metabot, "ask", "-e", engine]
    if account_used and engine != "cursor":   # the core has logins for claude and codex alone
        argv += ["--login", account]
    argv += ["--cwd", cwd]
    if mode == "read-only":
        argv.append("--read-only")
    if max_cost is not None:
        argv += ["--max-cost", max_cost]
    if model is not None:
        argv += ["-m", model]   # the engine's model name as the core passes it on (cursor: --model)
    argv.append("-")
    with ctx.spawn_lock:   # SEC-12
        cap = cap_check(ctx, "run")
        if cap:
            return 409, {"ok": False, "error": cap}
        taken = taken_names(ctx)
        name = f"run-{label}" if label else alloc_run_name(taken)
        if name in taken:
            return 409, {"ok": False, "error": "name taken"}
        prompt = write_prompt(ctx, task)
        own = {}
        if run_in_process(ctx):
            try:
                p = spawn_run_process(ctx, name, cwd, argv, prompt, extra_env=run_env, extra_unset=run_unset)
                ok, err = True, ""
                own = {"pid": p.pid, "log": f"{name}.log"}
            except OSError as exc:
                ok, err = False, f"spawn failed: {type(exc).__name__}"
            try:
                prompt.unlink()   # the child holds the descriptor; nothing else reads it
            except OSError:
                pass
        else:
            ok, err = create_session(ctx, name, cwd, [str(prompt), *argv], extra_env=run_env or None,
                                     extra_unset=run_unset, script=FIXED_RUN_SCRIPT)
            if not ok:
                try:
                    prompt.unlink()
                except OSError:
                    pass
        if not ok:
            return 502, {"ok": False, "error": err}
        note_spawn(ctx)
        reg_write(ctx, {"name": name, "kind": "run", "account": account if account_used else None,
                        "engine": engine, "model": model, "cwd": cwd, "label": label,
                        "created": int(time.time()), "catalog": None, "argv": argv,
                        "prompt_file": prompt.name, **own})
    ctx.log("run", desk=name, engine=engine, model=model, mode=mode, cwd=cwd, account_used=account_used,
            max_cost=max_cost)
    return 200, {"ok": True, "name": name, "argv": argv, "prompt_file": prompt.name,
                 "account_used": account_used}


def _clean_out(text):
    text = OUT_CTRL_RE.sub("", text or "")
    return text[:OUT_CAP]


# MetaBot 0.4.0 cmd_engines prints a bridge as ``{mark} {name:<7} bridge[ from
# REF]: COMMAND[ (MISSING not found)]``: the command line a plugin wrote, which
# can carry a password as an MCP server's args do. It stays on the PC.
BRIDGE_LINE_RE = re.compile(r"^([* ] \S+ +bridge(?: from \S+)?): (.*)$")
BRIDGE_MISSING_RE = re.compile(r" \([^()]* not found\)$")
BRIDGE_READS = frozenset({"engines.list", "engines.manifest"})


def redact_bridges(text):
    """The engines reads with each bridge's command line replaced by
    ``<command on the PC>`` (and ``(its command is not found)`` when MetaBot
    said its program is missing); every other line as it was."""
    out = []
    for line in text.split("\n"):
        m = BRIDGE_LINE_RE.match(line)
        if m:
            line = f"{m.group(1)}: <command on the PC>" + (
                " (its command is not found)" if BRIDGE_MISSING_RE.search(m.group(2)) else "")
        out.append(line)
    return "\n".join(out)


def cli_read(ctx, body):
    """POST /api/cli/read -> (status, reply). Captured; cwd = home, or the body's
    ``cwd`` when given (section 2 rules), so a repository or office read runs
    where it belongs (review server-flows-6)."""
    cid = body.get("id")
    e = catalog.BY_ID.get(cid) if isinstance(cid, str) else None
    if e is None:
        return 404, {"ok": False, "error": "unknown id"}
    if e["kind"] != "read":
        return 403, {"ok": False, "error": "not a read"}
    argv, err = catalog.expand(cid, body.get("params"), ctx.catalog_ctx())
    if err:
        return 400, {"ok": False, "error": err}
    cwd, err = _cwd_from(ctx, body)
    if err:
        return 400, {"ok": False, "error": err}
    env = {k: v for k, v in os.environ.items() if k not in DROPPED_ENV}
    env["METABOT_HOME"] = ctx.metabot_home
    t0 = time.time()
    try:
        r = run_captured(argv, ctx.read_timeout, text=True, errors="replace", cwd=cwd, env=env)
    except subprocess.TimeoutExpired:
        ctx.log("cli_read", id=cid, argv=argv, error="timeout")
        return 502, {"ok": False, "error": "timeout"}
    except OSError as exc:
        ctx.log("cli_read", id=cid, argv=argv, error=str(exc))
        return 502, {"ok": False, "error": "cannot run"}
    ms = int((time.time() - t0) * 1000)
    out, errt = _clean_out(r.stdout), _clean_out(r.stderr)
    if cid in BRIDGE_READS:   # before the log line: history.sqlite never holds one either
        out, errt = redact_bridges(out), redact_bridges(errt)
    ctx.log("cli_read", id=cid, argv=argv, exit=r.returncode, stdout=out, stderr=errt, ms=ms)
    return 200, {"ok": True, "id": cid, "argv": argv, "exit": r.returncode,
                 "stdout": out, "stderr": errt, "ms": ms}


def cli_desk(ctx, body):
    """POST /api/cli/desk -> (status, reply). Opens the argv in a new desk."""
    cid = body.get("id")
    e = catalog.BY_ID.get(cid) if isinstance(cid, str) else None
    if e is None:
        return 404, {"ok": False, "error": "unknown id"}
    if e["kind"] != "desk":
        return 403, {"ok": False, "error": "not a desk command"}
    argv, err = catalog.expand(cid, body.get("params"), ctx.catalog_ctx())
    if err:
        return 400, {"ok": False, "error": err}
    label, err = _label(body)
    if err:
        return 400, {"ok": False, "error": err}
    cwd, err = _cwd_from(ctx, body)
    if err:
        return 400, {"ok": False, "error": err}
    with ctx.spawn_lock:   # SEC-12
        if cid == "serve" and serve_state(ctx)[0]:   # 25.3 B7: one serve at a time
            return 409, {"ok": False, "error": "serve already runs"}
        cap = cap_check(ctx, "cli")
        if cap:
            return 409, {"ok": False, "error": cap}
        taken = taken_names(ctx)
        if label:
            if label in taken:
                return 409, {"ok": False, "error": "name taken"}
            name = label
        else:
            name = alloc_name(cid.replace(".", "-"), taken)
        ok, err = create_session(ctx, name, cwd, argv)
        if not ok:
            return 502, {"ok": False, "error": err}
        note_spawn(ctx)
        reg_write(ctx, {"name": name, "kind": "cli", "account": None, "engine": None, "cwd": cwd,
                        "label": label, "created": int(time.time()), "catalog": cid,
                        "argv": argv, "prompt_file": None})
    ctx.log("cli_desk", desk=name, id=cid, argv=argv, cwd=cwd)
    return 200, {"ok": True, "name": name, "argv": argv}


def kill_session(ctx, name):
    """tmux kill-session for a desk; registry entry removed. (rc, err)."""
    rc, _, err = tmux(ctx, "kill-session", "-t", f"=fc-{name}")
    reg_delete(ctx, name)
    return rc == 0, err


def forget_desk(ctx, name, names_dir=None, entry=None):
    """Drop what MetaChat wrote for a desk: the registry entry and, for the
    kinds spawn_desk named in the names folder, its names-dir file
    (review F11). A names file MetaChat did not write is left alone. ``entry``
    is the registry entry when the caller read it before kill_session."""
    if entry is None:
        entry = reg_read(ctx, name)
    reg_delete(ctx, name)
    if is_process_run(entry) and entry.get("log"):
        with _PROCS_LOCK:
            _PROCS.pop(_proc_key(ctx, name), None)
        try:
            _log_path(ctx, entry).unlink()
        except OSError:
            pass
    if (names_dir is not None and isinstance(entry, dict) and entry.get("kind") in NAMED_KINDS
            and not is_signin(entry)):   # a sign-in desk never wrote one (25.3 B1)
        try:
            (Path(names_dir) / name).unlink()
        except OSError:
            pass
    return entry


SERVE_DONE_RE = re.compile(r"^\[metachat\] exit \d+|\(finished earlier; not run again\)", re.M)
# a serve desk this young whose serve has not locked yet counts as starting (MetaBot
# imports, then takes the lock), so a second tap never opens a second serve
SERVE_START_GRACE = 20.0
PROC_LOCKS = "/proc/locks"


def serve_lock_state(metabot_home):
    """MetaBot's own word on whether a serve runs (``metabot.serve.serve_lock``,
    0.4.0): while one runs it holds an ``flock`` on ``<home>/serve.lock`` with
    its pid in the file, and it empties the file when it stops. Never takes the
    lock (a serve starting then would refuse): the file's pid is matched with
    the FLOCK rows of /proc/locks by pid and inode. "held", "free" (the file is
    empty, or its pid holds no lock on it: a serve killed outright), or None
    when this cannot be told (no file: serve never ran in this home, or a
    MetaBot without the lock; a file that names no pid)."""
    path = os.path.join(str(metabot_home), "serve.lock")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None
        held = os.pread(fd, 32, 0).decode("ascii", "replace").strip()
    except OSError:
        return None
    finally:
        os.close(fd)
    if not held:
        return "free"
    if not (held.isascii() and held.isdigit()):
        return None
    pid = int(held)
    try:
        with open(PROC_LOCKS, "rb") as f:
            rows = f.read().decode("ascii", "replace").splitlines()
    except OSError:   # no /proc/locks: the pid alone says it
        return "held" if pid_alive(pid) else "free"
    for row in rows:
        parts = row.split()
        if parts[1:2] == ["->"]:   # a waiter, not a holder
            continue
        if len(parts) >= 6 and parts[1] == "FLOCK" and parts[4] == str(pid) \
                and parts[5].rsplit(":", 1)[-1] == str(st.st_ino):
            return "held"
    return "free"


def serve_state(ctx):
    """25.3 B6/B7: (running, desk). MetaBot's ``serve.lock`` decides
    (serve_lock_state): held = a serve runs, and ``desk`` is the registered
    live serve desk whose pane does not show the command ended (None when the
    serve runs from elsewhere); free = none runs, except a serve desk younger
    than SERVE_START_GRACE whose pane has not ended (it is starting). Without
    the lock's word, the pane alone: a live serve desk whose pane does not
    show FIXED_SCRIPT's ``[metachat] exit N`` line or the note a desk that came
    back shows instead of running it again."""
    lock = serve_lock_state(ctx.metabot_home)
    live = sessions(ctx)
    now = time.time()
    for name, e in sorted(registry(ctx).items()):
        if e.get("kind") != "cli" or e.get("catalog") != "serve" or f"fc-{name}" not in live:
            continue
        if lock == "free":
            created = e.get("created")
            if not isinstance(created, (int, float)) or now - created > SERVE_START_GRACE:
                continue
        rc, out, _ = tmux(ctx, "capture-pane", "-p", "-t", f"=fc-{name}:", timeout=2)
        if rc == 0 and SERVE_DONE_RE.search(out):
            continue
        return True, name
    return lock == "held", None


def serve_desk(ctx):
    """The desk serve_state names (None when no desk of ours runs serve)."""
    return serve_state(ctx)[1]


# ---------------------------------------------------------------- extend (25.3 B6)

EXTEND_TTL = 10.0
EXTEND_PARTS = ("plugins", "marketplaces", "engines")


def _s(v, limit=300):
    return v[:limit] if isinstance(v, str) else ""


def _names(v, limit=500):
    return [x[:200] for x in v if isinstance(x, str)][:limit] if isinstance(v, list) else []


def _plugin_row(p):
    """One installed plugin, names only: hook and server command lines, env
    values and the source path stay on the PC. A hook entry that is not a
    count is counted, never passed on."""
    hooks = {}
    if isinstance(p.get("hooks"), dict):
        for event, v in p["hooks"].items():
            n = (v if isinstance(v, int) and not isinstance(v, bool)
                 else len(v) if isinstance(v, list) else None)
            if isinstance(event, str) and n is not None:
                hooks[event[:100]] = n
    return {"name": _s(p.get("name")), "ref": _s(p.get("ref")), "marketplace": _s(p.get("marketplace")),
            "tree": _s(p.get("tree"), 64), "version": _s(p.get("version"), 100),
            "enabled": p.get("enabled") is True, "skills": _names(p.get("skills")),
            "commands": _names(p.get("commands")), "agents": _names(p.get("agents")), "hooks": hooks,
            "hooks_allowed": p.get("hooks_allowed") is True, "servers": _names(p.get("servers")),
            "servers_added": p.get("servers_added") is True, "bridges": _names(p.get("bridges")),
            "bridges_on": p.get("bridges_on") is True, "error": _s(p.get("error")) or None}


def _market_row(m):
    rows = m.get("plugins") if isinstance(m.get("plugins"), list) else []
    return {"name": _s(m.get("name")), "folder": _s(m.get("folder"), 4096),
            "plugins": [{"name": _s(x.get("name")), "description": _s(x.get("description"), 500)}
                        for x in rows if isinstance(x, dict)],
            "error": _s(m.get("error")) or None}


def _engine_row(e):
    return {"name": _s(e.get("name")), "kind": "builtin" if e.get("kind") == "builtin" else "bridge",
            "local": e.get("local") is True, "models": _names(e.get("models"), 50),
            "plugin": _s(e.get("plugin")) or None}


def extend_info(ctx, fresh=False):
    """25.3 B6: the MetaBot half of GET /api/extend: ``plugins``,
    ``marketplaces`` and ``engines`` from ``mbq.py plugins | marketplaces |
    engines`` (run at once), every row cut to the fields of the contract (names
    only), plus ``errors``: [{part, error}] for a part whose mbq call failed
    (that part is empty) and for settings MetaBot could not read. Cached
    EXTEND_TTL seconds; ``fresh`` reads again. Returns a copy."""
    with ctx.extend_lock:
        hit = ctx._extend
        if not fresh and hit[1] is not None and time.time() - hit[0] < EXTEND_TTL:
            return json.loads(json.dumps(hit[1]))
        got = {}

        def fetch(part):
            got[part] = mbq(ctx, part)

        threads = [threading.Thread(target=fetch, args=(part,), daemon=True) for part in EXTEND_PARTS]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        out = {"plugins": [], "marketplaces": [], "engines": [], "errors": []}
        for part, row_of in (("plugins", _plugin_row), ("marketplaces", _market_row),
                             ("engines", _engine_row)):
            res = got.get(part)
            rows = res.get(part) if isinstance(res, dict) else None
            if not isinstance(rows, list):
                err = res.get("error") if isinstance(res, dict) else None
                out["errors"].append({"part": part, "error": _s(err) or "mbq failed"})
                continue
            out[part] = [row_of(r) for r in rows if isinstance(r, dict)]
            if isinstance(res.get("error"), str) and res["error"]:
                out["errors"].append({"part": part, "error": _s(res["error"])})
        ctx._extend = [time.time(), out]
        return json.loads(json.dumps(out))


# ---------------------------------------------------------------- accounts

_MBQW = {"key": None, "worker": None}
_MBQW_LOCK = threading.Lock()


def close_reader() -> None:
    """Stop the warm MetaBot reader, if one runs, and wait for its process."""
    with _MBQW_LOCK:
        w, _MBQW["worker"], _MBQW["key"] = _MBQW["worker"], None, None
    if w is not None:
        w.close()


def mbq_worker(ctx):
    """Speed (11-09): the warm MetaBot reader for this ctx (mbqw.Worker running
    ``mbq.py serve``), one per (metabot_python, metabot_home); a new key closes
    the old worker. None when config says ``mbq_worker: false``."""
    if ctx.cfg.get("mbq_worker", True) is False:
        return None
    from . import mbqw
    env = dict(os.environ)
    env["METABOT_HOME"] = ctx.metabot_home
    # the env is part of the key: a worker must read with what a one-shot run would get
    key = (ctx.metabot_python, str(ctx.metabot_home), str(ctx.here),
           hash(frozenset(env.items())))
    with _MBQW_LOCK:
        if _MBQW["key"] != key:
            if _MBQW["worker"] is not None:
                _MBQW["worker"].close()
            _MBQW["worker"] = mbqw.Worker([key[0], str(ctx.here / "mbq.py"), "serve"], env=env,
                                          cwd=str(Path.home()), timeout=20.0)
            _MBQW["key"] = key
        return _MBQW["worker"]


def mbq(ctx, *args, timeout=20):
    """MetaBot read: through the warm worker (mbq_worker), else mbq.py run
    one-shot under metabot_python; the parsed object, or {"error": ...}."""
    w = mbq_worker(ctx)
    if w is not None:
        out = w.call(list(args), timeout=timeout)
        if not (isinstance(out, dict) and str(out.get("error") or "").startswith("mbq: cannot start")):
            return out
    return mbq_once(ctx, *args, timeout=timeout)


def mbq_once(ctx, *args, timeout=20):
    """Run mbq.py under metabot_python once; the parsed object, or {"error": ...}."""
    env = dict(os.environ)
    env["METABOT_HOME"] = ctx.metabot_home
    try:
        r = run_captured([ctx.metabot_python, str(ctx.here / "mbq.py"), *args],
                         timeout, text=True, env=env, cwd=str(Path.home()))
    except (OSError, subprocess.SubprocessError) as exc:
        return {"error": f"mbq: {type(exc).__name__}"}
    try:
        return json.loads(r.stdout)
    except ValueError:
        first = (r.stderr or "").strip().splitlines()
        return {"error": first[0][:200] if first else f"mbq exit {r.returncode}"}


def _ready(home):
    try:
        st = os.stat(home)
    except OSError:
        return False
    return os.path.isdir(home) and st.st_uid == os.getuid() and not (st.st_mode & 0o077)


def signed_in(engine, home):
    """25.3 B2: is this sign-in folder signed in? Stat only: a sign-in file
    is never opened or read. A MetaBot login (claude, codex) needs its folder
    fit to run on (``_ready``) and the file ``metabot login check`` looks for;
    a Cursor folder needs ``config/cursor/auth.json``. Any other engine: False."""
    if not isinstance(home, str) or not home:
        return False
    if engine == "cursor":
        return os.path.exists(os.path.join(home, *CURSOR_AUTH))
    f = SIGN_IN_FILES.get(engine)
    return bool(f) and _ready(home) and os.path.exists(os.path.join(home, f))


# 11-09: a login past its time limit still has its sign-in file, so a stat says
# ready; its newest run says it could not sign in. Read from MetaBot's own run
# rows, never from the sign-in file. A later run that worked clears it.
SIGNIN_FAILED_RE = re.compile(r"failed to authenticate|not logged in|please run /login|claude auth login|"
                              r"oauth token (?:has )?expired|invalid api key", re.I)


def last_signin_failures(ctx):
    """{login name, or "default:<engine>": {"at", "why"}} for each sign-in whose
    newest run could not sign in."""
    q = mbq(ctx, "runs")
    out, seen = {}, set()
    for row in (q.get("rows") if isinstance(q, dict) else None) or []:   # newest first
        if not isinstance(row, dict):
            continue
        login = row.get("login")
        key = login if isinstance(login, str) and login else f"default:{row.get('engine')}"
        if key in seen:
            continue
        seen.add(key)
        err = row.get("error") if isinstance(row.get("error"), str) else ""
        if row.get("status") not in (None, "ok") and SIGNIN_FAILED_RE.search(err):
            why = re.sub(r"^[a-z]+: (?:success: )?", "", OUT_CTRL_RE.sub("", err)).strip()
            out[key] = {"at": row.get("finished_at"), "why": why[:157] + "..." if len(why) > 160 else why}
    return out


def accounts(ctx):
    """GET /api/accounts: claude from MetaBot's login store, cursor from
    folders, and whether each is signed in (25.3 B2), plus the two default
    sign-ins (``defaults``), all by stat alone."""
    claude = []
    got = mbq(ctx, "logins")
    if isinstance(got, list):
        for login in got:
            if not isinstance(login, dict) or not isinstance(login.get("name"), str):
                continue
            engine, home = str(login.get("engine", "claude")), str(login.get("home", ""))
            claude.append({"name": login["name"], "home": home, "engine": engine,
                           "max_at_once": int(login.get("max_at_once", 1) or 1),
                           "ready": signed_in(engine, home), "source": "metabot"})
    cursor = []
    for d in sorted(ctx.accounts_dir("cursor").iterdir()):
        if d.is_dir() and ACCOUNT_RE.fullmatch(d.name):
            cursor.append({"name": d.name, "folder": str(d), "proven": False,
                           "ready": signed_in("cursor", str(d))})
    home = Path.home()
    defaults = {"claude": {"ready": os.path.exists(home / ".claude" / ".credentials.json")},
                "cursor": {"ready": os.path.exists(home / ".config" / "cursor" / "auth.json")}}
    failed = last_signin_failures(ctx)
    for a in claude:
        if a["name"] in failed:
            a.update(ready=False, signin_failed=failed[a["name"]])
    for engine, d in defaults.items():
        if f"default:{engine}" in failed:
            d.update(ready=False, signin_failed=failed[f"default:{engine}"])
    return {"claude": claude, "cursor": cursor, "defaults": defaults}


def add_account(ctx, body):
    """POST /api/accounts -> (status, reply)."""
    engine, name = body.get("engine"), body.get("name")
    if engine not in ("claude", "cursor"):
        return 400, {"ok": False, "error": "bad engine"}
    if not isinstance(name, str) or not ACCOUNT_RE.fullmatch(name) or name == "default":
        return 400, {"ok": False, "error": "bad name"}
    existing = accounts(ctx)
    if any(a["name"] == name for a in existing[engine]):
        return 409, {"ok": False, "error": "name taken"}
    folder = ctx.accounts_dir(engine) / name
    if folder.exists():
        return 409, {"ok": False, "error": "name taken"}
    if engine == "cursor":
        (folder / "config").mkdir(mode=0o700, parents=True)
        os.chmod(folder, 0o700)
        ctx.log("account", engine=engine, name=name)
        return 200, {"ok": True, "engine": engine, "name": name, "folder": str(folder)}
    folder.mkdir(mode=0o700, parents=True)
    os.chmod(folder, 0o700)
    src = Path.home() / ".claude"
    for item in CLAUDE_SEED:   # never .credentials.json, never .claude.json
        if (src / item).exists():
            try:
                (folder / item).symlink_to(src / item)
            except OSError:
                pass
    argv = [ctx.metabot, "login", "add", name, "--engine", "claude", "--home", str(folder),
            "--max-at-once", "3"]
    env = dict(os.environ)
    env["METABOT_HOME"] = ctx.metabot_home
    try:
        r = run_captured(argv, 30, text=True, errors="replace", env=env, cwd=str(Path.home()))
    except (OSError, subprocess.SubprocessError) as exc:
        shutil.rmtree(folder, ignore_errors=True)
        return 502, {"ok": False, "error": f"login add: {type(exc).__name__}"}
    if r.returncode != 0:
        shutil.rmtree(folder, ignore_errors=True)
        first = (r.stderr or r.stdout or "").strip().splitlines()
        return 502, {"ok": False, "error": (first[0] if first else "login add failed")[:200]}
    ctx.log("account", engine=engine, name=name, folder=str(folder))
    return 200, {"ok": True, "engine": engine, "name": name, "folder": str(folder)}


# ---------------------------------------------------------------- needs_you, live -> desk

def ask_class(lines):
    """The class chip of a waiting ask (22.3): destructive, write or read, read
    off the same pane lines the prompt sits on."""
    text = "\n".join(lines)
    if DESTRUCTIVE_RE.search(text):
        return "destructive"
    if WRITE_RE.search(text):
        return "write"
    return "read"


def ask_text(line):
    """Follow-up A: a row's ``ask_line`` from one pane line: control
    characters out, trimmed, at most ASK_LINE_MAX characters. None for a line
    that holds a link (a sign-in link's query string stays in the pane) or is
    empty once cleaned."""
    if "://" in line:
        return None
    return ASK_CTRL_RE.sub("", line).strip()[:ASK_LINE_MAX].rstrip() or None


def asking_lines(lines):
    """The lines of ``lines`` (the last NEEDS_LINES non-empty pane lines) that
    ask: NEEDS_RE or MENU_RE matches them, or, on the last line, one of
    ASK_RES (a secret prompt waits only where the cursor waits)."""
    alone = [bool(NEEDS_RE.search(ln) or MENU_RE.search(ln)) for ln in lines]
    out = []
    # 11-09 (found on the tablet): Claude Code wraps its own hint with a hard newline on a
    # narrow pane ("… · Esc" / "to cancel"); a match that only exists across the break is an
    # ask too, read as the two lines joined, in screen order. Never across a shell command line.
    for i, ln in enumerate(lines):
        if alone[i]:
            out.append(ln)
            continue
        if i + 1 < len(lines) and not alone[i + 1] and not COMMAND_LINE_RE.match(ln):
            joined = ln.rstrip() + " " + lines[i + 1].strip()
            m = NEEDS_RE.search(joined)
            if m and m.start() < len(ln.rstrip()) < m.end():
                out.append(joined)
    if lines and lines[-1] not in out and any(p.search(lines[-1]) for _, p in ASK_RES):
        out.append(lines[-1])
    return out


def ask_of(asks):
    """Follow-up A: (ask, ask_line) from the asking lines (see asking_lines):
    the latest one is the ask's line; the first of ASK_RES that matches it
    names the ask, and ``select`` when none does."""
    line = next((ln for ln in reversed(asks) if not HINT_ONLY_RE.match(ln)), asks[-1])
    ask = next((name for name, pat in ASK_RES if pat.search(line)), "select")
    return ask, ask_text(line)


def signin_ask_line(view, window):
    """The ask_line of a waiting sign-in desk: the latest line of its window
    that matches its step's pattern and is not the link, or None."""
    pat = dict(SIGNIN_STEPS.get(view["engine"], ())).get(view["step"])
    if pat is None:
        return None
    for ln in reversed(window):
        text = ask_text(ln) if pat.search(ln) else None
        if text:
            return text
    return None


def signin_window(text):
    """25.3 B1: the last SIGNIN_LINES lines of a capture. Blank rows under the
    screen (a tall pane the CLI has not filled) are dropped first: they are
    not lines of it."""
    lines = (text or "").splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    return lines[-SIGNIN_LINES:]


def signin_step(engine, lines):
    """The step whose pattern matched latest in ``lines`` (the index of each
    pattern's last matching line; the largest wins, a tie goes to the later
    screen), or None when none matched."""
    best, step = -1, None
    for name, pat in SIGNIN_STEPS.get(engine, ()):
        idx = max((i for i, ln in enumerate(lines) if pat.search(ln)), default=-1)
        if idx >= 0 and idx >= best:
            best, step = idx, name
    return step


def signin_url(lines):
    """25.3 B1: the sign-in link on the screen, or None. From the last line
    holding ``https://``: the text from there to the line's end (cut at the
    first space: "(c to copy)" is not the link), then each following line
    whose whole stripped text is URL characters (the CLI breaks a long link
    with hard newlines). At most SIGNIN_URL_MAX characters; kept only when it
    is all URL characters, has no user part and its host is a sign-in host."""
    for i in range(len(lines) - 1, -1, -1):
        at = lines[i].find("https://")
        if at >= 0:
            break
    else:
        return None
    first = lines[i][at:].rstrip()
    cut = re.search(r"\s", first)
    url = first[:cut.start()] if cut else first
    if not cut:
        for ln in lines[i + 1:]:
            part = ln.strip()
            if len(url) >= SIGNIN_URL_MAX or not URL_CHARS_RE.fullmatch(part):
                break
            url += part
    url = url[:SIGNIN_URL_MAX]
    if not URL_CHARS_RE.fullmatch(url):
        return None
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname
    except ValueError:
        return None
    if parts.scheme != "https" or "@" in parts.netloc or host not in SIGNIN_HOSTS:
        return None
    return url


def signin_view(entry, lines):
    """The ``signin`` field of a sign-in desk's row: {engine, account, step,
    url, exit}. No pattern matched: ``ready`` when the account is signed in
    (B2), else ``starting``. Follow-up C: when the CLI has ended (its
    ``[metachat] exit N`` line is the latest match) the step is ``ended``,
    ``exit`` is N and ``url`` is None (the link died with the CLI); ``exit``
    is None on every other step."""
    engine = entry.get("kind")
    step = signin_step(engine, lines)
    if step is None:
        step = "ready" if signed_in(engine, entry.get("signin_home")) else "starting"
    code = None
    if step == "ended":
        code = next((int(m.group(1)) for m in map(SIGNIN_ENDED_RE.search, reversed(lines)) if m), None)
    return {"engine": engine, "account": entry.get("account"), "step": step,
            "url": None if step == "ended" else signin_url(lines), "exit": code}


def after_run_exit(lines):
    """11-11 (found on the tablet): the lines that can still ask once a desk's script
    has printed its ``[metachat] exit N`` line (FIXED_RUN_SCRIPT, FIXED_SCRIPT). Between
    that line and the shell's first prompt the screen is the model's answer, which may
    quote an ask ("do you want to proceed"); nothing waits there. Lines after a command
    typed into the shell that follows count again (a ``sudo`` there does wait)."""
    for i in range(len(lines) - 1, -1, -1):
        if RUN_EXIT_LINE_RE.match(lines[i].rstrip()):
            rest = lines[i + 1:]
            for j, ln in enumerate(rest):
                if COMMAND_LINE_RE.match(ln):
                    return rest[j:]
            return []
    return lines


def needs_info(ctx, name, entry=None, fresh=False):
    """Section 7 / 21.4: does a prompt wait on one of the last NEEDS_LINES
    non-empty pane lines (see asking_lines)? A last line that is a shell
    prompt (ends in ``$`` or ``#``) means nothing waits. Cached 2 s per desk
    (``fresh`` reads the pane now). Returns ``{"needs": bool, "cls":
    "read"|"write"|"destructive"|None, "ask": str|None, "ask_line": str|None,
    "since": float|None}``: ``since`` is when the desk started waiting, and
    ``cls``, ``ask`` (follow-up A: signin, secret, yes, line or select, see
    ask_of) and ``ask_line`` are None when nothing waits. A sign-in desk
    (25.3 B1) is captured with ``-J -S -40``, waits on every step where its
    CLI waits for the person and never once its CLI has ended (follow-up C),
    asks ``signin``, and its reply adds ``signin`` (see signin_view).
    ``entry`` is the desk's registry entry when the caller has it."""
    if entry is None:
        entry = reg_read(ctx, name)
    signin = is_signin(entry)
    now = time.time()
    with ctx.lock:
        hit = ctx._needs.get(name)
        if not fresh and hit and now - hit[0] < 2.0 and (hit[2] is not None) == signin:
            out = dict(hit[1], since=ctx._needs_since.get(name))
            if signin:
                out["signin"] = dict(hit[2])
            return out
    if signin:
        rc, out, _ = tmux(ctx, "capture-pane", "-p", "-J", "-S", f"-{SIGNIN_LINES}",
                          "-t", f"=fc-{name}:", timeout=2)
        window = signin_window(out) if rc == 0 else []
        tail = [ln for ln in window if ln.strip()][-CLASS_LINES:]
    else:
        rc, out, _ = tmux(ctx, "capture-pane", "-p", "-t", f"=fc-{name}:", timeout=2)
        tail = [ln for ln in out.splitlines()[-40:] if ln.strip()][-CLASS_LINES:] if rc == 0 else []
    lines = tail[-NEEDS_LINES:]
    if not signin:   # a sign-in desk reads its own ended step (follow-up C)
        lines = after_run_exit(lines)
    at_shell = not lines or bool(SHELL_PROMPT_RE.search(lines[-1].rstrip()))
    asks = [] if at_shell else asking_lines(lines)
    val, view, step_waits = bool(asks), None, False
    if signin:
        view = signin_view(entry, window)
        step_waits = not at_shell and view["step"] in SIGNIN_WAITING.get(view["engine"], ())
        if view["step"] == "ended":
            val = False
        elif step_waits:
            val = True
    info = {"needs": val, "cls": None, "ask": None, "ask_line": None}
    if isinstance(entry, dict) and entry.get("kind") == "cli" and rc == 0:
        # review 1.2: a catalogue desk's result, FIXED_SCRIPT's ``[metachat] exit N``
        # (the latest on the screen), so the page can wait for it; None while it runs
        info["exit"] = next((int(m.group(1)) for m in map(SIGNIN_ENDED_RE.search,
                                                          reversed(out.splitlines())) if m), None)
    if val:
        info["cls"] = ask_class(tail)
        if signin:   # the step's own line, else the line that asks
            info["ask"] = "signin"
            info["ask_line"] = ((signin_ask_line(view, window) if step_waits else None)
                                or (ask_of(asks)[1] if asks else None))
        else:
            info["ask"], info["ask_line"] = ask_of(asks)
    with ctx.lock:
        ctx._needs[name] = (now, info, view)
        if val:
            ctx._needs_since.setdefault(name, now)
        else:
            ctx._needs_since.pop(name, None)
        since = ctx._needs_since.get(name)
    reply = dict(info, since=since)
    if signin:
        reply["signin"] = dict(view)
    return reply

def needs_you(ctx, name):
    """Section 7: does a prompt wait in this desk's pane (see needs_info)."""
    return needs_info(ctx, name)["needs"]


def _ppid(pid, tbl=None):
    """The parent pid: /proc when the host has it, else the ps table (A4)."""
    return procs.ppid(pid, tbl)


def desk_of_pid(ctx, pid, reg=None):
    """Walk the parents (at most 12 steps) to a registered desk's pane pid: /proc on
    Linux, one ps table elsewhere."""
    if not isinstance(pid, int) or pid <= 0:
        return None
    reg = registry(ctx) if reg is None else reg
    panes = {v: k for k, v in pane_pids(ctx).items() if k in reg}
    panes.update({d["pid"]: n for n, d in reg.items() if is_process_run(d)})
    tbl = None if procs.PROC.is_dir() else procs.table()
    cur = pid
    for _ in range(13):
        if cur in panes:
            return panes[cur]
        cur = _ppid(cur, tbl)
        if not cur or cur <= 1:
            return None
    return None


# ---------------------------------------------------------------- runs, receipts

def runs(ctx, limit=50, project=None):
    """GET /api/mb/runs."""
    reg = registry(ctx)
    out = []
    lv = mbq(ctx, "live")
    for e in (lv.get("alive") if isinstance(lv, dict) else None) or []:
        if not isinstance(e, dict):
            continue
        desk = desk_of_pid(ctx, e.get("pid"), reg)
        cwd = reg.get(desk, {}).get("cwd", "") if desk else ""
        out.append({"run_id": e.get("id"), "state": "live", "desk": desk,
                    "needs_you": needs_you(ctx, desk) if desk else False,
                    "project": project_of(ctx, cwd), "started": e.get("started"),
                    "engine": e.get("engine"), "cwd": cwd, "live_id": e.get("id"),
                    "kind": e.get("kind")})
    q = mbq(ctx, "runs")
    rows = q.get("rows") if isinstance(q, dict) else None
    err = q.get("error") if isinstance(q, dict) else "mbq failed"
    total = q.get("total", 0) if isinstance(q, dict) else 0
    desk_of_run = {e["run_id"]: n for n, e in reg.items() if isinstance(e.get("run_id"), str)}
    # a run whose process is gone and that never sealed a receipt (the app, and the run
    # with it, was killed mid-run): said so, never shown as live or done, never dropped
    sealed = {r.get("run_id") for r in rows or [] if isinstance(r, dict)}
    own_pid = {d["pid"]: n for n, d in reg.items() if is_process_run(d)}
    for e in (lv.get("stale") if isinstance(lv, dict) else None) or []:
        if not isinstance(e, dict) or e.get("id") in sealed or e.get("kind") not in (None, "run"):
            continue
        desk = desk_of_run.get(e.get("id")) or own_pid.get(e.get("pid"))
        cwd = reg.get(desk, {}).get("cwd", "") if desk else ""
        out.append({"run_id": e.get("id"), "state": "interrupted", "desk": desk,
                    "project": project_of(ctx, cwd), "started": e.get("started"),
                    "engine": e.get("engine"), "cwd": cwd, "kind": e.get("kind"),
                    "problem": "its process is gone and it left no receipt (the app stopped mid-run)"})
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        cwd = row.get("cwd") or (row.get("meta") or {}).get("cwd") if isinstance(row.get("meta"), dict) else row.get("cwd")
        out.append({"run_id": row.get("run_id"), "state": "done",
                    "receipt_state": row.get("state"), "desk": desk_of_run.get(row.get("run_id")),
                    "project": project_of(ctx, cwd if isinstance(cwd, str) else ""), "row": row})
    if project:
        out = [r for r in out if r["project"] == project]
    return {"runs": out[:limit], "total": total, "error": err}


# A run desk tells its lane how the run ended (11-09). Its run id comes from the
# live list while it runs (the pid walks up to the desk's pane) or, for a run too
# fast to be seen live, from the receipt line MetaBot printed, read only after
# the desk's script printed its exit line.
RUN_RECEIPT_LINE_RE = re.compile(r"^\[metabot\] receipt \S*/([A-Za-z0-9][A-Za-z0-9._-]{0,127})\.json \(", re.M)
RUN_EXIT_LINE_RE = re.compile(r"^\[metachat\] exit (\d{1,3})[ \t]*$", re.M)


def live_run_desks(ctx, names, reg):
    """{desk: run_id} for the desks in ``names`` with a live MetaBot run."""
    lv = mbq(ctx, "live")
    out = {}
    for e in (lv.get("alive") if isinstance(lv, dict) else None) or []:
        if isinstance(e, dict) and isinstance(e.get("id"), str):
            desk = desk_of_pid(ctx, e.get("pid"), reg)
            if desk in names:
                out[desk] = e["id"]
    return out


def run_pane_end(ctx, name):
    """(exit code, run id or None) once the run desk's script has printed its
    exit line; (None, None) while it has not. A run the app spawned itself is
    read from its log (run_proc_end)."""
    entry = reg_read(ctx, name)
    if is_process_run(entry):
        return run_proc_end(ctx, name, entry)
    rc, out, _ = tmux(ctx, "capture-pane", "-p", "-J", "-S", "-300", "-t", f"=fc-{name}:", timeout=2)
    if rc != 0:
        return None, None
    ex = RUN_EXIT_LINE_RE.findall(out)
    if not ex:
        return None, None
    ids = RUN_RECEIPT_LINE_RE.findall(out)
    return int(ex[-1]), (ids[-1] if ids else None)


def live_desk(ctx, run_id):
    """The desk of a run that is still live (its live entry's pid), else None."""
    lv = mbq(ctx, "live")
    for e in (lv.get("alive") if isinstance(lv, dict) else None) or []:
        if isinstance(e, dict) and e.get("id") == run_id:
            return desk_of_pid(ctx, e.get("pid"))
    return None


def run_view(ctx, run_id):
    """GET /api/mb/run/<id> -> (status, view or error reply). 404 only for a
    JSON null from mbq; an mbq failure is 502 (contract 3 / 4.14, review F10)."""
    view = mbq(ctx, "run", run_id)
    if view is None:
        return 404, {"ok": False, "error": "no such run"}
    if isinstance(view, dict) and "error" in view and "run_id" not in view:
        return 502, {"ok": False, "error": str(view["error"])[:200]}
    return 200, view


def receipt_path(ctx, run_id):
    """The receipt file inside <metabot_home>/receipts, or None."""
    if not catalog.is_safe_id(run_id):
        return None
    folder = Path(ctx.metabot_home) / "receipts"
    try:
        f = (folder / f"{run_id}.json").resolve()
        if folder.resolve() not in f.parents or not f.is_file():
            return None
    except (OSError, RuntimeError):
        return None
    return f


def _orcha_verify(ctx, arg, inp=None):
    """``orcha verify --json <arg>``; ``inp`` is bytes handed to stdin unchanged
    (no text-mode re-encoding: the PC must verify the exact bytes the tablet
    verified, review VP-1). Returns (pc object or None, exit, first stderr line)."""
    try:
        r = run_captured([ctx.orcha, "verify", "--json", arg], 30, inp=inp, cwd=str(Path.home()))
    except (OSError, subprocess.SubprocessError) as exc:
        return None, 2, f"orcha: {type(exc).__name__}"
    out = r.stdout.decode("utf-8", "replace")
    errt = r.stderr.decode("utf-8", "replace")
    try:
        pc = json.loads(out)
        if not isinstance(pc, dict):
            pc = None
    except ValueError:
        pc = None
    first = errt.strip().splitlines()
    return pc, r.returncode, (first[0] if first else "")[:200]


def verify(ctx, run_id):
    """POST /api/mb/verify."""
    f = receipt_path(ctx, run_id)
    if f is None:
        return 404, {"ok": False, "error": "no such receipt"}
    data = f.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    pc, code, _ = _orcha_verify(ctx, str(f))
    st = mbq(ctx, "state", run_id)
    receipt_state = st.get("state") if isinstance(st, dict) else None
    index_ok = None
    q = mbq(ctx, "runs")
    for row in (q.get("rows") if isinstance(q, dict) else None) or []:
        if isinstance(row, dict) and row.get("run_id") == run_id:
            digest = row.get("receipt_sha256")   # the index row's digest field
            if isinstance(digest, str):
                index_ok = digest == sha
            break
    ctx.log("verify", run_id=run_id, pc_exit=code, valid=bool(pc and pc.get("valid")),
            receipt_state=receipt_state, index_sha_ok=index_ok, sha256=sha)
    return 200, {"ok": True, "id": run_id, "pc": pc, "pc_exit": code,
                 "metabot": {"receipt_state": receipt_state}, "index_sha_ok": index_ok,
                 "sha256": sha}


def sdk_bytes(body):
    """The envelope bytes of a POST /api/sdk/verify body, or None (400 bad raw).

    ``raw_b64`` (base64 of the exact bytes the tablet verified) wins when
    present. ``raw`` is text of at most RAW_LIMIT chars, encoded as strict
    UTF-8: a lone surrogate has no bytes, so it is refused rather than sent to
    orcha as '?' or U+FFFD (review VP-1)."""
    b64 = body.get("raw_b64")
    if b64 is not None:
        if not isinstance(b64, str) or len(b64) > 4 * RAW_LIMIT:
            return None
        try:
            return base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError):
            return None
    raw = body.get("raw")
    if not isinstance(raw, str) or len(raw) > RAW_LIMIT:
        return None
    try:
        return raw.encode("utf-8")
    except UnicodeEncodeError:
        return None


def sdk_verify(ctx, body):
    """POST /api/sdk/verify {raw} | {raw_b64}."""
    data = sdk_bytes(body if isinstance(body, dict) else {})
    if data is None:
        return 400, {"ok": False, "error": "bad raw"}
    pc, code, first = _orcha_verify(ctx, "-", inp=data)
    ctx.log("verify", pc_exit=code, source="sdk", valid=bool(pc and pc.get("valid")),
            sha256=hashlib.sha256(data).hexdigest())
    return 200, {"ok": True, "pc": pc, "pc_exit": code, "stderr": first}


# ---------------------------------------------------------------- meta

def _version(binary):
    try:
        r = subprocess.run([binary, "--version"], capture_output=True, text=True,
                           errors="replace", timeout=10)
    except (OSError, subprocess.SubprocessError):
        return {"bin": binary, "version": "", "ok": False}
    m = VERSION_RE.search((r.stdout or "") + " " + (r.stderr or ""))
    if r.returncode != 0 or not m:
        return {"bin": binary, "version": "", "ok": False}
    return {"bin": binary, "version": m.group(1), "ok": True}


def versions(ctx):
    """Read once (first use) and keep."""
    with ctx.lock:
        if ctx._versions is not None:
            return ctx._versions
    v = {"metabot": _version(ctx.metabot), "orcha": _version(ctx.orcha)}
    with ctx.lock:
        ctx._versions = v
    return v


def vnc_up(addr=("127.0.0.1", 5900)):
    try:
        with socket.create_connection(addr, timeout=0.2):
            return True
    except OSError:
        return False


DISPLAY_UNITS = ("metachat-gui", "fc-gui")


def active_unit(ctx, units=DISPLAY_UNITS):
    """21.4: which virtual-display unit is active (``systemctl --user is-active``,
    a read), the first of ``units`` that is, else None. Cached 5 s. A machine
    without systemctl answers None."""
    now = time.time()
    with ctx.lock:
        if now - ctx._unit[0] < 5.0:
            return ctx._unit[1]
    found = None
    try:
        r = subprocess.run(["systemctl", "--user", "is-active", *units], capture_output=True,
                           text=True, timeout=3)
        states = (r.stdout or "").split()
        for unit, state in zip(units, states):
            if state == "active":
                found = unit
                break
    except (OSError, subprocess.SubprocessError):
        found = None
    with ctx.lock:
        ctx._unit = [now, found]
    return found


def build_id(here):
    """21.4: the short git SHA of the tree that serves, read once at start from
    ``.git`` (a directory, or a worktree's ``gitdir:`` file) -> HEAD -> the ref
    in the common dir or ``packed-refs``; else the ``BUILD`` file deploy.sh
    writes; else "dev". Never raises."""
    here = Path(here)
    try:
        sha = _git_head(here)
        if sha:
            return sha[:7]
    except (OSError, ValueError):
        pass
    try:
        text = (here / "BUILD").read_text(encoding="utf-8").strip()
        if re.fullmatch(r"[0-9a-f]{7,40}", text):   # deploy.sh writes `git rev-parse --short HEAD`
            return text[:12]
    except (OSError, UnicodeDecodeError):
        pass
    return "dev"


def _git_head(here):
    dot = here / ".git"
    if dot.is_file():
        line = dot.read_text(encoding="utf-8").strip()
        if not line.startswith("gitdir:"):
            return None
        gitdir = Path(line[7:].strip())
        if not gitdir.is_absolute():
            gitdir = (here / gitdir).resolve()
    elif dot.is_dir():
        gitdir = dot
    else:
        return None
    common = gitdir
    cd = gitdir / "commondir"
    if cd.is_file():
        rel = cd.read_text(encoding="utf-8").strip()
        common = Path(rel) if os.path.isabs(rel) else (gitdir / rel).resolve()
    head = (gitdir / "HEAD").read_text(encoding="utf-8").strip()
    if not head.startswith("ref:"):
        return head if re.fullmatch(r"[0-9a-f]{40}", head) else None
    ref = head[4:].strip()
    for base in (gitdir, common):
        f = base / ref
        if f.is_file():
            sha = f.read_text(encoding="utf-8").strip()
            if re.fullmatch(r"[0-9a-f]{40}", sha):
                return sha
    packed = common / "packed-refs"
    if packed.is_file():
        for ln in packed.read_text(encoding="utf-8").splitlines():
            parts = ln.split()
            if len(parts) == 2 and parts[1] == ref and re.fullmatch(r"[0-9a-f]{40}", parts[0]):
                return parts[0]
    return None
