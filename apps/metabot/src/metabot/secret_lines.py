"""Whether a shell line could reach MetaBot's guarded paths (``metabot.secret_paths``).

A line is read as the shell reads it: quotes, ``\\`` and continued lines; braces, then
``~`` (``~+`` is the folder the line is in); the variables it can know (``HOME``,
``PWD``, ``METABOT_HOME``, ``ORCHA_KEY_PATH``, and the line's own assignments and ``for``
words); patterns matched one part at a time; and ``cd`` targets as folders later words are
read from. A word whose text is made as the line runs (another variable, ``$(...)``,
``$'...'``, ``${X:-y}``, ``/proc/self/cwd``) is unknown.

The line asks when:

- any word names a guarded path or a path in one;
- a command that reads, copies, archives or writes files (``cat``, ``cp``, ``tar``,
  ``tee``, an interpreter) has an unknown word, or a word read from an unknown folder;
- a recursive reader (``grep -r``, ``rg``) reads a folder above one, the folder it runs in
  included, or a copy or an archive takes one in;
- a word names a folder above one and the line hands names on (``xargs``, ``read``,
  ``--files0-from``).

Command substitutions, ``sh -c`` and ``env -S`` strings, ``find -exec`` clauses and
here-documents fed to a shell are read as lines of their own; code given to an
interpreter (``python -c``, ``awk``, a here-document fed to ``python``) is searched for
words that name one.
"""

from __future__ import annotations

import glob
import os
import re
from typing import Any

from metabot.secret_paths import (
    ASKS,
    CAP,
    HOLDS,
    INSIDE,
    UNKNOWN,
    absolute_relation,
    braces,
    escape,
    has_magic,
    pattern_relation,
    secret_roots,
    text_relation,
    tidy,
    worst,
)
from metabot.shell_policy import (
    _HEADERS,
    _HEREDOC,
    _INTERPRETER,
    _MARK,
    _RESERVED,
    _SHELLS,
    _SOURCE,
    _WRAPPERS,
    _XARGS_VALUES,
    _env_options,
    _lex,
    _redirect_at,
    _shell_string,
    _skip_options,
    _split,
)

