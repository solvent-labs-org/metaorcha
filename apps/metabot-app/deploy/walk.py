#!/usr/bin/env python3
"""deploy/walk.py — walk contract 25's journeys against the SANDBOX (deploy/sandbox.sh up).

Real CLIs, a real tmux server (the sandbox's own, -L mb-sandbox), real MetaBot
(the sandbox's own home and key); nothing touches the live app, the default tmux
server, ~/.metabot or a chat inbox. Sign-ins stop at the link: no login is
ever completed and no code is ever sent. The engine for runs is a bridge made
from MetaBot's own template, so a walk costs nothing.

    python3 -I deploy/walk.py            # every step
    python3 -I deploy/walk.py J2 J7      # some

Prints one PASS/FAIL line per check (never a token, a link's query or a key).
Exit 0 only when every check passed.
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(os.environ.get("MB_SANDBOX") or Path.home() / ".local/state/metachat-sandbox")
PORT = int(os.environ.get("MB_SANDBOX_PORT") or 8471)
HOST = f"127.0.0.1:{PORT}"
PROJECT = str(ROOT / "projects/sandbox")
NODE = str(Path.home() / ".nvm/versions/node/v20.20.2/bin/node")
HERE = Path(__file__).resolve().parent.parent
SOCK = os.environ.get("MB_SANDBOX_SOCK") or "mb-sandbox"
SIGNIN_HOSTS = ("claude.com", "claude.ai", "platform.claude.com", "console.anthropic.com", "cursor.com")

RESULTS = []
MADE = []   # desks this walk opened; all are killed at the end


def token():
    return (ROOT / "state/token").read_text().strip()


def call(method, path, body=None, timeout=90):
    sep = "&" if "?" in path else "?"
    url = f"http://{HOST}{path}{sep}t={token()}"
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Host", HOST)
    if method != "GET":
        req.add_header("Content-Type", "application/json")
        req.add_header("X-MetaChat", "1")
        req.add_header("Origin", f"http://{HOST}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            status = r.status
    except urllib.error.HTTPError as e:
        raw, status = e.read(), e.code
    try:
        return status, json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return status, raw


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail else ""), flush=True)
    return bool(ok)


def pane(name, lines=60):
    r = subprocess.run(["/usr/bin/tmux", "-L", SOCK, "capture-pane", "-p", "-J", "-S", f"-{lines}",
                        "-t", f"=fc-{name}:"], capture_output=True, text=True, timeout=5)
    return r.stdout


def type_in(name, text):
    """The person typing in a sandbox desk (an approval MetaBot asks for at its terminal)."""
    subprocess.run(["/usr/bin/tmux", "-L", SOCK, "send-keys", "-t", f"=fc-{name}:", "-l", text],
                   check=True, timeout=5)
    subprocess.run(["/usr/bin/tmux", "-L", SOCK, "send-keys", "-t", f"=fc-{name}:", "Enter"],
                   check=True, timeout=5)


def desk_row(name):
    st, d = call("GET", "/api/desks")
    rows = d if isinstance(d, list) else (d.get("desks") if isinstance(d, dict) else None) or []
    for r in rows:
        if isinstance(r, dict) and r.get("name") == name:
            return r
    return None


def wait(pred, secs=40, step=1.0):
    end = time.time() + secs
    while time.time() < end:
        v = pred()
        if v:
            return v
        time.sleep(step)
    return None


def finished(name, secs=60):
    """A catalogue desk prints '[metachat] exit N' when its command ends."""
    def done():
        m = re.findall(r"\[metachat\] exit (\d+)", pane(name))
        return m[-1] if m else None
    return wait(done, secs)


def needs(name, secs=30):
    return wait(lambda: (desk_row(name) or {}).get("needs_you"), secs)


def cli_desk(cid, params, label=None, cwd=PROJECT):
    body = {"id": cid, "params": params, "cwd": cwd}
    if label:
        body["label"] = label
    st, d = call("POST", "/api/cli/desk", body)
    name = d.get("name") if isinstance(d, dict) else None
    if name:
        MADE.append(name)
    return st, d, name


def signin_step(name):
    r = desk_row(name) or {}
    s = r.get("signin") or {}
    return s


def host_of(url):
    m = re.match(r"https://([^/]+)/", url or "")
    return m.group(1) if m else None


# ------------------------------------------------------------------ journeys

def j0():
    st, m = call("GET", "/api/meta")
    check("J0 meta answers", st == 200 and isinstance(m, dict), f"build {m.get('build') if isinstance(m, dict) else '?'}")
    check("J0 meta.default_folder is the sandbox project (B4)", isinstance(m, dict) and m.get("default_folder") == PROJECT,
          str(m.get("default_folder") if isinstance(m, dict) else None))


def j3():
    st, d = call("POST", "/api/spawn", {"kind": "shell", "label": "w-shell"})
    if st == 200:
        MADE.append("w-shell")
    check("J3 a shell desk opens", st == 200, str(d))
    st, d = call("POST", "/api/spawn", {"kind": "claude", "label": "w-claude"})
    if st == 200:
        MADE.append("w-claude")
    check("J3 a claude desk without a folder opens", st == 200, str(d))
    if st == 200:
        r = subprocess.run(["/usr/bin/tmux", "-L", SOCK, "display-message", "-p", "-t", "=fc-w-claude:",
                            "#{pane_current_path}"], capture_output=True, text=True, timeout=5)
        check("J3 ...and it opened in the default folder, not home (B4)", r.stdout.strip() == PROJECT, r.stdout.strip())


def j2():
    st, d = call("POST", "/api/accounts", {"engine": "claude", "name": "wsb"})
    check("J2 a Claude login is added", st in (200, 409), str(d))
    st, d = call("POST", "/api/spawn", {"kind": "claude", "signin": True})
    check("J2 sign-in without an account is refused (B1)", st == 400, str(d))
    st, d = call("POST", "/api/spawn", {"kind": "claude", "account": "wsb", "signin": True})
    name = d.get("name") if isinstance(d, dict) else None
    if name:
        MADE.append(name)
    check("J2 a Claude sign-in desk opens", st == 200 and bool(name), str(d))
    if not name:
        return
    st2, d2 = call("POST", "/api/spawn", {"kind": "claude", "account": "wsb", "signin": True})
    check("J2 a second sign-in desk for the same login is refused (B1)", st2 == 409, str(d2))
    s = wait(lambda: signin_step(name).get("step") == "style" and signin_step(name), 40)
    check("J2 step 'style' is read off the theme picker", bool(s), str(signin_step(name).get("step")))
    check("J2 ...and needs_you is set", bool((desk_row(name) or {}).get("needs_you")))
    st, d = call("POST", "/api/keys", {"name": name, "keys": ["enter"]})
    check("J2 Enter through /api/keys (B3)", st == 200, str(d))
    s = wait(lambda: signin_step(name).get("step") == "method", 20)
    check("J2 step 'method' (Select login method)", bool(s), str(signin_step(name).get("step")))
    call("POST", "/api/keys", {"name": name, "key": "enter"})
    s = wait(lambda: signin_step(name).get("step") == "code" and signin_step(name).get("url"), 30)
    url = signin_step(name).get("url")
    check("J2 step 'code' with a joined sign-in link", bool(s), str(signin_step(name).get("step")))
    check("J2 ...the link's host is on the sign-in list", host_of(url) in SIGNIN_HOSTS, str(host_of(url)))
    check("J2 ...and the link is whole (no spaces, has a code_challenge)", bool(url) and " " not in url and "code_challenge=" in url)
    st, d = call("POST", "/api/keys", {"name": name, "text": "x" * 513})
    check("J2 a 513-character code is refused (B3)", st == 400, str(d))
    # no code is ever sent: the walk ends the sign-in here
    st, d = call("POST", "/api/kill", {"name": name, "confirm": True})
    check("J2 the sign-in desk is closed", st == 200, str(d))


def j2c():
    st, d = call("POST", "/api/accounts", {"engine": "cursor", "name": "wsc"})
    check("J2c a Cursor folder is added", st in (200, 409), str(d))
    st, a = call("GET", "/api/accounts")
    row = next((x for x in (a.get("cursor") or []) if x.get("name") == "wsc"), None) if isinstance(a, dict) else None
    check("J2c the folder reads not ready (B2)", row is not None and row.get("ready") is False, str(row))
    check("J2c defaults are reported (B2)", isinstance(a, dict) and isinstance(a.get("defaults"), dict), str(a.get("defaults") if isinstance(a, dict) else a))
    st, d = call("POST", "/api/spawn", {"kind": "cursor", "account": "wsc", "signin": True})
    name = d.get("name") if isinstance(d, dict) else None
    if name:
        MADE.append(name)
    check("J2c a Cursor sign-in desk opens", st == 200 and bool(name), str(d))
    if not name:
        return
    s = wait(lambda: signin_step(name).get("step") == "link" and signin_step(name).get("url"), 40)
    url = signin_step(name).get("url")
    check("J2c step 'link' with the cursor.com link", bool(s) and host_of(url) == "cursor.com", str(host_of(url)))
    st, d = call("POST", "/api/keys", {"name": name, "text": "abc"})
    check("J2c text is refused while the step is not 'code' (B3: 409)", st == 409, str(d))
    st, d = call("POST", "/api/keys", {"name": name, "keys": ["y", "enter", "y", "enter", "y"]})
    check("J2c five keys are refused (B3)", st == 400, str(d))
    st, d = call("POST", "/api/keys", {"name": name, "key": "rm"})
    check("J2c a key off the list is refused (B3)", st == 400, str(d))
    st, d = call("POST", "/api/kill", {"name": name, "confirm": True})
    check("J2c the sign-in desk is closed", st == 200, str(d))


def j7():
    st, d, n1 = cli_desk("plugin.new-bridge", {"name": "walk-bridge"})
    check("J7 Make → Bridge opens a desk (B7)", st == 200 and bool(n1), str(d))
    check("J7 ...the bridge is written", n1 and finished(n1) == "0" and (Path(PROJECT) / "walk-bridge/bin/walk-bridge").is_file())
    st, d, n2 = cli_desk("plugin.marketplace.add", {"name": "walk-bridge", "folder": f"{PROJECT}/walk-bridge"})
    check("J7 Add as marketplace", st == 200 and n2 and finished(n2) == "0", str(d))
    st, d, n3 = cli_desk("plugin.install", {"ref": "walk-bridge@walk-bridge"})
    check("J7 Install opens a desk", st == 200 and bool(n3), str(d))
    if n3:
        check("J7 ...it waits on '[y/N]' and needs_you sees it", bool(needs(n3)))
        st, d = call("POST", "/api/keys", {"name": n3, "keys": ["y", "enter"]})
        check("J7 ...y + Enter through /api/keys", st == 200, str(d))
        check("J7 ...installed", finished(n3) == "0")
    st, d, n4 = cli_desk("plugin.enable-bridges", {"name": "walk-bridge"})
    check("J7 Enable as an engine", st == 200 and n4 and finished(n4) == "0", str(d))
    st, x = call("GET", "/api/extend?fresh=1")
    p = next((q for q in (x.get("plugins") or []) if q.get("name") == "walk-bridge"), None) if isinstance(x, dict) else None
    check("J7 /api/extend lists the plugin, pinned (B6)", p is not None and re.fullmatch(r"[0-9a-f]{12,64}", str(p.get("tree") or "")), str(p))
    check("J7 ...with its bridge on", bool(p) and p.get("bridges_on") is True and "walk-bridge" in (p.get("bridges") or []), str(p))
    e = next((q for q in (x.get("engines") or []) if q.get("name") == "walk-bridge"), None) if isinstance(x, dict) else None
    check("J7 ...and the bridge is an engine", e is not None and e.get("kind") == "bridge", str(e))


def run_ids():
    st, r = call("GET", "/api/mb/runs?limit=50")
    rows = (r.get("runs") or []) if isinstance(r, dict) else []
    return {row.get("run_id") for row in rows}


def j5():
    before = run_ids()
    st, d = call("POST", "/api/run", {"task": "walk: say hi", "engine": "walk-bridge", "cwd": PROJECT, "mode": "read-only"})
    name = d.get("name") if isinstance(d, dict) else None
    if name:
        MADE.append(name)
    check("J5 a run on the bridge starts", st == 200 and bool(name), str(d))

    def done_row():
        st, r = call("GET", "/api/mb/runs?limit=20")
        rows = (r.get("runs") or []) if isinstance(r, dict) else []
        for row in rows:   # the new done row, sealed on the bridge
            if row.get("run_id") not in before and row.get("state") == "done" and "walk-bridge" in json.dumps(row.get("row") or row):
                return row
        return None
    row = wait(done_row, 60, 2)
    check("J5 the run is done with a valid receipt", bool(row) and row.get("receipt_state") == "valid",
          str(row and (row.get("run_id"), row.get("receipt_state"))))
    if not row:
        return
    rid = row["run_id"]
    st, v = call("POST", "/api/mb/verify", {"id": rid})
    pc = (v.get("pc") or {}) if isinstance(v, dict) else {}
    check("J5 the PC check verifies", st == 200 and pc.get("valid") is True and v.get("index_sha_ok") is True, str(pc.get("checks")))
    sep = "?"
    req = urllib.request.Request(f"http://{HOST}/api/mb/receipt/{rid}{sep}t={token()}", headers={"Host": HOST})
    raw = urllib.request.urlopen(req, timeout=20).read()
    probe = HERE / "deploy" / ".walk-verify.mjs"
    probe.write_text("import { readFileSync } from 'node:fs';\n"
                     f"const M = await import({json.dumps(str(HERE / 'vendor/mbverify.js'))});\n"
                     "const r = await M.verify(new Uint8Array(readFileSync(0)));\n"
                     "console.log(JSON.stringify({valid: r.valid, problem: r.problem}));\n")
    try:
        out = subprocess.run([NODE, str(probe)], input=raw, capture_output=True, timeout=30).stdout.decode()
    finally:
        probe.unlink()
    tv = json.loads(out.strip().splitlines()[-1]) if out.strip() else {}
    check("J5 the tablet verifier agrees", tv.get("valid") is True, str(tv))


def j6():
    st, d = call("POST", "/api/cli/read", {"id": "propose", "params": {"folder": PROJECT, "task": "fix the failing test"}})
    out = d.get("stdout") if isinstance(d, dict) else None
    try:
        prop = json.loads(out or "")
    except ValueError:
        prop = None
    check("J6 Propose returns a draft (B7)", st == 200 and isinstance(prop, dict) and prop.get("shape") in ("ask", "ladder", "office"),
          str(prop.get("shape") if isinstance(prop, dict) else d))
    st, d, n1 = cli_desk("workflow.new", {"name": "walk-wf", "engine": "walk-bridge", "folder": PROJECT, "prompt": "walk: the workflow step"})
    check("J6 Keep as a workflow (on the bridge)", st == 200 and n1 and finished(n1) == "0", str(d))
    st, d, n2 = cli_desk("workflow.approve", {"ref": "walk-wf"})
    check("J6 Approve opens a desk", st == 200 and bool(n2), str(d))
    if n2:
        check("J6 ...it asks at the terminal (needs_you)", bool(needs(n2)))
        text = pane(n2)
        word = "yes" if re.search(r"type 'yes'|type yes", text, re.I) else "y"
        type_in(n2, word)   # the person signs, in the sandbox desk
        check("J6 ...approved", finished(n2) == "0", text.strip().splitlines()[-1][:120] if text.strip() else "")
    st, d, n3 = cli_desk("workflow.run", {"ref": "walk-wf"})
    check("J6 Run now", st == 200 and n3 and finished(n3, 90) == "0", str(d))


def j8():
    st, d = call("POST", "/api/modules/new", {"id": "walk-mod", "label": "Walk module"})
    check("J8 New module writes a starter manifest (B8)", st == 200 and isinstance(d, dict) and d.get("ok") and Path(str(d.get("file"))).is_file(), str(d))
    st, d = call("POST", "/api/modules/new", {"id": "walk-mod", "label": "Walk module"})
    check("J8 ...a second one with the same id is refused", st == 409, str(d))
    st, m = call("GET", "/api/modules")
    mods = m.get("modules") if isinstance(m, dict) else m
    check("J8 ...and it loads", isinstance(mods, list) and any(isinstance(x, dict) and x.get("id") == "walk-mod" for x in mods), "")


def j9():
    st, d, n1 = cli_desk("serve", {}, cwd=PROJECT)
    check("J9 Start serve opens a desk (B7)", st == 200 and bool(n1), str(d))
    st, x = call("GET", "/api/extend?fresh=1")
    check("J9 /api/extend says serve runs", isinstance(x, dict) and (x.get("serve") or {}).get("running") is True, str(x.get("serve") if isinstance(x, dict) else x))
    st, d = call("POST", "/api/cli/desk", {"id": "serve", "params": {}, "cwd": PROJECT})
    if st == 200 and isinstance(d, dict) and d.get("name"):
        MADE.append(d["name"])
    check("J9 a second serve is refused", st == 409, str(d))


STEPS = {"J0": j0, "J3": j3, "J2": j2, "J2c": j2c, "J7": j7, "J5": j5, "J6": j6, "J8": j8, "J9": j9}


def main(argv):
    want = argv or list(STEPS)
    try:
        for k in want:
            print(f"--- {k}", flush=True)
            try:
                STEPS[k]()
            except Exception as exc:   # one journey's crash is its failure, not the walk's
                check(f"{k} ran to the end", False, f"{type(exc).__name__}: {exc}")
    finally:
        for name in reversed(MADE):
            call("POST", "/api/kill", {"name": name, "confirm": True})
    bad = [n for n, ok in RESULTS if not ok]
    print(f"\nwalk: {len(RESULTS) - len(bad)} passed, {len(bad)} failed")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
