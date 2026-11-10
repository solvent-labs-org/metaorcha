"""The places a run's tools never reach with no one asked, and how a path stands to them.

They are MetaBot's connection tokens (``<home>/secrets``), the key receipts are signed
with (``ORCHA_KEY_PATH``, else ``~/.orcha/key``) and every login folder in config.json.
A path is INSIDE when it is one of them or in one, HOLDS when it is a folder above one,
and UNKNOWN when where it leads is made as the call runs (``/proc/self/cwd``, a link
through it). A call that could reach one asks: a person is asked for that exact call, and
with no one there it is refused.

A path is read the ways a tool may open it: as written and tidied (``a/../b`` is ``b``),
as the kernel follows its links part by part, and tidied and then followed; ``~``,
``$HOME`` and ``file://`` as a tool that expands them would. Names are compared without
regard to case. A pattern (``*``, ``?``, ``[...]``, ``**``) is matched one part at a
time, with ``..`` taken after the part before it.

What it sees is what a call names. A program that builds such a path as it runs is beyond
any reading of the call; so is anything a person allows. ``metabot.secret_lines`` reads
shell lines with this module. Standard library only: the gate hook imports it for every
call an engine makes.
"""

from __future__ import annotations

import fnmatch
import functools
import json
import os
import re
from collections.abc import Iterable, Mapping
from typing import Any

INSIDE, UNKNOWN, HOLDS = "inside", "unknown", "holds"
_RANK = {INSIDE: 3, UNKNOWN: 2, HOLDS: 1, None: 0}
ASKS = frozenset({INSIDE, UNKNOWN})
DEFAULT_KEY = os.path.join("~", ".orcha", "key")
KEY_ENV = "ORCHA_KEY_PATH"
CAP = 64  # alternatives a word or a pattern may stand for before it counts as unknown
# The links in /proc that lead to another process's folders and files, as that process
# sees them: what they name is the running program's, never this one's.
_MAGIC = re.compile(r"/proc/[^/]+/(?:cwd|root|fd|fdinfo|map_files|task|exe)(?:/|$)|/dev/fd(?:/|$)")
_LINKS = 40  # links followed in one path, as the kernel's limit
_CLASS = re.compile(r"\[[:=.]")  # [[:alpha:]], [=a=], [.a.]: read as any name
_PERCENT = re.compile(r"%([0-9A-Fa-f]{2})")
# claude's own tools, whose path fields are known; any other tool's every value is read.
OWN_TOOLS = frozenset(
    {
        "Read", "Write", "Edit", "MultiEdit", "NotebookRead", "NotebookEdit", "LS", "Glob",
        "Grep", "WebFetch", "WebSearch", "TodoWrite", "Task", "Agent", "Bash",
        "BashOutput", "KillShell", "ExitPlanMode",
    }
)  # fmt: skip
_PATH_FIELDS = ("file_path", "notebook_path", "path", "url")
# A connection call's values: how far the scan goes; past it the call asks.
MAX_LEAVES, MAX_DEPTH, MAX_PATHS = 4096, 16, 1024
_PATHLIKE = ("/", "~", "$HOME", "${HOME}", "file:")
_TOKEN = re.compile(r"""[^\s'"`;,()<>|&=+]+""")


def worst(*relations: str | None) -> str | None:
    """The relation that matters most: INSIDE, then UNKNOWN, then HOLDS."""
    return max(relations, key=_RANK.__getitem__, default=None)


def key_path() -> str:
    """Where the signing key is, as ``emerge.record.resolve_key_path`` finds it."""
    named = os.environ.get(KEY_ENV, "").strip()
    return os.path.expanduser(named or DEFAULT_KEY)


def _home() -> str:
    raw = os.environ.get("METABOT_HOME")
    path = os.path.expanduser(raw) if raw else os.path.join(os.path.expanduser("~"), ".metabot")
    return os.path.abspath(path)


def tidy(path: str) -> str:
    """``normpath``, with a leading ``//`` read as ``/``, as Linux reads it."""
    tidied = os.path.normpath(path)
    return "/" + tidied.lstrip("/") if tidied.startswith("//") else tidied


@functools.lru_cache(maxsize=16)
def _roots(home: str, key: str, _seen: tuple[int, int, int] | None) -> tuple[str, ...]:
    paths = [os.path.join(home, "secrets"), key]
    config = os.path.join(home, "config.json")
    if _seen is not None:
        with open(config, encoding="utf-8") as fh:
            data = json.load(fh)  # one that does not read stops the call: never fail open
        logins = data.get("logins") if isinstance(data, dict) else None
        if isinstance(logins, dict):
            for spec in logins.values():
                folder = spec.get("home") if isinstance(spec, dict) else None
                if isinstance(folder, str) and os.path.isabs(folder) and "\0" not in folder:
                    paths.append(folder)
    out = []
    for path in paths:
        named = tidy(os.path.abspath(path))
        out += [named.casefold(), os.path.realpath(named).casefold()]
    return tuple(dict.fromkeys(out))