MAX_DEPTH = 12
_BASES = 32  # folders a line may move to before it counts as moving anywhere
_DISK = 512  # names a pattern may match on disk before it counts as unknown
Alt = tuple[str, str, bool]  # (the text, the text as a pattern, whether it is one)
_GREPS = frozenset(
    {"grep", "egrep", "fgrep", "zgrep", "zegrep", "zfgrep", "bzgrep", "xzgrep", "zstdgrep"}
)
_RGS = frozenset({"rg", "ag", "ack", "ack-grep", "pt", "ugrep", "ug", "sift"})
_SEDS = frozenset({"sed", "gsed"})
_AWKS = frozenset({"awk", "gawk", "mawk", "nawk"})
_JQS = frozenset({"jq", "gojq", "jaq", "yq"})
_READERS = frozenset(
    {
        "cat", "tac", "head", "tail", "less", "more", "most", "nl", "od", "xxd", "hexdump",
        "hd", "base64", "base32", "basenc", "strings", "rev", "fold", "fmt", "pr", "expand",
        "unexpand", "cut", "paste", "join", "comm", "diff", "diff3", "sdiff", "cmp", "sort",
        "uniq", "wc", "md5sum", "sha1sum", "sha224sum", "sha256sum", "sha384sum",
        "sha512sum", "b2sum", "cksum", "sum", "shasum", "file", "column", "iconv",
        "dos2unix", "unix2dos", "zcat", "gzcat", "bzcat", "xzcat", "zstdcat", "lz4cat",
        "zless", "zmore", "zdiff", "zcmp", "bat", "batcat", "vim", "vi", "view", "nvim",
        "nano", "emacs", "ed", "ex", "look", "split", "csplit", "shuf", "openssl", "gpg",
        "gpg2", "age", "ssh-keygen", "xmllint", "mlr",
    }
) | _GREPS | _RGS | _SEDS | _AWKS | _JQS  # fmt: skip
_WRITERS = frozenset({"tee", "touch", "chmod", "chown", "chgrp", "sponge"})
_COPIERS = frozenset({"cp", "mv", "ln", "install", "rsync", "scp"})
_ARCHIVERS = frozenset(
    {
        "tar", "zip", "7z", "7za", "7zr", "rar", "jar", "cpio", "pax", "rclone", "restic",
        "borg", "duplicity", "ar", "mksquashfs", "genisoimage", "mkisofs", "xorriso",
        "docker", "podman", "nerdctl", "gzip", "gunzip", "bzip2", "bunzip2", "xz", "unxz",
        "zstd", "unzstd", "lz4", "pigz",
    }
)  # fmt: skip
_CONTENT = _READERS | _WRITERS | _COPIERS | _ARCHIVERS | _SOURCE | _SHELLS
_FIND_RUNS = frozenset({"-exec", "-execdir", "-ok", "-okdir"})
_FIND_WRITES = ("-fprint", "-fls")
_FD_RUNS = frozenset({"-x", "--exec", "-X", "--exec-batch"})
_SETTERS = frozenset({"read", "mapfile", "readarray", "getopts", "let", "printf"})
_DECLARERS = frozenset({"export", "local", "declare", "typeset", "readonly"})
_NAMES_IN = frozenset({"read", "mapfile", "readarray", "cpio"})
_FILES_FROM = re.compile(r"--(?:files0-from|files-from|from-file|arg-file)(?:=|$)")
_SELF_SET = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*):?=")
_DOTS = re.compile(r"dotglob|globdots|glob_dots|GLOBIGNORE", re.IGNORECASE)
_MIDWORD = re.compile(r"[^\s;&|()<>=]\(")  # `@(...)`, `k(e|x)y`, `f()`: patterns, or a function
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SPECIAL = frozenset("@*#?-$!0123456789")
_KNOWN = ("METABOT_HOME", "ORCHA_KEY_PATH")
# The word of a command that is its program or pattern rather than a path, and the options
# that take values before it (with how many). An option in ``given`` names it instead.
_FIRST: dict[frozenset[str], tuple[str, dict[str, int]]] = {
    _GREPS: (
        "ef",
        {
            "-e": 1, "-f": 1, "-m": 1, "-A": 1, "-B": 1, "-C": 1, "-d": 1, "-D": 1,
            "--regexp": 1, "--file": 1, "--max-count": 1, "--after-context": 1,
            "--before-context": 1, "--context": 1, "--directories": 1, "--devices": 1,
            "--include": 1, "--exclude": 1, "--exclude-dir": 1, "--label": 1,
        },
    ),
    _RGS: (
        "ef",
        {
            "-e": 1, "-f": 1, "-g": 1, "-t": 1, "-T": 1, "-m": 1, "-A": 1, "-B": 1, "-C": 1,
            "-j": 1, "-M": 1, "-E": 1, "-r": 1, "-d": 1, "--glob": 1, "--iglob": 1,
            "--type": 1, "--type-not": 1, "--max-count": 1, "--context": 1,
            "--after-context": 1, "--before-context": 1, "--threads": 1, "--max-columns": 1,
            "--encoding": 1, "--replace": 1, "--regexp": 1, "--file": 1, "--max-depth": 1,
            "--max-filesize": 1, "--sort": 1, "--sortr": 1, "--type-add": 1, "--ignore-file": 1,
        },
    ),
    _SEDS: ("ef", {"-e": 1, "-f": 1, "-l": 1, "--expression": 1, "--file": 1}),
    _AWKS: ("f", {"-f": 1, "-v": 1, "-F": 1, "--file": 1, "--assign": 1}),
    _JQS: (
        "f",
        {"--arg": 2, "--argjson": 2, "--slurpfile": 2, "--rawfile": 2, "--indent": 1, "-L": 1},
    ),
}  # fmt: skip
# Options whose value is a count or a field, never a path.
_COUNTS: dict[str, frozenset[str]] = {
    "head": frozenset({"-n", "-c", "--lines", "--bytes"}),
    "tail": frozenset({"-n", "-c", "--lines", "--bytes", "-s", "--sleep-interval"}),
    "cut": frozenset({"-d", "-f", "-c", "-b", "--delimiter", "--fields"}),
    "sort": frozenset({"-k", "-t", "-S", "--key", "--field-separator", "--buffer-size"}),
    "uniq": frozenset({"-f", "-s", "-w"}),
    "fold": frozenset({"-w"}),
    "shuf": frozenset({"-n"}),
    "xxd": frozenset({"-l", "-s", "-c", "-g"}),
    "od": frozenset({"-N", "-j", "-w", "-t"}),
    "base64": frozenset({"-w"}),
}


class _Ask(Exception):
    """The line asks."""


class _State:
    __slots__ = ("holds", "names")

    def __init__(self) -> None:
        self.holds = False  # a word names a folder above a guarded path
        self.names = False  # the line hands names on to another command


