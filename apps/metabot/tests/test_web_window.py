"""``metabot app`` (APP): the web UI in a window of its own, and the launcher that opens it.

No browser is ever started here. The lookup and the launch are faked: a stand-in on PATH
that writes down what it was given, or a recorder in place of the launch.
"""

from __future__ import annotations

import os
import re
import select
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import httpx2
import pytest

from metabot import web_theme, web_window
from metabot.cli import main

URL = re.compile(r"http://127\.0\.0\.1:(\d+)/\?token=([A-Za-z0-9_-]{43})")


def _on_path(monkeypatch, folder: Path, *names: str, body: str = "exit 0\n") -> Path:
    """Stand-ins named ``names``, the only commands on PATH; none of them is a browser."""
    folder.mkdir(parents=True, exist_ok=True)
    for name in names:
        path = folder / name
        path.write_text(f"#!/bin/sh\n{body}", encoding="utf-8")
        path.chmod(0o755)
    monkeypatch.setenv("PATH", str(folder))
    return folder


class Launches:
    """Stands in for the launch: keeps each argv, starts nothing."""

    def __init__(self) -> None:
        self.argvs: list[list[str]] = []
        self.kwargs: list[dict] = []

    def __call__(self, argv, **kwargs):
        self.argvs.append(list(argv))
        self.kwargs.append(kwargs)

        class Done:
            def wait(self) -> int:
                return 0

        return Done()


def _ready(monkeypatch, *, tty: bool) -> list:
    """``serve_ui`` faked: the address is made and handed over, and nothing is served."""
    from metabot.web_guard import Guard

    handed: list = []

    def serve_ui(port, ready):
        guard = Guard("t" * 43, 47021)
        handed.append(ready(guard))
        return 0 if handed[-1] else 1

    monkeypatch.setattr(web_window, "serve_ui", serve_ui)
    monkeypatch.setattr(web_window, "_tty", lambda: tty)
    return handed


# ── finding a browser ───────────────────────────────────────────────────────


def test_a_chromium_family_browser_is_found_on_path(tmp_path, monkeypatch):
    bin_ = _on_path(monkeypatch, tmp_path / "bin", "chromium", "brave-browser")
    assert web_window.find_browser() == str(bin_ / "chromium")  # in the listed order
    _on_path(monkeypatch, tmp_path / "other", "firefox", "chrome-is-not-this")
    assert web_window.find_browser() is None


def test_the_window_is_the_token_address_as_an_app():
    url = "http://127.0.0.1:5000/?token=" + "x" * 43
    assert web_window.window_argv("/usr/bin/chromium", url) == ["/usr/bin/chromium", f"--app={url}"]


def test_the_window_opens_apart_from_this_command(monkeypatch):
    launches = Launches()
    monkeypatch.setattr(web_window.subprocess, "Popen", launches)
    assert web_window.open_window("/b/chromium", "http://127.0.0.1:1/?token=t") is True
    [argv] = launches.argvs
    assert argv == ["/b/chromium", "--app=http://127.0.0.1:1/?token=t"]
    [kwargs] = launches.kwargs
    assert kwargs["start_new_session"] is True
    assert {kwargs[k] for k in ("stdin", "stdout", "stderr")} == {subprocess.DEVNULL}


def test_a_browser_that_cannot_start_opens_no_window(monkeypatch):
    def refuse(argv, **kwargs):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(web_window.subprocess, "Popen", refuse)
    assert web_window.open_window("/b/chromium", "http://127.0.0.1:1/?token=t") is False


# ── metabot app ─────────────────────────────────────────────────────────────


def test_app_opens_the_window_and_never_prints_the_token(tmp_path, monkeypatch, capsys):
    _on_path(monkeypatch, tmp_path / "bin", "google-chrome")
    launches = Launches()
    monkeypatch.setattr(web_window.subprocess, "Popen", launches)
    handed = _ready(monkeypatch, tty=False)  # as a launcher starts it: no terminal
    assert main(["app"]) == 0 and handed == [True]
    [argv] = launches.argvs
    assert argv[0] == str(tmp_path / "bin" / "google-chrome")
    assert argv[1] == f"--app=http://127.0.0.1:47021/?token={'t' * 43}"
    said = capsys.readouterr()
    assert "t" * 43 not in said.out + said.err
    assert "127.0.0.1:47021" in said.out and "Ctrl-C" in said.err


