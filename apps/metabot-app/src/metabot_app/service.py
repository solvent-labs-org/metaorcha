"""D4: keep the app server running across logins. ``install`` writes a systemd user
unit (Linux) or a launchd agent (macOS) that runs ``<this python> -m metabot_app serve``
with the login shell's PATH captured (a user unit starts with a bare one, and the vendor
CLIs live in places only the login shell knows). ``remove`` and ``status`` undo and read
it. Nothing here needs root; nothing here is called by the server."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape

from . import paths

UNIT = "metabot-app.service"
LABEL = "ai.metaorcha.metabot-app"
KINDS = ("systemd", "launchd")


class ServiceError(Exception):
    pass


def kind_for(kind: str) -> str:
    if kind == "auto":
        if sys.platform.startswith("linux"):
            return "systemd"
        if sys.platform == "darwin":
            return "launchd"
        raise ServiceError(
            "no service manager for this system (Linux: systemd user units; macOS: launchd)"
        )
    if kind not in KINDS:
        raise ServiceError(f"unknown service kind {kind!r}; one of {', '.join(KINDS)}")
    return kind


def unit_text(python: str, path: str) -> str:
    return (
        "[Unit]\n"
        "Description=MetaBot, the app: the local server the page, the phone and the desks use\n"
        "After=network.target\n"
        "\n"
        "[Service]\n"
        f"ExecStart={python} -m metabot_app serve\n"
        f"Environment=PATH={path.replace('%', '%%')}\n"
        "Restart=on-failure\n"
        "RestartSec=3\n"
        "KillMode=process\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def plist_text(python: str, path: str, log: Path) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        "<dict>\n"
        "    <key>Label</key>\n"
        f"    <string>{LABEL}</string>\n"
        "    <key>ProgramArguments</key>\n"
        "    <array>\n"
        f"        <string>{escape(python)}</string>\n"
        "        <string>-m</string>\n"
        "        <string>metabot_app</string>\n"
        "        <string>serve</string>\n"
        "    </array>\n"
        "    <key>EnvironmentVariables</key>\n"
        "    <dict>\n"
        "        <key>PATH</key>\n"
        f"        <string>{escape(path)}</string>\n"
        "    </dict>\n"
        "    <key>RunAtLoad</key>\n"
        "    <true/>\n"
        "    <key>KeepAlive</key>\n"
        "    <true/>\n"
        "    <key>AbandonProcessGroup</key>\n"
        "    <true/>\n"
        "    <key>StandardOutPath</key>\n"
        f"    <string>{escape(str(log))}</string>\n"
        "    <key>StandardErrorPath</key>\n"
        f"    <string>{escape(str(log))}</string>\n"
        "</dict>\n"
        "</plist>\n"
    )


def unit_path(kind: str) -> Path:
    home = Path.home()
    if kind == "systemd":
        return home / ".config" / "systemd" / "user" / UNIT
    return home / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _python() -> str:
    return sys.executable


def _path() -> str:
    return paths.search_path()


def _run(argv) -> tuple[int, str]:
    try:
        r = subprocess.run(list(argv), capture_output=True, text=True, timeout=30)
        return r.returncode, r.stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise ServiceError(f"{argv[0]}: {exc}") from exc


def install(kind: str = "auto") -> Path:
    kind = kind_for(kind)
    target = unit_path(kind)
    target.parent.mkdir(parents=True, exist_ok=True)
    if kind == "systemd":
        text = unit_text(_python(), _path())
    else:
        log = Path.home() / "Library" / "Logs" / "metabot-app.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        text = plist_text(_python(), _path(), log)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.chmod(0o644)
    tmp.replace(target)
    if kind == "systemd":
        _run(["systemctl", "--user", "daemon-reload"])
        _run(["systemctl", "--user", "enable", "--now", UNIT])
    else:
        _run(["launchctl", "unload", str(target)])
        _run(["launchctl", "load", "-w", str(target)])
    return target


def remove(kind: str = "auto") -> bool:
    """True when a service was there and is gone; False when none was installed."""
    kind = kind_for(kind)
    target = unit_path(kind)
    if not target.exists():
        return False
    if kind == "systemd":
        _run(["systemctl", "--user", "disable", "--now", UNIT])
    else:
        _run(["launchctl", "unload", "-w", str(target)])
    target.unlink()
    if kind == "systemd":
        _run(["systemctl", "--user", "daemon-reload"])
    return True


def status(kind: str = "auto") -> dict:
    kind = kind_for(kind)
    target = unit_path(kind)
    installed = target.exists()
    active = ""
    if installed:
        if kind == "systemd":
            _, out = _run(["systemctl", "--user", "is-active", UNIT])
            active = out.strip()
        else:
            code, _ = _run(["launchctl", "list", LABEL])
            active = "active" if code == 0 else "inactive"
    return {"kind": kind, "installed": installed, "active": active, "path": str(target)}