class _Ctx:
    """What a line knows as it is read: where its words are read from, and its variables."""

    __slots__ = ("roots", "bases", "lost", "homes", "assigned", "unknown", "dots", "odd", "depth")

    def __init__(self, roots: tuple[str, ...], cwd: str) -> None:
        self.roots = roots
        self.bases = [cwd]
        self.lost = False  # the line may move to a folder no reading can name
        self.homes = [os.path.expanduser("~")]
        self.assigned: dict[str, list[Alt]] = {}
        self.unknown: set[str] = set()
        self.dots = False  # patterns match names that start with a dot
        self.odd = False  # a `(` inside a word: a pattern this reading does not follow
        self.depth = 0

    def child(self) -> _Ctx:
        out = _Ctx.__new__(_Ctx)
        out.roots, out.homes, out.dots, out.odd, out.lost = (
            self.roots,
            self.homes,
            self.dots,
            self.odd,
            self.lost,
        )
        out.bases = list(self.bases)
        out.assigned = {k: list(v) for k, v in self.assigned.items()}
        out.unknown = set(self.unknown)
        out.depth = self.depth + 1
        if out.depth > MAX_DEPTH:
            raise _Ask
        return out

    def seen(self) -> tuple[Any, ...]:
        return (
            tuple(self.bases),
            self.lost,
            tuple(sorted((k, tuple(v)) for k, v in self.assigned.items())),
            frozenset(self.unknown),
        )

    def values(self, name: str) -> list[Alt] | None:
        """What ``$name`` may be; None when it may be anything."""
        if name in self.unknown:
            return None
        if name == "HOME":
            found = [(h, escape(h), False) for h in self.homes]
        elif name == "PWD":
            if self.lost:
                return None
            found = [(b, escape(b), False) for b in self.bases]
        elif name in _KNOWN:
            value = os.environ.get(name, "")
            found = [(value, escape(value), False)]
        elif name in self.assigned and name not in os.environ:
            found = [("", "", False)]  # unset until the line sets it
        else:
            return None  # the shell's environment, positional words: not this reading's
        return found + self.assigned.get(name, [])

    def tilde(self, name: str) -> list[str] | None:
        if "HOME" in self.unknown and name == "":
            return None
        if name == "":
            return self.homes + [v[0] for v in self.assigned.get("HOME", []) if not v[2]]
        if name == "+":
            return None if self.lost else list(self.bases)
        if name == "-" or re.fullmatch(r"[+-]?\d+", name):
            return None
        expanded = os.path.expanduser("~" + name)
        return None if expanded.startswith("~") else [expanded]

    def assign(self, name: str, alts: list[Alt] | None) -> None:
        if alts is None:
            self.unknown.add(name)
            return
        held = self.assigned.setdefault(name, [])
        for alt in alts:
            if alt not in held:
                held.append(alt)
        if len(held) > CAP:
            self.unknown.add(name)

    def move(self, folders: list[str]) -> None:
        for folder in folders:
            if folder not in self.bases:
                self.bases.append(folder)
        if len(self.bases) > _BASES:
            self.lost = True


# ── reading words ───────────────────────────────────────────────────────────
# A word is a list of tokens: a plain (unquoted) character as a one-character str,
# ("q", ch) for a quoted one, ("v", name, quoted) for a variable, ("u",) for text made as
# the line runs.


def _read(segment: str) -> list[tuple[str, Any]] | None:
    """A segment's words ("w", tokens) and redirections ("r", op); None when a quote is
    not closed."""
    items: list[tuple[str, Any]] = []
    toks: list[Any] = []
    started = False
    i, n = 0, len(segment)
    while i < n:
        c = segment[i]
        if c in " \t\n":
            if started:
                items.append(("w", toks))
            toks, started = [], False
            i += 1
            continue
        started = True
        if c == "\\":
            if segment[i + 1 : i + 2] == "\n":
                i += 2  # a continued line
                started = bool(toks)
                continue
            if i + 1 < n:
                toks.append(("q", segment[i + 1]))
            i += 2
        elif c == "'":
            end = segment.find("'", i + 1)
            if end < 0:
                return None
            toks += [("q", ch) for ch in segment[i + 1 : end]]
            i = end + 1
        elif segment.startswith("$'", i):
            j = i + 2
            while j < n and segment[j] != "'":
                j += 2 if segment[j] == "\\" else 1
            if j >= n:
                return None
            toks.append(("u",))  # $'...': escapes this reading does not decode
            i = j + 1
        elif c == '"' or segment.startswith('$"', i):
            closed = _double(segment, i + (2 if c == "$" else 1), toks)
            if closed is None:
                return None
            i = closed
        elif c == "$":
            i = _dollar(segment, i, toks, quoted=False)
        elif c == _MARK:
            i = _marker(segment, i, toks)
        elif c in "<>&" and (op := _redirect_at(segment, i)):
            if toks and all(isinstance(t, str) and t.isdigit() for t in toks):
                toks = []  # the descriptor it redirects
            elif toks:
                items.append(("w", toks))
                toks = []
            started = False
            items.append(("r", op))
            i += len(op)
        else:
            toks.append(c)
            i += 1
    if started:
        items.append(("w", toks))
    return items


def _double(s: str, j: int, toks: list[Any]) -> int | None:
    n = len(s)
    while j < n:
        d = s[j]
        if d == '"':
            return j + 1
        if d == "\\" and j + 1 < n and s[j + 1] in '$`"\\\n':
            if s[j + 1] != "\n":
                toks.append(("q", s[j + 1]))
            j += 2
        elif d == "$":
            j = _dollar(s, j, toks, quoted=True)
        elif d == _MARK:
            j = _marker(s, j, toks)
        else:
            toks.append(("q", d))
            j += 1
    return None


