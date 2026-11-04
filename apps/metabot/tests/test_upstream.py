from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time

import pytest

from conftest import FAKE_MCP
from metabot.connections import ConnectionStore
from metabot.upstream import Upstream


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def http_server():
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, str(FAKE_MCP), "--http", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    yield f"http://127.0.0.1:{port}/mcp"
    proc.terminate()
    proc.wait(10)


async def test_stdio_list_and_call(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME_SECRET", "must-not-leak")
    count = tmp_path / "count"
    store = ConnectionStore()
    conn = store.add(
        name="gh",
        transport="stdio",
        command=[sys.executable, str(FAKE_MCP), "--count-file", str(count)],
        token="tok-123",
        token_env="FAKE_TOKEN",
    )
    async with Upstream(conn, store.secret(conn)) as up:
        names = [t.name for t in await up.list_tools()]
        assert names == ["list_issues", "create_issue", "delete_repo", "frobnicate", "echo_env"]
        result = await up.call_tool("echo_env", {})
    seen = json.loads(result.content[0].text)
    assert seen["FAKE_TOKEN"] == "tok-123"
    assert seen["HOME_SECRET"] is None
    assert count.read_text().splitlines() == ["echo_env"]


async def test_http_bearer_header(http_server):
    store = ConnectionStore()
    conn = store.add(name="h", transport="http", url=http_server, token="tok-http")
    async with Upstream(conn, store.secret(conn)) as up:
        result = await up.call_tool("echo_env", {})
    seen = json.loads(result.content[0].text)
    assert seen["authorization"] == "Bearer tok-http"
    assert seen["FAKE_TOKEN"] is None


async def test_http_custom_header_carries_the_raw_token(http_server):
    store = ConnectionStore()
    conn = store.add(name="k", transport="http", url=http_server, token="k-1", header="X-API-Key")
    async with Upstream(conn, store.secret(conn)) as up:
        seen = json.loads((await up.call_tool("echo_env", {})).content[0].text)
    assert seen["x-api-key"] == "k-1"
    assert seen["authorization"] is None


async def test_unknown_tool_is_an_error_result(tmp_path):
    store = ConnectionStore()
    conn = store.add(name="gh", transport="stdio", command=[sys.executable, str(FAKE_MCP)])
    async with Upstream(conn, None) as up:
        result = await up.call_tool("no_such_tool", {})
    assert result.is_error is True


async def test_a_server_that_will_not_start_raises(tmp_path):
    store = ConnectionStore()
    conn = store.add(name="gone", transport="stdio", command=[str(tmp_path / "missing-server")])
    with pytest.raises(Exception):  # noqa: B017 - any failure to connect is fine; it must not hang
        async with Upstream(conn, None, timeout=10):
            pass


COPY = f"plugins/tools@{'a' * 64}"
REMEDY = f"metabot plugin enable {'a' * 12} --add-servers"


@pytest.mark.parametrize(
    "line",
    [
        "exec python3 {copy}/server.py",
        'cd "{copy}" && exec ./run',
        "exec node '{copy}/index.js'",
        "x;{copy}/run",
        "PATH={copy}/bin:$PATH exec run",
    ],
)
def test_a_whole_path_to_this_homes_copy_in_a_shell_line_starts(line):
    from metabot.paths import metabot_home
    from metabot.upstream import misplaced_copy

    copy = metabot_home() / COPY
    copy.mkdir(parents=True)
    # before: refused, after a space or a quote, and enabling again wrote it the same
    assert misplaced_copy(["sh", "-c", line.format(copy=copy)]) is None


@pytest.mark.parametrize(
    "command",
    [
        ["bash", "-lc", "exec python3 {copy}/server.py"],
        ["/bin/sh", "-e", "-c", "exec python3 {copy}/server.py"],
        ["sh", "-o", "errexit", "-c", "exec python3 {copy}/server.py"],
        ["sh", "-c", 'exec python3 "$0"', "{copy}/server.py"],
        ["sh", "{copy}/run.sh"],
        ["python3", "--root={copy}"],
        ["sh", "-c", "-", "exec python3 {copy}/server.py"],
        ["sh", "-c", "+", "exec python3 {copy}/server.py"],
    ],
)
def test_the_line_a_shell_runs_is_read_as_it_reads_it_and_every_other_word_whole(command):
    from metabot.paths import metabot_home
    from metabot.upstream import misplaced_copy

    copy = metabot_home() / COPY
    copy.mkdir(parents=True)
    assert misplaced_copy([w.format(copy=copy) for w in command]) is None


@pytest.mark.parametrize(
    "line",
    [
        "exec python3 rh/{copy}/server.py",
        "exec python3 ../..{home}/{copy}/server.py",
        'cd "rh/{copy}" && exec ./run',
        "-I{home}/{copy}/include",
        "@{home}/{copy}/args",
    ],
)
def test_a_copy_a_shell_line_names_by_a_relative_path_is_refused(line):
    from metabot.paths import metabot_home
    from metabot.upstream import misplaced_copy

    home = metabot_home()
    (home / COPY).mkdir(parents=True)
    said = misplaced_copy(["sh", "-c", line.format(copy=COPY, home=home)])
    assert said is not None and said.endswith(REMEDY)


@pytest.mark.parametrize(
    "command",
    [
        ["sh", "-c", "exec $(pwd)/{copy}/server.sh"],
        ["sh", "-c", "exec $(printf .){copy}/server.sh"],
        ["sh", "-c", "exec $((1)){copy}/server.sh"],
        ["bash", "-O", "extglob", "-c", "exec +(x){copy}/server.sh"],
        ["sh", "-c", "exec x\r{copy}/server.sh"],
    ],
)
def test_a_word_a_shell_line_glues_to_the_copys_path_is_read_with_it(command):
    from metabot.paths import metabot_home
    from metabot.upstream import misplaced_copy

    copy = metabot_home() / COPY
    copy.mkdir(parents=True)
    said = misplaced_copy([w.format(copy=copy) for w in command])
    # before: started, as the copy's path was read as a word of its own, and the shell ran a
    # path in the server's folder
    assert said is not None and said.endswith(REMEDY)


@pytest.mark.parametrize(
    "command",
    [
        ["sh", "-c", "# it's the server\nexec x';'{copy}/run # it's"],
        ["sh", "-c", ": <<'E'\n'\nE\nexec x';'{copy}/run #'"],
        ["bash", "-O", "extglob", "-c", "exec +(x|{copy}/run)"],
        ["sh", "-c", "exec ${{x-;{copy}/run}}"],
        ["sh", "-c", "exec ${{x-&{copy}/run}}"],
        ["sh", "-c", "cd $(dirname {copy}/server.py) && exec ./server.py"],
        ["sh", "-c", "(cd {copy} && exec ./run)"],
    ],
)
def test_a_shell_line_holding_what_this_does_not_read_as_a_shell_does_starts_no_copy(command):
    from metabot.paths import metabot_home
    from metabot.upstream import misplaced_copy

    copy = metabot_home() / COPY
    copy.mkdir(parents=True)
    said = misplaced_copy([w.format(copy=copy) for w in command])
    # before: started; a comment's or a here-document's quote, an extglob's '|' or an operator
    # in ${...} split the line where the shell does not, and the shell ran a relative path
    assert said is not None and said.endswith(REMEDY)


@pytest.mark.parametrize("spelled", ["past a ..", "through a link"])
@pytest.mark.parametrize(
    "command",
    [
        ["env", "sh", "-c", "{word}"],
        ["sh", "-c", "exec $0", "{word}"],
        ["env", "-S", "{word} --stdio"],
        ["python3", "{word}"],
    ],
)
def test_a_copy_named_by_a_path_a_program_can_split_starts_under_no_home(
    tmp_path, spelled, command
):
    from metabot.paths import metabot_home
    from metabot.upstream import misplaced_copy

    copy = metabot_home() / COPY
    copy.mkdir(parents=True)
    if spelled == "past a ..":
        (tmp_path / "x y").mkdir()
        word = f"{tmp_path}/x y" + "/.." * (len((tmp_path / "x y").parts) - 1) + f"{copy}/run"
    else:
        (tmp_path / "a b").symlink_to(metabot_home())
        word = f"{tmp_path}/a b/{COPY}/run"
    said = misplaced_copy([w.format(word=word) for w in command])
    # before: started under a plain home, and a shell behind env, an unquoted $0 or env -S split
    # the path at its blank to run the file before it
    assert said is not None and said.endswith(REMEDY)


@pytest.mark.parametrize("last", ["p", "plug"])
@pytest.mark.parametrize(
    "command",
    [
        ["python3", "{other}/{copy}/run"],
        ["python3", "x{other}/{copy}/run"],
        ["sh", "-c", "exec x{other}/{copy}/run"],
    ],
)
def test_a_home_named_as_a_part_of_plugins_still_reads_every_copy(
    tmp_path, monkeypatch, last, command
):
    from metabot.upstream import misplaced_copy

    monkeypatch.setenv("METABOT_HOME", str(tmp_path / "hp" / last))
    (tmp_path / "hp" / last / COPY).mkdir(parents=True)
    said = misplaced_copy([w.format(other=tmp_path / "hp", copy=COPY) for w in command])
    # before: started, as holding the home's path ate the start of plugins/ and no copy was read
    assert said is not None and said.endswith(REMEDY)


@pytest.mark.parametrize(
    "command",
    [
        ["python3 {copy}/server.py"],
        ["python3", "@{copy}/server.py"],
        ["python3", '"{copy}/server.py"'],
        ["python3", "'{copy}/server.py'"],
        ["python3", "({copy}/server.py"],
        ["sh", "-c", "exec run", "x {copy}/y"],
    ],
)
def test_a_word_no_shell_reads_names_the_copy_only_whole(command):
    from metabot.paths import metabot_home
    from metabot.upstream import misplaced_copy

    copy = metabot_home() / COPY
    copy.mkdir(parents=True)
    said = misplaced_copy([w.format(copy=copy) for w in command])
    # before: started, and the program, which no shell splits for, opened a relative path
    assert said is not None and said.endswith(REMEDY)


@pytest.mark.parametrize(
    ("folder", "command"),
    [
        ("R&D", ["sh", "-c", "exec python3 {copy}/server.py"]),
        ("x|y", ["sh", "-c", "exec node {copy}/index.js"]),
        ("a;b", ["sh", "-c", "{copy}/bin/server"]),
        ("App Support", ["sh", "-c", "exec python3 {copy}/server.py"]),
        ("App Support", ["sh", "-c", "PYTHONPATH={copy}/lib exec python3 -m srv"]),
        ("x:y", ["sh", "-c", "PYTHONPATH={copy}/lib exec python3 -m srv"]),
        ("x:y", ["env", "PYTHONPATH={copy}/lib", "python3", "-m", "srv"]),
        ("[ab]", ["sh", "-c", "exec python3 {copy}/server.py"]),
        ("$x", ["sh", "-c", "exec python3 {copy}/server.py"]),
    ],
)
def test_a_copy_a_list_or_a_shell_line_splits_inside_the_homes_path_is_refused(
    tmp_path, monkeypatch, folder, command
):
    from metabot.upstream import misplaced_copy

    where = tmp_path / folder / "mb"
    monkeypatch.setenv("METABOT_HOME", str(where))
    (where / COPY).mkdir(parents=True)
    said = misplaced_copy([w.format(copy=where / COPY) for w in command])
    # before: started, and sh or the list's reader took a relative tail in the server's folder
    assert said is not None and "move the home" in said and said.endswith(REMEDY)


@pytest.mark.parametrize(
    ("folder", "command"),
    [
        ("App Support", ["sh", "-c", 'cd "{copy}" && exec ./run']),
        ("App Support", ["python3", "{copy}/server.py"]),
        ("R&D", ["sh", "-c", "exec python3 '{copy}/server.py'"]),
        ("App Support", ["env", "sh", "-c", "{copy}/server.sh"]),
        ("[ab]", ["nice", "sh", "-c", "{copy}/server.sh"]),
        ("App Support", ["bash", "--norc", "-c", "{copy}/server.sh"]),
        ("App Support", ["sh", "-c", "exec $0", "{copy}/server.sh"]),
        ("App Support", ["env", "-S", "{copy}/server.sh --stdio"]),
    ],
)
def test_no_server_names_a_copy_under_a_home_a_program_can_split_or_expand(
    tmp_path, monkeypatch, folder, command
):
    from metabot.upstream import misplaced_copy

    where = tmp_path / folder / "mb"
    monkeypatch.setenv("METABOT_HOME", str(where))
    (where / COPY).mkdir(parents=True)
    said = misplaced_copy([w.format(copy=where / COPY) for w in command])
    # before: started, and env, a shell behind another program, or $0 unquoted split or
    # globbed the home's path, to run a file beside it
    assert said is not None and said.startswith(f"it names a copy of plugin tools in {where}/")
    assert "move the home" in said and said.endswith(REMEDY)


def test_a_misnamed_copy_under_such_a_home_is_told_both(tmp_path, monkeypatch):
    from metabot.upstream import misplaced_copy

    where = tmp_path / "App Support" / "mb"
    monkeypatch.setenv("METABOT_HOME", str(where))
    (where / COPY).mkdir(parents=True)
    said = misplaced_copy(["python3", f"/old/home/{COPY}/server.py"])
    # before: that it names the copy in this home, which it does not
    assert said is not None and "other than by a whole path to the one in" in said
    assert "move the home" in said and said.endswith(REMEDY)


@pytest.mark.skipif(os.geteuid() == 0, reason="root enters every folder")
@pytest.mark.parametrize("past", ["a file", "a folder no one can enter"])
def test_a_copy_named_past_a_part_no_system_call_can_follow_is_refused(tmp_path, past):
    from metabot.paths import metabot_home
    from metabot.upstream import misplaced_copy

    copy = metabot_home() / COPY
    copy.mkdir(parents=True)
    part = tmp_path / "part"
    if past == "a file":
        part.write_text("", encoding="utf-8")
    else:
        part.mkdir()
        part.chmod(0o600)
    try:
        said = misplaced_copy(["python3", f"{part}/../{copy.relative_to(tmp_path)}/server.py"])
    finally:
        part.chmod(0o700)
    # before: started, as 3.11 and 3.12 take that .. as text, and failed with no line to mend it
    assert said is not None and said.endswith(REMEDY)
