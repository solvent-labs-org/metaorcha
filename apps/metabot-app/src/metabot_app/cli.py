"""``metabot-app``: start the app server and look after it.

metabot-app serve                 start the server
metabot-app pair [--port N]       show a pairing code for a phone (the server must be running)
metabot-app devices [--remove ID] list paired phones, or remove one
metabot-app doctor [--json]       what this machine has for a first run; exit 1 on a blocker
metabot-app send <to> -m <text>   message a desk (or 'tablet' for the person); a desk's
                                  own env (METACHAT_URL/METACHAT_STATE) names the server
metabot-app service install|remove|status [--kind systemd|launchd]
                                  keep the server running across logins (no root)
metabot-app --version
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

from . import __version__


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="metabot-app", description="MetaBot, the app.")
    ap.add_argument("--version", action="version", version=f"metabot-app {__version__}")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("serve", help="start the app server")
    pair = sub.add_parser("pair", help="show a pairing code for a phone")
    pair.add_argument(
        "--port",
        type=int,
        help="the running server's loopback port (default: the config's)",
    )
    dev = sub.add_parser("devices", help="list paired phones")
    dev.add_argument(
        "--port",
        type=int,
        help="the running server's loopback port (default: the config's)",
    )
    dev.add_argument(
        "--remove", metavar="ID", help="remove this device; its token stops working"
    )
    doc = sub.add_parser("doctor", help="what this machine has for a first run")
    doc.add_argument(
        "--json", action="store_true", help="the report as one JSON object"
    )
    snd = sub.add_parser("send", help="message a desk (or 'tablet' for the person)")
    snd.add_argument("to", help="the desk's name, or 'tablet' for the person")
    snd.add_argument("-m", "--message", required=True, help="the message text")
    snd.add_argument(
        "--from",
        dest="frm",
        default=None,
        help="the sender's name (default: this desk's mailbox, else the shell's)",
    )
    svc = sub.add_parser("service", help="keep the server running across logins")
    svc.add_argument("action", choices=("install", "remove", "status"))
    svc.add_argument(
        "--kind",
        default="auto",
        choices=("auto", "systemd", "launchd"),
        help="the service manager (default: the host's)",
    )
    return ap


class _Api:
    """Loopback calls to the running server with the desktop token. No server is started."""

    def __init__(self, port):
        from . import server

        cfg = server.load_config()
        import os
        from pathlib import Path
        from urllib.parse import urlsplit

        state = Path(os.environ.get("METACHAT_STATE") or cfg["state_dir"]).expanduser()
        self.port = port or int(cfg["port"])
        # 11-12 (D2): a desk's env names its server (spawn_argv sets METACHAT_URL);
        # an explicit --port wins over it; else the config's loopback port
        env_url = os.environ.get("METACHAT_URL")
        self.base = env_url if (env_url and not port) else f"http://127.0.0.1:{self.port}"
        self.host = urlsplit(self.base).netloc
        try:
            self.token = (state / "token").read_text().strip()
        except OSError:
            self.token = ""

    def call(self, method, path, body=None):
        url = f"{self.base}{path}?t={self.token}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Host": self.host,
                "Content-Type": "application/json",
                "X-MetaChat": "1",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b"{}")
            except ValueError:
                return e.code, {}
        except (urllib.error.URLError, OSError, ValueError) as e:
            print(
                f"metabot-app: the server is not running on {self.host} ({e.reason if hasattr(e, 'reason') else e})",
                file=sys.stderr,
            )
            return None, None


def _pair(port) -> int:
    api = _Api(port)
    status, reply = api.call("POST", "/api/pair/start", {})
    if status is None:
        return 1
    if status != 200:
        print(f"metabot-app: {reply.get('error', status)}", file=sys.stderr)
        return 1
    code = reply["code"]
    addrs = reply["addresses"] or ["<this computer's address>"]
    print("Pair a phone (MetaBot for Android):")
    print(
        f"  Address: {addrs[0]}:{reply['port']}"
        + (f"   (also {', '.join(addrs[1:])})" if len(addrs) > 1 else "")
    )
    print(
        f"  Code:    {code[:4]} {code[4:]}        good for {reply['expires_in'] // 60} minutes"
    )
    print(f"  Words:   {' '.join(reply['words'])}")
    print(
        "  The phone shows four words after it reads the address. Say Yes there only if they match these."
    )
    print(f"  QR text: {reply['payload']}")
    return 0


def _devices(port, remove) -> int:
    api = _Api(port)
    if remove:
        status, reply = api.call("POST", "/api/devices/revoke", {"id": remove})
        if status is None:
            return 1
        if status != 200:
            print(f"metabot-app: {reply.get('error', status)}", file=sys.stderr)
            return 1
        print(f"removed {remove}")
        return 0
    status, reply = api.call("GET", "/api/devices")
    if status is None:
        return 1
    if status != 200:
        print(f"metabot-app: {reply.get('error', status)}", file=sys.stderr)
        return 1
    rows = reply.get("devices") or []
    if not rows:
        print("no paired phones")
        return 0
    for d in rows:
        print(
            f"{d['id']}  {d['name']}  ({d['scope']}; seen: {'yes' if d.get('seen') else 'never'})"
        )
    return 0


def _doctor(as_json: bool) -> int:
    """No server needed: the same report GET /api/doctor gives, from this process."""
    from . import doctor, server

    server.configure()
    rep = doctor.report(server.MB, server.on_device())
    if as_json:
        print(json.dumps(rep, indent=2))
    else:
        for i in rep["items"]:
            mark = "ok  " if i["ok"] else ("FAIL" if i["blocker"] else "note")
            print(f"{mark} {i['id']:<14} {i['what']}")
            if i["fix"]:
                print(f"     {'':<14} fix: {i['fix']}")
        print("all well" if rep["ok"] else "a blocker stops the first run")
    return 0 if rep["ok"] else 1


def _send(to: str, message: str, frm: str | None) -> int:
    """``metabot-app send <to> -m <text>``: the reply command a desk is told to use
    (server.reply_cmd). The wheel ships it, so every install has it on PATH — the
    repo's bin/mc-send was named to desks that had none (11-11, the tablet)."""
    import os
    import socket

    frm = (
        frm
        or os.environ.get("METABOT_MAILBOX")
        or os.environ.get("METACHAT_DESK")
        or f"{socket.gethostname().split('.')[0]}-shell"
    )
    api = _Api(None)
    status, reply = api.call("POST", "/api/send", {"target": to, "text": message, "from": frm})
    if status is None:
        return 1
    if status == 200 and reply.get("ok"):
        print(f"sent to {to} via {reply.get('via')}")
        return 0
    print(f"not sent: {reply.get('error', status)}")
    return 1