def _dollar(s: str, i: int, toks: list[Any], quoted: bool) -> int:
    n = len(s)
    nxt = s[i + 1 : i + 2]
    if nxt == "{":
        depth, j = 1, i + 2
        while j < n and depth:
            depth += {"{": 1, "}": -1}.get(s[j], 0)
            j += 1
        inner = s[i + 2 : j - 1]
        toks.append(("v", inner, quoted) if not depth and _NAME.fullmatch(inner) else ("u",))
        return j
    if nxt and (nxt.isalpha() or nxt == "_"):
        match = _NAME.match(s, i + 1)
        assert match is not None
        j = match.end()
        odd = s[j : j + 1] == "[" or (s[j : j + 1] == ":" and s[j + 1 : j + 2].isalpha())
        toks.append(("u",) if odd else ("v", match.group(), quoted))  # zsh's $X:h, $X[1]
        return j
    if nxt and nxt in _SPECIAL:
        toks.append(("u",))
        return i + 2
    toks.append(("q", "$") if quoted else "$")
    return i + 1


def _marker(s: str, i: int, toks: list[Any]) -> int:
    end = s.find(_MARK, i + 1)
    toks.append(("u",))
    return len(s) if end < 0 else end + 1


def _text(tokens: list[Any]) -> str:
    """A word as plain text, for names and options: a variable as ``$NAME``."""
    out = []
    for t in tokens:
        if isinstance(t, str):
            out.append(t)
        elif t[0] == "q":
            out.append(t[1])
        elif t[0] == "v":
            out.append("$" + t[1])
        else:
            out.append(_MARK + "u" + _MARK)
    return "".join(out)


def _assignment(tokens: list[Any]) -> tuple[str, list[Any], bool] | None:
    """(name, value, appends) for a word ``NAME=VALUE`` (``NAME+=VALUE``)."""
    k = 0
    while (
        k < len(tokens) and isinstance(tokens[k], str) and (tokens[k].isalnum() or tokens[k] == "_")
    ):
        k += 1
    name = "".join(tokens[:k])
    if not _NAME.fullmatch(name):
        return None
    if tokens[k : k + 2] == ["+", "="]:
        return name, tokens[k + 2 :], True
    if tokens[k : k + 1] == ["="]:
        return name, tokens[k + 1 :], False
    return None


# ── expanding words ─────────────────────────────────────────────────────────


def _tildes(
    word: list[Any], ctx: _Ctx, assignment: bool, value: bool = False
) -> list[list[Any]] | None:
    """``~`` at a word's start (and, in an assignment, after ``=`` and each ``:``);
    ``value``: the word is an assignment's value alone."""
    outs: list[list[Any]] = [[]]
    i, n = 0, len(word)
    start, after_eq = True, value
    while i < n:
        tok = word[i]
        if tok == "~" and start:
            j = i + 1
            while j < n and word[j] != "/" and not (after_eq and word[j] == ":"):
                j += 1
            prefix = word[i + 1 : j]
            if all(isinstance(t, str) for t in prefix):
                values = ctx.tilde("".join(prefix))
                if values is None:
                    return None
                outs = [o + [("q", ch) for ch in v] for o in outs for v in values]
                if len(outs) > CAP:
                    return None
                i, start = j, False
                continue
        outs = [[*o, tok] for o in outs]
        start = False
        if assignment and tok == "=" and not after_eq:
            start = after_eq = True
        elif after_eq and tok == ":":
            start = True
        i += 1
    return outs


def _expand(
    tokens: list[Any], ctx: _Ctx, assignment: bool = False, value: bool = False
) -> list[Alt] | None:
    """What a word may stand for once expanded; None when it may be anything. ``value``:
    an assignment's value alone, which the shell neither splits nor matches as a pattern."""
    assignment = assignment or value
    alternatives = [list(tokens)] if assignment else braces(tokens)
    if alternatives is None:
        return None
    out: list[Alt] = []
    for alternative in alternatives:
        tilded = _tildes(alternative, ctx, assignment, value)
        if tilded is None:
            return None
        for word in tilded:
            parts: list[Alt] = [("", "", False)]
            for tok in word:
                if isinstance(tok, str):
                    magic = tok in "*?[" and not assignment
                    pat = escape(tok) if assignment else tok
                    parts = [(a + tok, p + pat, g or magic) for a, p, g in parts]
                elif tok[0] == "q":
                    parts = [(a + tok[1], p + escape(tok[1]), g) for a, p, g in parts]
                elif tok[0] == "v":
                    values = ctx.values(tok[1])
                    if values is None:
                        return None
                    grown: list[Alt] = []
                    for a, p, g in parts:
                        for va, vp, vg in values:
                            if vg:  # a `for` word's pattern: any name it matched
                                grown.append((a + va, p + vp, True))
                            elif tok[2] or assignment:
                                grown.append((a + va, p + escape(va), g))
                            elif any(ch in va for ch in " \t\n"):
                                return None  # split into words this reading does not follow
                            else:
                                grown.append((a + va, p + va, g or has_magic(va)))
                    parts = grown
                else:
                    return None
                if len(parts) > CAP:
                    return None
            out += parts
            if len(out) > CAP:
                return None
    return out


