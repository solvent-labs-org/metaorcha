"""``metabot app``: the web UI in a window of its own, and a launcher that opens it.

The UI is served as ``metabot ui`` serves it, with a token made at this start, the same
Host and Origin checks and the same form tokens. A Chromium-family browser on PATH opens
its address in an ``--app=`` window, which has no tabs and no address bar. The address,
token and all, goes to the browser on its command line and never to this command's
output, which a launcher hands to the session's log. With no such browser the address is
printed, as ``metabot ui`` prints it, when there is a terminal to print it to.

``--install`` writes a freedesktop.org launcher, with the mark as its icon, that runs
``metabot app``. There is no web manifest and no fixed port: a browser fetches a manifest
without the cookie, and an installed start address would carry no token.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

from metabot.web_commands import (
    EXIT_FAILED,
    EXIT_OK,
    EXIT_USAGE,
    LOOPBACK,
    address,
    port_problem,
    serve_ui,
)
from metabot.web_guard import Guard

# Browsers that open an address as an app window, by their commands, in the order tried.
BROWSERS = (
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "brave-browser",
    "microsoft-edge",
    "microsoft-edge-stable",
)
MAC_BROWSERS = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
)
NO_BROWSER = (
    "metabot app: no Chromium-family browser here to open a window in (google-chrome, "
    "chromium, brave-browser or microsoft-edge); run metabot ui in a terminal and open "
    "the address it prints in any browser"
)
# A launcher's command, as the Desktop Entry spec reads it: these must be quoted.
RESERVED = frozenset(" \t\n\"'\\><~|&;$*?#()`")


def _err(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


def _tty() -> bool:
    return sys.stdout.isatty()


def find_browser() -> str | None:
    """A browser that opens an app window: the path of the first one found, or None."""
    for name in BROWSERS:
        found = shutil.which(name)
        if found:
            return found
    if sys.platform == "darwin":
        return next((path for path in MAC_BROWSERS if os.access(path, os.X_OK)), None)
    return None


def window_argv(browser: str, url: str) -> list[str]:
    return [browser, f"--app={url}"]


def open_window(browser: str, url: str) -> bool:
    """Open ``url`` in an app window of ``browser``, apart from this command; False when the
    browser does not start. Its output goes nowhere, so the address is never logged."""
    try:
        proc = subprocess.Popen(
            window_argv(browser, url),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        _err(f"metabot app: {browser} did not start: {exc.strerror or exc}")
        return False
    threading.Thread(target=proc.wait, daemon=True).start()  # reaped when it closes
    return True


def cmd_app(args: argparse.Namespace) -> int:
    if args.install:
        return install()
    problem = port_problem(args.port)
    if problem:
        _err(problem)
        return EXIT_USAGE
    browser = find_browser()
    if browser is None and not _tty():  # the address would go to a log, token and all
        _err(NO_BROWSER)
        return EXIT_FAILED

    def ready(guard: Guard) -> bool:
        if browser is None:
            print(f"metabot app: {address(guard)}", flush=True)
            _err("[metabot] no window to open: open the address in a browser; stop with Ctrl-C")
            return True
        if not open_window(browser, address(guard)):
            _err("metabot app: no window opened; run metabot ui in a terminal instead")
            return False
        print(f"metabot app: the window is open on http://{LOOPBACK}:{guard.port}/", flush=True)
        _err("[metabot] the window works until this stops; stop it with Ctrl-C")
        return True

    return serve_ui(args.port, ready)


# ── the launcher ────────────────────────────────────────────────────────────


def data_home() -> Path:
    """Where a person's launchers and icons go: ``XDG_DATA_HOME``, when it is a full path."""
    named = os.environ.get("XDG_DATA_HOME", "")
    return Path(named) if os.path.isabs(named) else Path.home() / ".local" / "share"


def _plain(value: str) -> str:
    """``value`` as a Desktop Entry string; ValueError for one no launcher can hold."""
    if any(ord(c) < 0x20 or c == "\x7f" for c in value):
        raise ValueError(f"{value!r} holds a line break or another control character")
    return value.replace("\\", "\\\\")


def _arg(word: str) -> str:
    """One word of a launcher's command: quoted when the spec says it must be, with ``%``
    doubled; its backslashes are then escaped again, as every Desktop Entry string's are."""
    _plain(word)
    word = word.replace("%", "%%")
    if RESERVED.isdisjoint(word):
        return word
    inner = "".join(f"\\{c}" if c in '"`$\\' else c for c in word)
    return f'"{inner}"'.replace("\\", "\\\\")


def exec_line(argv: list[str]) -> str:
    return " ".join(map(_arg, argv))


def launcher(executable: str, icon: Path) -> str:
    """The launcher: it runs this Python's ``metabot app``, isolated from the folder it
    starts in, so a ``metabot`` folder there is never what it imports."""
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Version=1.5\n"
        "Name=MetaBot\n"
        "Comment=Run your work on the plan you already have, with a signed receipt of each run\n"
        f"TryExec={_plain(executable)}\n"
        f"Exec={exec_line([executable, '-I', '-m', 'metabot', 'app'])}\n"
        f"Icon={_plain(str(icon))}\n"
        "Terminal=false\n"
        "Categories=Development;\n"
        "StartupNotify=true\n"
    )


def install() -> int:
    """Write the launcher and its icon; elsewhere than a freedesktop.org desktop, print the
    command instead."""
    from metabot.paths import write_private
    from metabot.web_theme import icon

    if sys.platform == "darwin" or sys.platform.startswith("win"):
        print("metabot app: no launcher is written on this system yet; open the window with:")
        print("  metabot app")
        return EXIT_OK
    if find_browser() is None:
        _err(NO_BROWSER)
        return EXIT_FAILED
    share = data_home()
    icon_path = share / "icons" / "hicolor" / "scalable" / "apps" / "metabot.svg"
    desktop = share / "applications" / "metabot.desktop"
    try:
        entry = launcher(sys.executable, icon_path)
    except ValueError as exc:
        _err(f"metabot app: no launcher can run this Python: {exc}")
        return EXIT_FAILED
    try:
        write_private(icon_path, icon())
        write_private(desktop, entry)
    except OSError as exc:
        _err(f"metabot app: cannot write the launcher: {exc}")
        return EXIT_FAILED
    print(f"metabot app: wrote {desktop}")
    print(f"metabot app: wrote {icon_path}")
    print("next      open MetaBot from your applications menu; it runs metabot app")
    return EXIT_OK
