"""Reading the tool calls a local model writes badly.

Small models often call a tool by writing the call as their answer, put the
arguments in a code fence, or get a tool's name nearly right. These functions
read such a call when it can be read without guessing, and read nothing
otherwise: a call MetaBot is not sure of is never made. Every call that is
read still goes through the gate as the tool it resolves to.

Two repairs read what a model plainly meant. Python's triple quotes written
inside a JSON string are read as text: valid JSON never holds three quotes in
a row, so the repair is tried after a parse fails and cannot change a call
that was valid. When an answer is calls one after another and the last one
breaks off before its JSON ends, ``leading_calls`` reads the complete ones and
never the broken one. A call in a code fence inside a longer answer is never
made: ``calls_in_fences`` names it, so the engine can ask for the call alone.
"""

from __future__ import annotations

import difflib
import json
import re
from collections.abc import Sequence
from typing import Any

NAME_KEYS = ("name", "tool")
ARG_KEYS = ("arguments", "parameters", "args", "input")
NAME_CUTOFF = 0.85  # how close a misspelt name must be to the one tool it means

# What models call the built-in tools; used only when the tool is offered.
ALIASES = {
    "ls": "list_dir",
    "listdir": "list_dir",
    "listdirectory": "list_dir",
    "listfiles": "list_dir",
    "cat": "read_file",
    "readfiles": "read_file",
    "openfile": "read_file",
    "writefiles": "write_file",
    "createfile": "write_file",
    "savefile": "write_file",
    "bash": "shell",
    "sh": "shell",
    "terminal": "shell",
    "runcommand": "shell",
    "runshell": "shell",
    "execute": "shell",
    "exec": "shell",
}

_FENCE = re.compile(r"^```[A-Za-z0-9_-]*\s*\n?(.*?)\n?```$", re.S)
_TAGS = re.compile(r"^<tool_call>\s*(.*?)\s*</tool_call>$", re.S)
_FENCES = re.compile(r"```[A-Za-z0-9_-]*[ \t]*\n(.*?)```", re.S)  # each fence in a longer text
_TRIPLE = re.compile(r'(?<!\\)"""')  # three quotes in a row: never in valid JSON


def unwrap(text: str) -> str:
    """The text inside one code fence or one pair of ``<tool_call>`` tags, else the text."""
    text = text.strip()
    for pattern in (_FENCE, _TAGS):
        match = pattern.match(text)
        if match:
            text = match.group(1).strip()
    return text


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def resolve_name(asked: str, offered: Sequence[str]) -> str | None:
    """The offered tool ``asked`` means, when it can mean only one; else None."""
    if asked in offered:
        return asked
    key = _norm(asked)
    if not key:
        return None
    same = [name for name in offered if _norm(name) == key]
    if len(same) == 1:
        return same[0]
    alias = ALIASES.get(key)
    if alias in offered:
        return alias
    by_norm = {_norm(name): name for name in offered}
    close = difflib.get_close_matches(key, list(by_norm), n=2, cutoff=NAME_CUTOFF)
    return by_norm[close[0]] if len(close) == 1 else None


def parse_args(raw: Any) -> dict[str, Any] | None:
    """A call's arguments as an object: given as one, or as JSON text (fenced, or
    encoded twice). None when they are not an object."""
    if raw is None:
        return {}
    for _ in range(2):  # an object written as a JSON string inside a JSON string
        if isinstance(raw, dict):
            return raw
        if not isinstance(raw, str):
            return None
        text = unwrap(raw)
        if not text:
            return {}
        try:
            raw = json.loads(text)
        except (ValueError, RecursionError):
            return None
    return raw if isinstance(raw, dict) else None


def _values(text: str) -> list[Any] | None:
    """The JSON values ``text`` is made of, one after another; None if it is not only that."""
    decoder = json.JSONDecoder()
    values: list[Any] = []
    at = 0
    while at < len(text):
        while at < len(text) and text[at].isspace():
            at += 1
        if at == len(text):
            break
        try:
            value, at = decoder.raw_decode(text, at)
        except (ValueError, RecursionError):
            return None
        values.append(value)
    return values


def _objects(text: str) -> list[Any] | None:
    """The JSON values the whole text is made of, one after another; None if it is not only that."""
    text = unwrap(text)
    values = _values(text)
    if values is None and '"""' in text:  # Python's docstring quotes, meant as text
        values = _values(_TRIPLE.sub(r'\\"\\"\\"', text))
    if values and len(values) == 1 and isinstance(values[0], list):
        values = values[0]
    return values or None