def _pieces(text: str) -> list[str]:
    """The parts of a word a program may read as a path: after ``--x=``, ``@``, ``:``."""
    cuts = [m.start() for m in re.finditer(r"[=:,@]", text)]
    if not cuts:
        return []
    out = set(re.split(r"[=:,@]", text))
    out.update(text[c + 1 :] for c in cuts)
    out.update(text[:c] for c in cuts)
    out.discard("")
    out.discard(text)
    return sorted(out)[:32]


def _on_disk(pattern: str, ctx: _Ctx) -> str | None:
    if "**" in pattern:
        return None  # the part-by-part walk covers it; a deep walk is too slow for a gate
    found = None
    for count, path in enumerate(glob.iglob(pattern, include_hidden=ctx.dots)):
        if count >= _DISK:
            return UNKNOWN
        found = worst(found, absolute_relation(path, ctx.roots))
        if found == INSIDE:
            break
    return found


def _relation(
    alts: list[Alt] | None, ctx: _Ctx, bases: list[str], lost: bool, disk: bool
) -> str | None:
    if alts is None:
        return UNKNOWN
    found = None
    for literal, pattern, globbed in alts:
        if "\0" in literal:
            continue  # no file is named with one
        if literal.startswith("/"):
            fulls = [(literal, pattern)]
        else:
            if lost:
                found = worst(found, UNKNOWN)
            fulls = [(f"{b}/{literal}", f"{escape(b)}/{pattern}") for b in bases]
        for full, pat in fulls:
            if globbed:
                found = worst(found, pattern_relation(pat, ctx.roots, ctx.dots))
                if disk:
                    found = worst(found, _on_disk(pat, ctx))
            else:
                found = worst(found, absolute_relation(full, ctx.roots))
        if not globbed:
            for piece in _pieces(literal):
                for text in {piece, os.path.expanduser(piece)}:
                    heads = [text] if text.startswith("/") else [f"{b}/{text}" for b in bases]
                    for head in heads:
                        found = worst(found, absolute_relation(head, ctx.roots))
        if found == INSIDE:
            return found
    return found


def _check(
    word: list[Any],
    ctx: _Ctx,
    state: _State,
    where: tuple[list[str], bool] | None = None,
    *,
    unknown: bool = False,
    holds: bool = False,
    disk: bool = False,
    assignment: bool = False,
) -> list[Alt] | None:
    """Ask when ``word`` names a guarded path; when it may (``unknown``); when it names a
    folder above one (``holds``). Returns what it stands for."""
    bases, lost = where or (ctx.bases, ctx.lost)
    alts = _expand(word, ctx, assignment)
    found = _relation(alts, ctx, bases, lost, disk)
    if found == INSIDE or (found == UNKNOWN and unknown) or (found == HOLDS and holds):
        raise _Ask
    if found == HOLDS:
        state.holds = True
    if assignment and alts:  # a value the line later splits into words
        for literal, _, _ in alts:
            for field in literal.split():
                if (
                    field != literal
                    and _relation([(field, escape(field), False)], ctx, bases, lost, False)
                    == INSIDE
                ):
                    raise _Ask
    return alts


# ── the line ────────────────────────────────────────────────────────────────


def line_asks(command: str, cwd: str | None = None) -> bool:
    """Whether a shell line run in ``cwd`` could reach a guarded path (see the module)."""
    ctx = _Ctx(secret_roots(), tidy(os.path.abspath(cwd or os.getcwd())))
    state = _State()
    try:
        _line(command, ctx, state)
    except _Ask:
        return True
    return state.holds and state.names


def _line(command: str, ctx: _Ctx, state: _State) -> None:
    lexed = _lex(command)
    if lexed is None:
        raise _Ask
    text, inner, _, bodies = lexed
    segments = _split(text)
    if segments is None:
        raise _Ask
    parsed = []
    for segment, _piped in segments:
        items = _read(segment)
        if items is None:
            raise _Ask
        parsed.append(_sorted(items))
    ctx = _prepare(parsed, ctx, text, command)
    for sub in inner:
        _line(sub, ctx.child(), state)
    for words, redirects in parsed:
        name = _simple(words, redirects, ctx, bodies, state)
        if name in _SOURCE:
            ctx.lost = True  # a sourced file may move, or set any variable
            ctx.unknown.update(ctx.assigned)


def _sorted(items: list[tuple[str, Any]]) -> tuple[list[list[Any]], list[tuple[str, list[Any]]]]:
    words, redirects = [], []
    i = 0
    while i < len(items):
        kind, value = items[i]
        if kind == "r":
            if i + 1 >= len(items) or items[i + 1][0] != "w":
                raise _Ask  # an operator with nothing to redirect to
            redirects.append((value, items[i + 1][1]))
            i += 2
            continue
        words.append(value)
        i += 1
    return words, redirects