def secret_roots() -> tuple[str, ...]:
    """Every guarded path, as named and as its links resolve, in case-folded form. Raises
    when config.json is there and does not read, so a caller fails closed."""
    home = _home()
    try:
        st = os.stat(os.path.join(home, "config.json"))
        seen: tuple[int, int, int] | None = (st.st_mtime_ns, st.st_size, st.st_ino)
    except FileNotFoundError:
        seen = None
    return _roots(home, tidy(os.path.abspath(key_path())), seen)


# ── one path ────────────────────────────────────────────────────────────────


def follow(path: str) -> str | None:
    """An absolute ``path`` as the kernel follows it, link by link; None when it leads
    through a process's own links in /proc, or loops."""
    done = "/"
    todo = [part for part in reversed(path.split("/")) if part]
    links = 0
    while todo:
        part = todo.pop()
        if part == ".":
            continue
        if part == "..":
            done = os.path.dirname(done)
            continue
        step = os.path.join(done, part)
        if _MAGIC.match(step):
            return None
        try:
            target = os.readlink(step)
        except (OSError, ValueError):  # not a link, or not there: taken as named
            done = step
            continue
        links += 1
        if links > _LINKS:
            return None
        if target.startswith("/"):
            done = "/"
        todo.extend(part for part in reversed(target.split("/")) if part)
    return done


def _against(form: str, roots: Iterable[str]) -> str | None:
    named = form.casefold()
    found = None
    for root in roots:
        if named == root or named.startswith(root.rstrip("/") + "/"):
            return INSIDE
        if named == "/" or root.startswith(named.rstrip("/") + "/"):
            found = HOLDS
    return found


def absolute_relation(path: str, roots: Iterable[str]) -> str | None:
    """How an absolute path stands to the roots, read every way it may be opened."""
    roots = tuple(roots)
    if "\0" in path:
        return None  # no file is named with one
    tidied = tidy(path)
    if _MAGIC.match(tidied):
        return UNKNOWN
    found = _against(tidied, roots)
    for form in (follow(path), follow(tidied)):
        found = worst(found, UNKNOWN if form is None else _against(form, roots))
    return found


def _spellings(path: str) -> list[str]:
    """A tool's path as written, and as a tool that expands ``file://``, ``~`` or
    ``$HOME`` reads it."""
    out = [path]
    if path[:7].lower() == "file://":
        rest = path[7:]
        if rest[:10].lower() == "localhost/":
            rest = rest[9:]
        out.append(_PERCENT.sub(lambda m: chr(int(m.group(1), 16)), rest))
    for text in list(out):
        if text.startswith("~"):
            out.append(os.path.expanduser(text))
        for name in ("$HOME", "${HOME}"):
            if text.startswith(name):
                out.append(os.path.expanduser("~") + text[len(name) :])
    return out


def path_relation(path: str, cwd: str, roots: Iterable[str] | None = None) -> str | None:
    """How a path a tool opens, relative to ``cwd``, stands to the roots."""
    roots = tuple(secret_roots() if roots is None else roots)
    found = None
    for text in _spellings(path):
        full = text if text.startswith("/") else os.path.join(cwd, text)
        found = worst(found, absolute_relation(full, roots))
    return found


# ── patterns ────────────────────────────────────────────────────────────────


def escape(text: str) -> str:
    """``text`` as a pattern that matches it alone."""
    return re.sub(r"([*?\[])", r"[\1]", text)


def has_magic(pattern: str) -> bool:
    return bool(re.search(r"[*?\[]", re.sub(r"\[[*?\[]\]", "", pattern)))


def part_matches(part: str, name: str, dots: bool) -> bool:
    """Whether one part of a pattern matches one name. A part that starts with ``*`` or
    ``?`` never matches a name that starts with a dot, unless ``dots``."""
    if not dots and part[:1] in ("*", "?") and name.startswith("."):
        return False
    if _CLASS.search(part):
        return True
    return fnmatch.fnmatchcase(name, part.replace("[^", "[!"))