def _name_and_args(obj: Any) -> tuple[Any, Any, bool]:
    """(name, raw arguments, whether it is shaped like a call) for one object."""
    if not isinstance(obj, dict):
        return None, None, False
    fn = obj.get("function")
    if isinstance(fn, dict):
        obj = fn
    name = next((obj[k] for k in (*NAME_KEYS, "function") if isinstance(obj.get(k), str)), None)
    has_args = any(k in obj for k in ARG_KEYS)
    raw = next((obj[k] for k in ARG_KEYS if k in obj), None)
    return name, raw, name is not None and (has_args or set(obj) <= {*NAME_KEYS, "type"})


def calls_in_text(
    text: str, offered: Sequence[str]
) -> list[tuple[str, dict[str, Any], str]] | None:
    """The calls a model wrote as its whole answer: ``(tool, args, name it asked for)``.

    All or nothing: when any part of the text is not a call to an offered tool
    with an object of arguments, nothing is read.
    """
    values = _objects(text)
    if values is None:
        return None
    calls = []
    for value in values:
        name, raw, shaped = _name_and_args(value)
        if not shaped:
            return None
        tool = resolve_name(name, offered)
        args = parse_args(raw)
        if tool is None or args is None:
            return None
        calls.append((tool, args, name))
    return calls


def leading_calls(
    text: str, offered: Sequence[str]
) -> list[tuple[str, dict[str, Any], str]] | None:
    """The complete calls before a call that breaks off, in an answer made of calls.

    The answer must be calls one after another, every one read as
    ``calls_in_text`` reads them, until the JSON of one breaks: what is left
    starts as an object and never parses. That one and anything after it is
    never read. None when the whole answer parses, or when anything before the
    break is not a call to an offered tool.
    """
    text = unwrap(text)
    if '"""' in text:
        text = _TRIPLE.sub(r'\\"\\"\\"', text)
    decoder = json.JSONDecoder()
    calls: list[tuple[str, dict[str, Any], str]] = []
    at = 0
    while at < len(text):
        while at < len(text) and text[at].isspace():
            at += 1
        if at == len(text):
            return None  # it all parsed: calls_in_text decides
        try:
            value, at = decoder.raw_decode(text, at)
        except (ValueError, RecursionError):
            return calls if calls and text[at] == "{" else None
        name, raw, shaped = _name_and_args(value)
        tool = resolve_name(name, offered) if shaped else None
        args = parse_args(raw) if tool else None
        if tool is None or args is None:
            return None
        calls.append((tool, args, name))
    return None


def calls_in_fences(text: str, offered: Sequence[str]) -> list[str]:
    """The tools named by calls a model wrote in code fences inside a longer answer.

    None of them is made: the answer is more than the call, so whether the
    model meant to make it, or to show it, is a guess. The engine asks for the
    call alone instead.
    """
    if calls_in_text(text, offered) is not None:
        return []  # the whole answer is calls: read as calls, not here
    named: list[str] = []
    for block in _FENCES.findall(text):
        read = calls_in_text(block, offered)
        if read:
            named.extend(tool for tool, _args, _asked in read)
    return named


def call_shaped(text: str) -> bool:
    """Whether a whole answer has the shape of a tool call: a name and its arguments."""
    values = _objects(text)
    if not values:
        return False
    for value in values:
        if not isinstance(value, dict):
            return False
        fn = value.get("function")
        obj = fn if isinstance(fn, dict) else value
        if not (
            any(isinstance(obj.get(k), str) for k in NAME_KEYS) and any(k in obj for k in ARG_KEYS)
        ):
            return False
    return True


def step_in_text(text: str, offered: Sequence[str]) -> tuple[str, dict[str, Any]] | None:
    """One step of constrained output, ``{"tool": ..., "arguments": {...}}``: a tool,
    or ``answer``. None when it does not read as one."""
    values = _objects(text)
    if not values or len(values) != 1 or not isinstance(values[0], dict):
        return None
    obj = values[0]
    name = obj.get("tool", obj.get("name"))
    if not isinstance(name, str):
        return None
    tool = "answer" if name == "answer" else resolve_name(name, offered)
    args = parse_args(obj.get("arguments", obj.get("parameters")))
    if tool is None or args is None:
        return None
    return tool, args