def test_app_with_no_browser_prints_the_address_in_a_terminal(tmp_path, monkeypatch, capsys):
    _on_path(monkeypatch, tmp_path / "bin", "firefox")
    handed = _ready(monkeypatch, tty=True)
    assert main(["app"]) == 0 and handed == [True]
    out = capsys.readouterr().out
    assert URL.search(out) and out.startswith("metabot app: http://127.0.0.1:47021/?token=")


def test_app_with_no_browser_and_no_terminal_keeps_the_token_out_of_its_log(
    tmp_path, monkeypatch, capsys
):
    _on_path(monkeypatch, tmp_path / "bin", "firefox")
    handed = _ready(monkeypatch, tty=False)
    assert main(["app"]) == 1 and handed == []  # refused before the UI starts
    said = capsys.readouterr()
    assert "token" not in said.out + said.err and "metabot ui" in said.err


def test_app_whose_browser_does_not_start_keeps_the_token_out_of_its_log(
    tmp_path, monkeypatch, capsys
):
    _on_path(monkeypatch, tmp_path / "bin", "chromium")

    def refuse(argv, **kwargs):
        raise OSError(2, "No such file or directory")

    monkeypatch.setattr(web_window.subprocess, "Popen", refuse)
    handed = _ready(monkeypatch, tty=False)
    assert main(["app"]) == 1 and handed == [False]
    said = capsys.readouterr()
    assert "t" * 43 not in said.out + said.err and "metabot ui" in said.err


@pytest.mark.parametrize("port", ["-1", "65536"])
def test_app_refuses_a_port_out_of_range(tmp_path, monkeypatch, capsys, port):
    _on_path(monkeypatch, tmp_path / "bin", "chromium")
    monkeypatch.setattr(web_window.subprocess, "Popen", Launches())
    assert main(["app", "--port", port]) == 2
    assert "is not a port" in capsys.readouterr().err


def _line(proc: subprocess.Popen, timeout: float = 30.0) -> str:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        ready, _, _ = select.select([proc.stdout], [], [], 0.2)
        if ready:
            return proc.stdout.readline().decode("utf-8")
        assert proc.poll() is None, "metabot app ended before it said where it serves"
    raise AssertionError("metabot app said nothing")


def test_app_serves_the_window_its_token_and_keeps_the_token_off_disk_and_logs(tmp_path, home):
    # the stand-in browser writes down what it was given, and is gone; it opens nothing
    given = tmp_path / "given.txt"
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    fake = bin_ / "google-chrome"
    fake.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > "{given}"\n', encoding="utf-8")
    fake.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    env["PATH"] = str(bin_)  # the stand-in alone: no browser of this machine is in reach
    err_path = tmp_path / "app.err"
    with err_path.open("wb") as err:
        proc = subprocess.Popen(
            [sys.executable, "-m", "metabot", "app"],
            cwd=tmp_path,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=err,
            start_new_session=True,
        )
    try:
        line = _line(proc)
        assert line.startswith("metabot app: the window is open on http://127.0.0.1:"), line
        deadline = time.monotonic() + 20
        while not given.exists() or not given.read_text(encoding="utf-8"):
            assert time.monotonic() < deadline, "the window was never asked for"
            time.sleep(0.05)
        [arg] = given.read_text(encoding="utf-8").splitlines()
        match = URL.fullmatch(arg.removeprefix("--app="))
        assert arg.startswith("--app=") and match, arg
        port, token = int(match.group(1)), match.group(2)
        assert f"127.0.0.1:{port}" in line
        with httpx2.Client(base_url=f"http://127.0.0.1:{port}") as client:
            assert client.get("/").status_code == 401
            assert client.get(f"/?token={token}").status_code == 303
            assert client.get("/").status_code == 200
        os.kill(proc.pid, signal.SIGTERM)
        assert proc.wait(timeout=20) == 0
        rest = proc.stdout.read().decode("utf-8", "replace")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        proc.stdout.close()
    assert token not in line + rest
    assert token not in err_path.read_text(encoding="utf-8", errors="replace")
    for path in home.rglob("*"):
        if path.is_file():
            assert token.encode() not in path.read_bytes(), path


