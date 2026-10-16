"""Shared test setup: the suite never reaches past this machine."""

from __future__ import annotations

import errno
import ipaddress
import os
import socket

import pytest

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
                raise socket.gaierror(
                    socket.EAI_NONAME, f"tests stay on this machine: {host!r}"
                )
            return real(host, *args, **kwargs)

        return call

    def guard_connect(real, returns_errno):
        def call(sock, address):
            if sock.family in (socket.AF_INET, socket.AF_INET6) and not _here(
                address[0]
            ):
                _reached("connect", address[0])
                if returns_errno:
                    return errno.ENETUNREACH
                raise OSError(
                    errno.ENETUNREACH, f"tests stay on this machine: {address[0]!r}"
                )
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


def pytest_sessionfinish(session, exitstatus):
    if _REACHED:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_terminal_summary(terminalreporter):
    if _REACHED:
        terminalreporter.section("tests reached for another host")
        for line in _REACHED:
            terminalreporter.write_line(line)