def _bare(text: str) -> str:
    """``text`` with its quoted parts blanked."""
    out, i, n, double = [], 0, len(text), False
    while i < n:
        c = text[i]
        if c == "\\":
            out.append("  "[: n - i])
            i += 2
        elif c == "'" and not double:
            end = text.find("'", i + 1)
            end = n - 1 if end < 0 else end
            out.append(" " * (end - i + 1))
            i = end + 1
        else:
            if c == '"':
                double = not double
            out.append(" " if double or c == '"' else c)
            i += 1
    return "".join(out)


def _prepare(
    parsed: list[tuple[list[list[Any]], list[Any]]], parent: _Ctx, text: str, raw: str
) -> _Ctx:
    """The line's variables and folders, before any word is judged: a value set anywhere in
    the line counts everywhere in it, since a loop runs its words in any order. Each round
    moves from the folders the line starts in, so a relative ``cd`` moves once; one inside
    a loop may move again and again, so it moves anywhere."""
    ctx = parent.child()
    ctx.depth -= 1
    ctx.dots = ctx.dots or bool(_DOTS.search(raw))
    ctx.odd = ctx.odd or bool(_MIDWORD.search(_bare(text)))
    ctx.unknown.update(m.group(1) for m in _SELF_SET.finditer(raw))
    for _round in range(4):
        before = ctx.seen()
        ctx.bases, ctx.lost = list(parent.bases), parent.lost
        loops = 0
        for words, _ in parsed:
            loops = _learn(words, ctx, loops)
        if ctx.seen() == before:
            return ctx
    ctx.unknown.update(ctx.assigned)  # still growing: a loop this reading does not unwind
    ctx.lost = True
    return ctx


def _command_index(texts: list[str], k: int) -> int:
    """Where the command is, past assignments and wrappers (``time``, ``nice``, ``env``)."""
    while k < len(texts):
        name = os.path.basename(texts[k])
        if name in _WRAPPERS:
            rest, _ = _skip_options(texts[k + 1 :], _WRAPPERS[name])
            k = len(texts) - len(rest) + (1 if name == "timeout" and rest else 0)
        elif name == "env":
            rest, _ = _env_options(texts[k + 1 :])
            k = len(texts) - len(rest)
            while k < len(texts) and "=" in texts[k] and _NAME.match(texts[k]):
                k += 1
        else:
            return k
    return k


def _learn(words: list[list[Any]], ctx: _Ctx, loops: int) -> int:
    """Learn one segment's assignments and moves; ``loops``: the ``do`` bodies it is in,
    returned for the next segment."""
    texts = [_text(w) for w in words]
    k = 0
    while k < len(words) and texts[k] in _RESERVED:
        loops += {"do": 1, "done": -1}.get(texts[k], 0)
        k += 1
    if k >= len(words):
        return loops
    if texts[k] in ("for", "select"):
        if k + 1 < len(words):
            if texts[k + 2 : k + 3] == ["in"]:
                for word in words[k + 3 :]:
                    ctx.assign(texts[k + 1], _expand(word, ctx))
            else:
                ctx.unknown.add(texts[k + 1])
        return loops
    if texts[k] in _HEADERS or texts[k] == "function":
        return loops
    while k < len(words) and (found := _assignment(words[k])) is not None:
        name, value, appends = found
        ctx.assign(name, None if appends else _expand(value, ctx, value=True))
        k += 1
    k = _command_index(texts, k)
    if k >= len(words):
        return loops
    command = os.path.basename(texts[k])
    if command in _DECLARERS:
        nameref = any(t.startswith("-") and "n" in t for t in texts[k + 1 :])
        for word, t in zip(words[k + 1 :], texts[k + 1 :], strict=True):
            found = _assignment(word)
            if found is not None:
                name, value, appends = found
                ctx.assign(name, None if appends or nameref else _expand(value, ctx, value=True))
            elif nameref:
                ctx.unknown.add(t)
    elif command in _SETTERS:
        ctx.unknown.update(t.split("=", 1)[0] for t in texts[k + 1 :])
        ctx.unknown.update({"REPLY", "MAPFILE", "OPTARG"})
    elif command in ("cd", "pushd"):
        _chdir(words[k + 1 :], texts[k + 1 :], ctx, loops > 0)
    elif command == "popd":
        ctx.lost = True
    return loops


def _chdir(words: list[list[Any]], texts: list[str], ctx: _Ctx, looped: bool) -> None:
    i = 0
    while i < len(texts) and texts[i].startswith("-") and texts[i] != "-":
        i += 1
        if texts[i - 1] == "--":
            break
    if i >= len(texts):
        ctx.move(list(ctx.homes))  # `cd` alone goes home
        return
    alts = None if texts[i] == "-" else _expand(words[i], ctx)
    if alts is None:
        ctx.lost = True
        return
    for literal, _, globbed in alts:
        if globbed or "\0" in literal:
            ctx.lost = True
        elif literal.startswith("/"):
            ctx.move([tidy(literal)])
        elif "CDPATH" in os.environ or "CDPATH" in ctx.assigned or looped:
            ctx.lost = True  # found under any CDPATH folder, or moved to again in a loop
        else:
            ctx.move([tidy(f"{b}/{literal}") for b in ctx.bases])


