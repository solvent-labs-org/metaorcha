"""Shared fixtures: every test gets its own MetaBot home and signing key path."""

from __future__ import annotations

import errno
import inspect
import ipaddress
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from fake_chat import FakeChat

TESTS = Path(__file__).resolve().parent
FIXTURES = TESTS / "fixtures"
FAKE_CLI = TESTS / "fake_engines" / "fake_cli.py"
FAKE_MCP = TESTS / "fake_mcp" / "server.py"


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    monkeypatch.setenv("METABOT_HOME", str(h))
    monkeypatch.setenv("ORCHA_KEY_PATH", str(tmp_path / "orcha-key"))
    return h


@pytest.fixture(autouse=True)
def claude_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The claude engine's config folder: empty, so no test reads this machine's settings."""
    cfg = tmp_path / "claude-config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    return cfg


@pytest.fixture(autouse=True)
def _no_sandbox_here(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No run hides with this machine's bwrap, or finds this machine's own rule folders."""
    monkeypatch.setattr("metabot.sandbox.find_bwrap", lambda: None)
    monkeypatch.setattr("metabot.sandbox.user_home", lambda: tmp_path / "user-home")
    monkeypatch.delenv("CODEX_HOME", raising=False)


@pytest.fixture(autouse=True)
def _nothing_installed_here(monkeypatch: pytest.MonkeyPatch) -> None:
    """The roster sees no engine CLI and no local model server unless a test says so."""
    nothing = {"claude": None, "codex": None, "cursor": None}
    monkeypatch.setattr("metabot.roster.here", lambda cfg: (dict(nothing), False))
    monkeypatch.setattr("metabot.roster.available", lambda: dict(nothing))


@pytest.fixture(autouse=True)
def _every_desk_command_here(monkeypatch: pytest.MonkeyPatch) -> None:
    """An office desk finds its engine's command on PATH, so no test reads this machine's."""
    monkeypatch.setattr("metabot.office.missing_command", lambda engine: None)


def not_on_path(monkeypatch: pytest.MonkeyPatch, *commands: str) -> None:
    """An office desk finds these engine commands missing from PATH, and every other one there."""
    from metabot.engines import BINARIES

    absent = {engine: binary for engine, binary in BINARIES.items() if binary in commands}
    monkeypatch.setattr("metabot.office.missing_command", absent.get)


