"""A fake browser MCP server for tests, over stdio: it keeps one page's url, and nothing else.

Tools: navigate(url), navigate_back(), tabs(action, index), evaluate(expression),
click(selector), type(text), screenshot(). A url with "unreachable" in it
fails, the way a page that does not load does.

--count-file F   append one line per tool call that reached this server
"""

from __future__ import annotations

import argparse
import json

from mcp.server import MCPServer

parser = argparse.ArgumentParser()
parser.add_argument("--count-file")
opts = parser.parse_args()

server = MCPServer("fake-browser")
page = {"url": "about:blank"}


def _count(name: str) -> None:
    if opts.count_file:
        with open(opts.count_file, "a", encoding="utf-8") as fh:
            fh.write(name + "\n")


@server.tool()
def navigate(url: str, referrer_url: str = "") -> str:
    """Open a url in the page."""
    _count("navigate")
    if "unreachable" in url:
        raise RuntimeError(f"cannot load {url}")
    page["url"] = url
    return json.dumps({"url": url})


@server.tool()
def navigate_back() -> str:
    """Go back to the page before this one."""
    _count("navigate_back")
    return json.dumps({"went": "back"})


@server.tool()
def tabs(action: str, index: int = 0) -> str:
    """List, open, select or close the browser's tabs."""
    _count("tabs")
    return json.dumps({"action": action, "index": index})


@server.tool()
def evaluate(expression: str) -> str:
    """Run JavaScript in the page."""
    _count("evaluate")
    return json.dumps({"ran": len(expression)})


@server.tool()
def click(selector: str) -> str:
    """Click an element of the page."""
    _count("click")
    return json.dumps({"clicked": selector, "url": page["url"]})


@server.tool(name="type")
def type_text(text: str) -> str:
    """Type text into the element that has focus."""
    _count("type")
    return json.dumps({"typed": len(text), "url": page["url"]})


@server.tool()
def screenshot() -> str:
    """A picture of the page."""
    _count("screenshot")
    return json.dumps({"png": "iVBORw0KGgo=", "url": page["url"]})


if __name__ == "__main__":
    server.run()