def _service(action: str, kind: str) -> int:
    from . import service

    try:
        if action == "install":
            path = service.install(kind)
            print(f"installed {path}")
            if path.suffix == ".service":
                print(
                    f"  it is running now and after every login: systemctl --user status {service.UNIT}"
                )
                print(
                    "  to keep it running when you are logged out: loginctl enable-linger"
                )
            else:
                print(
                    f"  it is running now and after every login: launchctl list {service.LABEL}"
                )
            return 0
        if action == "remove":
            if service.remove(kind):
                print("removed; the server is no longer started at login")
                return 0
            print("metabot-app: not installed; nothing to remove", file=sys.stderr)
            print("  (metabot-app service install puts one in)", file=sys.stderr)
            return 1
        st = service.status(kind)
        print(
            f"{st['kind']}: {'installed' if st['installed'] else 'not installed'}"
            + (f", {st['active']}" if st["active"] else "")
            + f"  ({st['path']})"
        )
        return 0
    except service.ServiceError as exc:
        print(f"metabot-app: {exc}", file=sys.stderr)
        return 2


def main(argv: list[str] | None = None) -> int:
    ap = _parser()
    args = ap.parse_args(argv)
    if args.cmd is None:
        ap.print_usage(sys.stderr)
        return 2
    if args.cmd == "serve":
        from . import server

        return int(server.main() or 0)
    if args.cmd == "pair":
        return _pair(args.port)
    if args.cmd == "devices":
        return _devices(args.port, args.remove)
    if args.cmd == "doctor":
        return _doctor(args.json)
    if args.cmd == "send":
        return _send(args.to, args.message, args.frm)
    if args.cmd == "service":
        return _service(args.action, args.kind)
    return 2


if __name__ == "__main__":
    sys.exit(main())