@pytest.fixture(params=["raising", "lenient"])
def path_checks(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    """Path.is_file and is_dir raise for a path they cannot look at through Python 3.13, as its
    sources show; a later one may say False, as os.path does. What MetaBot reads must not
    depend on the Python it runs on."""
    if request.param == "lenient":
        monkeypatch.setattr(Path, "is_file", lambda self, **_: os.path.isfile(self))
        monkeypatch.setattr(Path, "is_dir", lambda self, **_: os.path.isdir(self))
    return str(request.param)


@pytest.fixture
def python() -> str:
    return sys.executable


def private_mode(path: Path) -> int:
    return os.stat(path).st_mode & 0o777


@pytest.fixture
def chat():
    fakes: list[FakeChat] = []

    def make(*args, **kwargs) -> FakeChat:
        fake = FakeChat(*args, **kwargs)
        fakes.append(fake)
        return fake

    yield make
    for fake in fakes:
        fake.close()


# Tests stay on this machine. A lookup of, or a connection to, any other host
# fails at once, the test is named in the summary, and the run fails.
_REACHED: list[str] = []


def _here(host: object) -> bool:
    """Whether ``host`` is this machine: localhost, or a loopback or unspecified address."""
    if host is None:
        return True
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    if not isinstance(host, str):
        return False
    if host in ("", "localhost"):
        return True
    try:
        address = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return False
    return address.is_loopback or address.is_unspecified


def _reached(what: str, host: object) -> None:
    _REACHED.append(f"{os.environ.get('PYTEST_CURRENT_TEST', '?')}: {what} {host!r}")


@pytest.fixture(autouse=True, scope="session")
def _tests_stay_on_this_machine():
    saved = (
        socket.getaddrinfo,
        socket.gethostbyname,
        socket.gethostbyname_ex,
        socket.socket.connect,
        socket.socket.connect_ex,
    )
    lookup, by_name, by_name_ex, connect, connect_ex = saved

    def guard_lookup(real):
        def call(host=None, *args, **kwargs):
            if not _here(host):
                _reached("lookup", host)
                raise socket.gaierror(socket.EAI_NONAME, f"tests stay on this machine: {host!r}")
            return real(host, *args, **kwargs)

        return call

    def guard_connect(real, returns_errno):
        def call(sock, address):
            if sock.family in (socket.AF_INET, socket.AF_INET6) and not _here(address[0]):
                _reached("connect", address[0])
                if returns_errno:
                    return errno.ENETUNREACH
                raise OSError(errno.ENETUNREACH, f"tests stay on this machine: {address[0]!r}")
            return real(sock, address)

        return call

    socket.getaddrinfo = guard_lookup(lookup)
    socket.gethostbyname = guard_lookup(by_name)
    socket.gethostbyname_ex = guard_lookup(by_name_ex)
    socket.socket.connect = guard_connect(connect, returns_errno=False)
    socket.socket.connect_ex = guard_connect(connect_ex, returns_errno=True)
    try:
        yield
    finally:
        (
            socket.getaddrinfo,
            socket.gethostbyname,
            socket.gethostbyname_ex,
            socket.socket.connect,
            socket.socket.connect_ex,
        ) = saved


# Tests start no engine CLI of this machine. Starting ``claude``, ``codex`` or
# ``cursor-agent`` from outside the tests' own folders fails at once, the test
# is named in the summary, and the run fails. A test's fake engines live in its
# temp folder. The live tests may start the real ones when METABOT_LIVE=1.
_STARTED: list[str] = []


def _engine_started(call: dict, ours: tuple[Path, ...]) -> str | None:
    """The engine CLI of this machine that a new process would run, if any."""
    from metabot.engines import BINARIES
    from metabot.sandbox import inner_argv

    args = call["args"]
    if call["shell"]:
        line = os.fsdecode(args if isinstance(args, (str, bytes)) else args[0]).split()
        name = line[0] if line else ""
    else:
        first = args if isinstance(args, (str, bytes, os.PathLike)) else args[0]
        name = os.fsdecode(call["executable"] if call["executable"] is not None else first)
        if not isinstance(args, (str, bytes, os.PathLike)) and os.path.basename(name) == "bwrap":
            inner = inner_argv([os.fsdecode(a) for a in args])  # the engine bwrap starts
            name = inner[0] if inner else ""
    if os.path.basename(name) not in BINARIES.values():
        return None
    search = os.pathsep.join(os.get_exec_path(call["env"]))
    path = name if os.sep in name else shutil.which(name, path=search)
    if path is None:
        return None
    where = Path(path)
    if any(p.is_relative_to(root) for p in (where.absolute(), where.resolve()) for root in ours):
        return None
    return path


@pytest.fixture(autouse=True, scope="session")
def _tests_start_no_engine_of_this_machine(tmp_path_factory: pytest.TempPathFactory):
    base = tmp_path_factory.getbasetemp()
    ours = (TESTS, TESTS.resolve(), base.absolute(), base.resolve())
    execute = subprocess.Popen._execute_child  # every Popen, asyncio's too, starts here
    signature = inspect.signature(execute)

    def guarded(self, *args, **kwargs):
        found = _engine_started(signature.bind(self, *args, **kwargs).arguments, ours)
        test = os.environ.get("PYTEST_CURRENT_TEST", "?")
        live = os.environ.get("METABOT_LIVE") == "1" and "test_live.py::" in test
        if found is not None and not live:
            _STARTED.append(f"{test}: {found}")
            why = f"tests start no engine CLI of this machine: {found}"
            raise PermissionError(errno.EACCES, why)
        return execute(self, *args, **kwargs)

    subprocess.Popen._execute_child = guarded
    try:
        yield
    finally:
        subprocess.Popen._execute_child = execute


# Tests keep out of this machine's MetaBot home. A test whose own ``home`` never ran (a
# parametrize argument of that name takes the fixture's place) finds a home in the session's
# temp folder instead of ``~/.metabot``; if it writes there, it is named in the summary and the
# run fails.
_HOMELESS: list[str] = []


@pytest.fixture(autouse=True, scope="session")
def _no_home_of_this_machine(tmp_path_factory: pytest.TempPathFactory):
    stray = tmp_path_factory.getbasetemp() / "no-test-home"
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("METABOT_HOME", str(stray / "home"))
        mp.setenv("ORCHA_KEY_PATH", str(stray / "orcha-key"))
        mp.setenv("CLAUDE_CONFIG_DIR", str(stray / "claude-config"))
        yield stray


@pytest.fixture(autouse=True)
def _a_test_writes_only_its_own_home(
    _no_home_of_this_machine: Path, request: pytest.FixtureRequest
):
    yield
    stray = _no_home_of_this_machine
    if stray.exists():
        _HOMELESS.append(f"{request.node.nodeid}: wrote {_clear_stray(stray)}")


def _clear_stray(stray: Path) -> list[str]:
    """What a test wrote in the stray home, which then goes, also a folder in it that lost the
    bits it is emptied by; one that still cannot go is moved aside, so no test after it finds a
    stray home and is named for it."""
    from metabot.plugins import _open_up

    _open_up(stray)
    wrote = sorted(p.name for p in stray.iterdir())
    shutil.rmtree(stray, ignore_errors=True)
    if stray.exists():
        stray.rename(stray.with_name(f"{stray.name}-{len(_HOMELESS)}"))
    return wrote


def pytest_sessionfinish(session, exitstatus):
    if _REACHED or _STARTED or _HOMELESS:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_terminal_summary(terminalreporter):
    for title, lines in (
        ("tests reached for another host", _REACHED),
        ("tests started an engine CLI of this machine", _STARTED),
        ("tests ran without their own MetaBot home", _HOMELESS),
    ):
        if lines:
            terminalreporter.section(title)
            for line in lines:
                terminalreporter.write_line(line)
