"""A fake upstream MCP server for tests, over stdio (default) or HTTP.

Tools: list_issues (read), create_issue (write), delete_repo (destructive),
frobnicate (unrecognised), echo_env (shows what credentials reached it).

--count-file F   append one line per tool call (proves a call never arrived)
--status-file F  at start, write this process's confinement (Linux /proc status)
--http PORT      serve streamable HTTP on 127.0.0.1:PORT instead of stdio
--pid-file F     at start, write this process's pid to F
--linger         keep running for a minute after stdin closes, as a server with
                 work of its own does (only a stop signal ends it sooner)
--say-token      at start, print FAKE_TOKEN to stderr: whole, split in two writes, and
                 deep in a long line (proves the run's log never holds it)
"""

from __future__ import annotations

import argparse
import json
import os

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp_types import ToolAnnotations

parser = argparse.ArgumentParser()
parser.add_argument("--count-file")
parser.add_argument("--status-file")
parser.add_argument("--http", type=int)
parser.add_argument("--say-token", action="store_true")
parser.add_argument("--pid-file")
parser.add_argument("--linger", action="store_true")
opts = parser.parse_args()

if opts.pid_file:
    with open(opts.pid_file, "w", encoding="utf-8") as fh:
        fh.write(str(os.getpid()))

if opts.say_token:
    import sys
    import time

    token = os.environ.get("FAKE_TOKEN", "")
    sys.stderr.write(f"starting with FAKE_TOKEN={token}\n")
    sys.stderr.flush()
    half = len(token) // 2
    sys.stderr.write(f"split {token[:half]}")
    sys.stderr.flush()
    time.sleep(0.2)  # the reader sees the first half on its own
    sys.stderr.write(f"{token[half:]} end\n")
    sys.stderr.write("x" * 200_000 + token + "y" * 10 + "\n")
    sys.stderr.flush()

if opts.status_file:
    try:
        with open("/proc/self/status", encoding="utf-8") as fh:
            wanted = ("Seccomp:", "NoNewPrivs:")
            status = {k.rstrip(":"): v.strip() for k, _, v in (ln.partition("\t") for ln in fh)
                      if k in wanted}  # fmt: skip
    except OSError:
        status = {}
    with open(opts.status_file, "w", encoding="utf-8") as fh:
        json.dump(status, fh)

server = MCPServer("fake")


def _count(name: str) -> None:
    if opts.count_file:
        with open(opts.count_file, "a", encoding="utf-8") as fh:
            fh.write(name + "\n")


@server.tool(annotations=ToolAnnotations(read_only_hint=True))
def list_issues(repo: str = "acme/widgets") -> str:
    """List open issues."""
    _count("list_issues")
    return json.dumps([{"number": 1, "title": "first", "repo": repo}])


@server.tool()
def create_issue(title: str) -> str:
    """Create an issue."""
    _count("create_issue")
    return json.dumps({"number": 2, "title": title})


@server.tool()
def delete_repo(repo: str) -> str:
    """Delete a repository."""
    _count("delete_repo")
    return json.dumps({"deleted": repo})


@server.tool()
def frobnicate(level: int = 1) -> str:
    """Do something nobody has classified."""
    _count("frobnicate")
    return json.dumps({"frobnicated": level})


@server.tool()
def echo_env(ctx: Context) -> str:
    """Report which credentials reached this server."""
    _count("echo_env")
    seen = {
        "FAKE_TOKEN": os.environ.get("FAKE_TOKEN"),
        "HOME_SECRET": os.environ.get("HOME_SECRET"),
    }
    request = getattr(ctx.request_context, "request", None)
    if request is not None:
        seen["authorization"] = request.headers.get("authorization")
        seen["x-api-key"] = request.headers.get("x-api-key")
    return json.dumps(seen)


if __name__ == "__main__":
    if opts.http:
        server.run(transport="streamable-http", port=opts.http)
    else:
        server.run()
    if opts.linger:
        import time

        time.sleep(60)