def _stacks(pattern: str, dots: bool) -> set[tuple[str, ...]] | None:
    """The part lists an absolute pattern can walk to, ``..`` taken as it comes; None past
    CAP. A part that can match ``..`` (``.*``, as older shells match it) may step up."""
    stacks: set[tuple[str, ...]] = {()}
    for part in pattern.casefold().split("/"):
        if not part:
            continue
        nxt: set[tuple[str, ...]] = set()
        for stack in stacks:
            if part == ".":
                nxt.add(stack)
            elif part == "..":
                nxt.add(stack[:-1])
                if stack[-1:] == ("**",):
                    nxt.add(stack)  # a folder at any depth, then its parent
            else:
                nxt.add((*stack, part))
                if part_matches(part, "..", dots):
                    nxt.add(stack[:-1])
                if part_matches(part, ".", dots):
                    nxt.add(stack)
        stacks = nxt
        if len(stacks) > CAP:
            return None
    return stacks


def _walk(stack: tuple[str, ...], root: list[str], dots: bool) -> str | None:
    seen: set[tuple[int, int]] = set()
    todo = [(0, 0)]
    found = None
    while todo:
        at = todo.pop()
        if at in seen:
            continue
        seen.add(at)
        si, ri = at
        if ri == len(root):
            return INSIDE  # the root itself, and what follows may name anything in it
        if si == len(stack):
            found = HOLDS
            continue
        part = stack[si]
        if part == "**":
            todo += [(si + 1, ri), (si, ri + 1)]
        elif part_matches(part, root[ri], dots):
            todo.append((si + 1, ri + 1))
    return found


def pattern_relation(pattern: str, roots: Iterable[str], dots: bool = False) -> str | None:
    """How an absolute pattern stands to the roots, matched part by part: INSIDE when it
    can name a root or a path in one, HOLDS when it can name a folder above one."""
    if "\0" in pattern:
        return None
    if _MAGIC.search(pattern):
        return UNKNOWN
    stacks = _stacks(pattern, dots)
    if stacks is None:
        return UNKNOWN
    found = None
    for root in roots:
        parts = [p for p in root.split("/") if p]
        for stack in stacks:
            found = worst(found, _walk(stack, parts, dots))
            if found == INSIDE:
                return found
    return found


def braces(tokens: list[Any], cap: int = CAP) -> list[list[Any]] | None:
    """Brace expansion over a word's tokens, as the shell does it before anything else:
    ``{a,b}`` and ``{1..3}`` made of plain (unquoted) ``str`` tokens; None past ``cap``."""
    out: list[list[Any]] = []
    todo = [list(tokens)]
    while todo:
        word = todo.pop()
        span = _brace_span(word, cap)
        if span is None:
            out.append(word)
        else:
            start, end, alternatives = span
            if alternatives is None:
                return None
            todo += [word[:start] + alt + word[end + 1 :] for alt in alternatives]
        if len(out) + len(todo) > cap:
            return None
    return out


_SEQUENCE = re.compile(r"(-?\d+|[A-Za-z])\.\.(-?\d+|[A-Za-z])(?:\.\.(-?\d+))?")
Span = tuple[int, int, "list[list[Any]] | None"]


def _brace_span(word: list[Any], cap: int) -> Span | None:
    """The first ``{...}`` the shell expands: (start, end, its alternatives), with None for
    alternatives past ``cap``."""
    for start, token in enumerate(word):
        if token != "{":
            continue
        depth, commas = 0, list[int]()
        for end in range(start + 1, len(word)):
            t = word[end]
            if t == "{":
                depth += 1
            elif t == "}" and depth:
                depth -= 1
            elif t == "}":
                inner = word[start + 1 : end]
                if commas:
                    cuts = [start, *commas, end]
                    pairs = zip(cuts, cuts[1:], strict=False)
                    return start, end, [word[a + 1 : b] for a, b in pairs]
                plain = "".join(t for t in inner if isinstance(t, str) and len(t) == 1)
                seq = _SEQUENCE.fullmatch(plain) if len(plain) == len(inner) else None
                if seq is not None:
                    a, b, step = seq.groups()
                    items = _sequence(a, b, step, cap)
                    return start, end, None if items is None else [list(i) for i in items]
                break
            elif t == "," and not depth:
                commas.append(end)
    return None


def _sequence(a: str, b: str, step: str | None, cap: int) -> list[str] | None:
    by = abs(int(step)) if step and int(step) else 1
    if a.lstrip("-").isdigit() and b.lstrip("-").isdigit():
        lo, hi = int(a), int(b)
        values = range(lo, hi + (1 if hi >= lo else -1), by if hi >= lo else -by)
        if len(values) > cap:
            return None
        width = (
            max(len(a), len(b))
            if a.lstrip("-").startswith("0") or b.lstrip("-").startswith("0")
            else 0
        )
        return [str(v).zfill(width) if width else str(v) for v in values]
    if len(a) == len(b) == 1 and a.isalpha() and b.isalpha():
        lo, hi = ord(a), ord(b)
        values = range(lo, hi + (1 if hi >= lo else -1), by if hi >= lo else -by)
        return None if len(values) > cap else [chr(v) for v in values]
    return None