# ── metabot app --install ───────────────────────────────────────────────────


def _desktop(text: str) -> dict[str, str]:
    lines = text.splitlines()
    assert lines[0] == "[Desktop Entry]"
    return dict(line.split("=", 1) for line in lines[1:] if line)


def test_install_writes_a_launcher_that_runs_metabot_app(tmp_path, monkeypatch, capsys):
    _on_path(monkeypatch, tmp_path / "bin", "chromium")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("HOME", str(tmp_path / "user"))  # a stray write would land here instead
    monkeypatch.setattr(web_window.sys, "executable", "/opt/mb/bin/python")
    assert main(["app", "--install"]) == 0
    launcher = tmp_path / "xdg" / "applications" / "metabot.desktop"
    icon = tmp_path / "xdg" / "icons" / "hicolor" / "scalable" / "apps" / "metabot.svg"
    entry = _desktop(launcher.read_text(encoding="utf-8"))
    assert entry["Type"] == "Application" and entry["Name"] == "MetaBot"
    assert entry["Exec"] == "/opt/mb/bin/python -I -m metabot app"
    assert entry["TryExec"] == "/opt/mb/bin/python"
    assert entry["Icon"] == str(icon) and entry["Terminal"] == "false"
    assert icon.read_text(encoding="utf-8") == web_theme.icon()
    for path in (launcher, icon):
        assert stat.S_IMODE(path.stat().st_mode) & 0o077 == 0  # the person's own
    assert not (tmp_path / "user").exists()
    out = capsys.readouterr().out
    assert str(launcher) in out and str(icon) in out


@pytest.mark.parametrize("xdg", [None, "", "relative/share"])
def test_install_with_no_usable_data_folder_writes_under_the_home_folder(
    tmp_path, monkeypatch, xdg
):
    _on_path(monkeypatch, tmp_path / "bin", "chromium")
    if xdg is None:
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    else:
        monkeypatch.setenv("XDG_DATA_HOME", xdg)  # a relative one is not a data folder
    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    monkeypatch.chdir(tmp_path)
    assert main(["app", "--install"]) == 0
    assert (tmp_path / "user" / ".local" / "share" / "applications" / "metabot.desktop").is_file()
    assert not (tmp_path / "relative").exists()


def test_install_with_no_browser_writes_nothing(tmp_path, monkeypatch, capsys):
    _on_path(monkeypatch, tmp_path / "bin", "firefox")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert main(["app", "--install"]) == 1
    assert not (tmp_path / "xdg").exists()
    assert "metabot ui" in capsys.readouterr().err


@pytest.mark.parametrize("platform", ["darwin", "win32"])
def test_install_elsewhere_prints_the_command_and_writes_nothing(
    tmp_path, monkeypatch, capsys, platform
):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(web_window.sys, "platform", platform)
    assert main(["app", "--install"]) == 0
    assert not (tmp_path / "xdg").exists()
    assert "metabot app" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("path", "written"),
    [
        ("/opt/mb/bin/python", "/opt/mb/bin/python"),
        ("/home/a b/py", '"/home/a b/py"'),
        ('/x/"q"', '"/x/\\\\"q\\\\""'),
        ("/x/$HOME", '"/x/\\\\$HOME"'),
        ("/x/a\\b", '"/x/a\\\\\\\\b"'),
        ("/x/100%", "/x/100%%"),
    ],
)
def test_the_launcher_quotes_its_command_as_desktop_files_read_it(path, written):
    assert web_window.exec_line([path, "-I"]) == f"{written} -I"


def test_a_path_with_a_line_break_is_refused_for_a_launcher():
    with pytest.raises(ValueError, match="line break"):
        web_window.exec_line(["/x/a\nb"])