# ── one command ─────────────────────────────────────────────────────────────


def _family(name: str) -> frozenset[str] | None:
    return next((f for f in _FIRST if name in f), None)


def _code_at(name: str, texts: list[str], k: int) -> int | None:
    """The index of the pattern, script or program a command takes as its first word
    (``grep``'s pattern, ``awk``'s program); None when an option names it."""
    family = _family(name)
    if family is None:
        return None
    given, values = _FIRST[family]
    i = k + 1
    while i < len(texts):
        t = texts[i]
        if t == "--":
            i += 1
            break
        if not t.startswith("-") or t == "-":
            break
        flag = t.split("=", 1)[0]
        if flag in {f"-{g}" for g in given} or flag in ("--regexp", "--file", "--expression"):
            return None
        if not t.startswith("--") and any(ch in given for ch in t[1:]):
            return None
        if flag in values and "=" not in t and len(t) == len(flag):
            i += values[flag]
        i += 1
    return i if i < len(texts) else None


def _recursive(name: str, texts: list[str]) -> bool:
    if name in _RGS:
        return True
    if name not in _GREPS and name != "diff":
        return False
    for i, t in enumerate(texts):
        if t.startswith("--"):
            flag, _, value = t[2:].partition("=")
            if flag and ("recursive".startswith(flag) or "dereference-recursive".startswith(flag)):
                return True
            if flag and name != "diff" and "directories".startswith(flag):
                value = value or (texts[i + 1] if i + 1 < len(texts) else "")
                if value and "recurse".startswith(value):
                    return True
        elif t.startswith("-") and len(t) > 1:
            letters = t[1:]
            if "r" in letters or "R" in letters:
                return True
            if name != "diff" and "d" in letters:
                value = letters.split("d", 1)[1] or (texts[i + 1] if i + 1 < len(texts) else "")
                if value and "recurse".startswith(value):
                    return True
    return False


def _counted(name: str, texts: list[str], i: int) -> bool:
    """Whether word ``i`` is a count or a field (``head -n "$N"``), never a path."""
    counts = _COUNTS.get(name, frozenset())
    flag = texts[i].split("=", 1)[0]
    if flag in counts or any(texts[i].startswith(c) and len(c) == 2 for c in counts):
        return True
    return i > 0 and texts[i - 1] in counts