# ── text that may hold paths ────────────────────────────────────────────────


def text_relation(
    text: str, bases: Iterable[str], roots: Iterable[str], relative: bool = True
) -> str | None:
    """INSIDE or UNKNOWN when a word of ``text`` (code, a here-document, a value) that reads
    as a path names a root; ``relative``: words with a ``/`` or a leading ``.`` count too,
    read from each of ``bases``. Past MAX_PATHS such words, UNKNOWN."""
    roots, bases = tuple(roots), tuple(bases)
    checked = 0
    for token in _TOKEN.findall(text):
        for word in _spellings(token):
            if word.startswith("/"):
                fulls = [word]
            elif relative and ("/" in word or word.startswith(".")):
                fulls = [os.path.join(base, word) for base in bases]
            else:
                continue
            for full in fulls:
                checked += 1
                if checked > MAX_PATHS:
                    return UNKNOWN
                found = absolute_relation(full, roots)
                if has_magic(full):
                    found = worst(found, pattern_relation(full, roots, dots=True))
                if found in ASKS:
                    return found
    return None


# ── tool calls ──────────────────────────────────────────────────────────────


def tool_asks(tool: str, args: Mapping[str, Any], cwd: str | None = None) -> bool:
    """Whether one of an engine's own tool calls (claude's ``Read``, ``Grep``...) could reach
    a root. A path field counts as the tool opens it; text a tool writes, or a pattern it
    searches for, is no path. ``Grep`` reads the files under its folder (the one it runs
    in when none is named), so a folder above a root asks; ``Glob`` names them, so a
    pattern that can name a path in a root asks. Any other tool is read as a connection's."""
    if tool not in OWN_TOOLS:
        return connection_asks(args)
    where = cwd or os.getcwd()
    roots = secret_roots()
    found = None
    for field in _PATH_FIELDS:
        value = args.get(field)
        if isinstance(value, str) and value:
            found = worst(found, path_relation(value, where, roots))
    if found in ASKS:
        return True
    folder = args.get("path")
    named = folder if isinstance(folder, str) and folder else None
    if tool == "Grep":
        return worst(found, path_relation(named or where, where, roots)) is not None
    if tool == "Glob":
        pattern = args.get("pattern")
        if isinstance(pattern, str) and pattern:
            return _glob_asks(pattern, named or where, where, roots)
    return False


def _glob_asks(pattern: str, folder: str, cwd: str, roots: tuple[str, ...]) -> bool:
    alternatives = braces(list(pattern))
    if alternatives is None:
        return True
    tops = []
    for text in _spellings(folder):
        full = text if text.startswith("/") else os.path.join(cwd, text)
        forms = [tidy(full), follow(full), follow(tidy(full))]
        if None in forms:
            return True
        tops += [f for f in forms if f is not None]
    for alternative in alternatives:
        for text in _spellings("".join(alternative)):
            fulls = [text] if text.startswith("/") else [escape(t) + "/" + text for t in tops]
            if any(pattern_relation(full, roots, dots=True) in ASKS for full in fulls):
                return True
    return False


def builtin_asks(path: str, cwd: str) -> bool:
    """Whether a path the openai engine's ``read_file``, ``list_dir`` or ``write_file`` opens
    under ``cwd``, exactly as written, could reach a root."""
    return path_relation(path, cwd) in ASKS


def connection_asks(args: Mapping[str, Any]) -> bool:
    """Whether a connection call's arguments could name a root. Every string, keys too, is
    read: one that starts as a path (``/``, ``~``, ``$HOME``, ``file:``) as a path and as a
    pattern, and each word in it that does. A ``path`` may name a place on a server, so a
    folder above a root does not ask; past the scan's reach the call asks."""
    roots = secret_roots()
    leaves = 0
    todo: list[tuple[Any, int]] = [(args, 0)]
    while todo:
        value, depth = todo.pop()
        if depth > MAX_DEPTH:
            return True
        if isinstance(value, str):
            leaves += 1
            if leaves > MAX_LEAVES:
                return True
            text = value.strip()
            if text.startswith(_PATHLIKE) and path_relation(text, "/", roots) in ASKS:
                return True
            if text.startswith(_PATHLIKE) and has_magic(text):
                for spelling in _spellings(text):
                    if spelling.startswith("/") and pattern_relation(spelling, roots, True) in ASKS:
                        return True
            if text_relation(text, (), roots, relative=False) in ASKS:
                return True
        elif isinstance(value, Mapping):
            for key, item in value.items():
                todo += [(key, depth + 1), (item, depth + 1)]
        elif isinstance(value, (list, tuple)):
            todo += [(item, depth + 1) for item in value]
    return False