def _simple(
    words: list[list[Any]],
    redirects: list[tuple[str, list[Any]]],
    ctx: _Ctx,
    bodies: list[str],
    state: _State,
) -> str:
    """Judge one simple command; returns its name."""
    texts = [_text(w) for w in words]
    where = (ctx.bases, ctx.lost)
    k = 0
    while k < len(words) and texts[k] in _RESERVED:
        k += 1
    if k < len(words) and (texts[k] in _HEADERS or texts[k] == "function"):
        for word in words:
            _check(word, ctx, state)
        _redirect(redirects, "", texts, k, ctx, bodies, state, where)
        return ""
    while k < len(words) and _assignment(words[k]) is not None:
        _check(words[k], ctx, state, assignment=True)
        k += 1
    names_in = False
    plain = list(range(k))  # words judged only for what they name
    while k < len(words):
        name = os.path.basename(texts[k])
        if name == "command" and texts[k + 1 : k + 2] in (["-v"], ["-V"]):
            plain += range(k, len(words))
            k = len(words)
            break
        if name in _WRAPPERS:
            rest, _ = _skip_options(texts[k + 1 :], _WRAPPERS[name])
            stop = len(texts) - len(rest) + (1 if name == "timeout" and rest else 0)
        elif name == "env":
            rest, split = _env_options(texts[k + 1 :])
            stop = len(texts) - len(rest)
            for i in range(k + 1, stop):
                if texts[i] in ("-C", "--chdir") and i + 1 < stop:
                    where = _moved(_check(words[i + 1], ctx, state), where)
                elif texts[i].startswith(("--chdir=", "-C")) and len(texts[i]) > 2:
                    where = (where[0], True)  # a folder joined to its option: not followed
            while stop < len(words) and _assignment(words[stop]) is not None:
                _check(words[stop], ctx, state, assignment=True)
                stop += 1
            if split is not None:  # `env -S STRING`: the string is a line
                _line(" ".join([split, *texts[stop:]]), ctx.child(), state)
        elif name in ("xargs", "parallel"):
            names_in = True
            rest, _ = _skip_options(texts[k + 1 :], _XARGS_VALUES)
            stop = len(texts) - len(rest)
            if any(t in ("-a", "--arg-file") or _FILES_FROM.match(t) for t in texts[k:stop]):
                plain += range(k, stop)
                k = stop
                if k < len(words) and os.path.basename(texts[k]) in _CONTENT:
                    raise _Ask  # names from a file: nothing this reading sees
                continue
        else:
            break
        plain += range(k, stop)
        k = stop
    name = os.path.basename(texts[k]) if k < len(words) else ""
    if names_in or name in _NAMES_IN or any(_FILES_FROM.match(t) for t in texts[k:]):
        state.names = True
    if name == "tar" and any(t == "-T" for t in texts[k:]):
        state.names = True
    for i in plain:
        _check(words[i], ctx, state, where)
    if k >= len(words):
        _redirect(redirects, "", texts, k, ctx, bodies, state, where)
        return ""
    _check(words[k], ctx, state, where)
    args = texts[k + 1 :]
    code = (
        name in _SEDS or name in _AWKS or (bool(_INTERPRETER.match(name)) and name not in _SHELLS)
    )
    recursive = _recursive(name, args)
    archiver = name in _ARCHIVERS
    if name == "find":
        archiver = any(t in _FIND_RUNS or t.startswith(_FIND_WRITES) for t in args)
        _find_clauses(words, texts, k, ctx, bodies, state)
    if name in ("fd", "fdfind"):
        archiver = any(t in _FD_RUNS for t in args)
    content = code or recursive or archiver or name in _CONTENT
    if content and ctx.odd:
        raise _Ask
    at = _code_at(name, texts, k)
    sources: set[int] = set()
    if name in _COPIERS:
        operands = [i for i in range(k + 1, len(words)) if not texts[i].startswith("-")]
        target = any(t == "-t" or t.startswith("--target-directory") for t in args)
        if target:
            after_t = {i + 1 for i in range(k + 1, len(words)) if texts[i] == "-t"}
            sources = {i for i in operands if i not in after_t}
        else:
            sources = set(operands[:-1])
    for i in range(k + 1, len(words)):
        is_code = i == at
        alts = _check(
            words[i],
            ctx,
            state,
            where,
            unknown=content and not is_code and not _counted(name, texts, i),
            holds=archiver or (recursive and not is_code) or i in sources,
            disk=content,
        )
        if code:
            for literal, _, _ in alts or [(_text(words[i]), "", False)]:
                if text_relation(literal, where[0], ctx.roots) in ASKS:
                    raise _Ask
    if name in _SHELLS:
        script = _shell_string(args)
        if script is not None:
            _line(script, ctx.child(), state)
    if recursive:
        if where[1]:
            raise _Ask
        if any(absolute_relation(b, ctx.roots) is not None for b in where[0]):
            raise _Ask  # it reads the folder it runs in, and that one holds a guarded path
    _redirect(redirects, name, texts, k, ctx, bodies, state, where, code=code or names_in)
    return name


def _moved(alts: list[Alt] | None, where: tuple[list[str], bool]) -> tuple[list[str], bool]:
    if alts is None or any(g for _, _, g in alts):
        return where[0], True
    folders = [tidy(a if a.startswith("/") else f"{b}/{a}") for a, _, _ in alts for b in where[0]]
    return folders, where[1]


def _find_clauses(
    words: list[list[Any]],
    texts: list[str],
    k: int,
    ctx: _Ctx,
    bodies: list[str],
    state: _State,
) -> None:
    """A ``find -exec`` clause is a command of its own."""
    i = k + 1
    while i < len(words):
        if texts[i] in _FIND_RUNS:
            j = i + 1
            while j < len(words) and texts[j] not in (";", "+"):
                j += 1
            clause = [
                w for w, t in zip(words[i + 1 : j], texts[i + 1 : j], strict=True) if t != "{}"
            ]
            if clause:
                _simple(clause, [], ctx, bodies, state)
            i = j
        i += 1


def _redirect(
    redirects: list[tuple[str, list[Any]]],
    name: str,
    texts: list[str],
    k: int,
    ctx: _Ctx,
    bodies: list[str],
    state: _State,
    where: tuple[list[str], bool],
    code: bool = False,
) -> None:
    """``code``: the command takes code or names on its input (an interpreter, ``xargs``)."""
    shell = name in _SHELLS and _shell_string(texts[k + 1 :]) is None
    takes = code or not name or name in _NAMES_IN or name in ("xargs", "parallel")
    for op, target in redirects:
        text = _text(target)
        heredoc = _HEREDOC.fullmatch(text) if op == "<<" else None
        if heredoc is not None:
            index = int(heredoc.group(1))
            fed = bodies[index] if index < len(bodies) else ""
        elif op == "<<<":
            alts = _check(target, ctx, state, where)
            fed = " ".join(a for a, _, _ in alts or [])
        elif op in ("<&", ">&") and (text.isdigit() or text == "-"):
            continue
        else:
            _check(target, ctx, state, where, unknown=True, disk=True)
            continue
        if shell:
            _line(fed, ctx.child(), state)  # a here-document a shell runs
        elif takes and text_relation(fed, where[0], ctx.roots) in ASKS:
            raise _Ask  # code, or names, from a here-document
