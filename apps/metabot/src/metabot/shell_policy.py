"""Classes for shell commands and for an engine's own tools.

Connection capabilities are classed by ``scope_classes`` (verbs in the
capability id; unrecognised is destructive). A shell command is classed
here instead, because a command line is not a capability id.

``read`` runs with nobody asked, so it is narrow: a small allow-list of
read-only commands, called by name (never by a path, which could be a
program the workspace ships), with nothing on the line that changes what
runs (no variable assignment, no ``env``, no option that writes a file or
runs a program), and every part of the line ``read``.

``destructive`` is for known destructive or outward-facing patterns:
deleting, force or any push, publishing, piping a download into an
interpreter, privilege escalation, ``eval``, recursive permission changes,
data-dropping SQL, cluster and infrastructure teardown. They are found
wherever the shell would run them: in subshells and groups, after keywords,
in command and process substitutions, behind wrappers, in ``sh -c`` and
``env -S`` strings, and in values that hold a command (a variable
assignment, a ``git -c`` setting). A command whose name the shell works out
at run time (a variable, a glob, a substitution) is destructive, because
nobody can tell what it is. Anything that cannot be parsed is destructive.

Every other command is ``write``: it runs only if the job allows writes or a
human approves it. That includes running a program or a script, which can do
anything the person can: allowing writes trusts the engine with arbitrary
code in the workspace, and only the destructive patterns still stop for a
human.

A compound command takes the strictest class of its parts.
"""

from __future__ import annotations

import os
import re
import shlex
from typing import Any

from metabot.scope_classes import ScopeClass, resolve_scope_class
from metabot.text import ascii_digits

READ, WRITE, DESTRUCTIVE = ScopeClass.READ, ScopeClass.WRITE, ScopeClass.DESTRUCTIVE
MAX_DEPTH = 12  # nested substitutions and command strings
MAX_SCANS = 64  # look-ups inside one unknown command's arguments

_READ_COMMANDS = frozenset(
    {
        "ls",
        "pwd",
        "cat",
        "head",
        "tail",
        "wc",
        "grep",
        "rg",
        "find",
        "echo",
        "which",
        "file",
        "stat",
        "tree",
        "du",
        "df",
        "printenv",
        "sort",
        "uniq",
        "basename",
        "dirname",
        "realpath",
        "true",
        "date",
    }
)
# Options that make a read-only command write a file or run a program.
_READ_BLOCKERS: dict[str, tuple[str, ...]] = {
    "sort": ("-o", "--output", "--compress-program"),
    "tree": ("-o", "-R"),
    "rg": ("--pre", "--hostname-bin"),
    "file": ("-C", "--compile"),
    "date": ("-s", "--set"),
}
_FIND_WRITES = frozenset({"-fprint", "-fprint0", "-fprintf", "-fls"})
_FIND_EXEC = frozenset({"-exec", "-execdir", "-ok", "-okdir"})
_GIT_READ = frozenset({"status", "log", "diff", "show", "rev-parse", "branch", "ls-files", "blame"})
_GIT_DESTRUCTIVE = frozenset({"push", "filter-branch", "filter-repo", "update-ref"})
_GIT_SAFE_GLOBALS = frozenset(
    {
        "--no-pager",
        "-P",
        "--paginate",
        "-p",
        "--no-optional-locks",
        "--literal-pathspecs",
        "--glob-pathspecs",
        "--noglob-pathspecs",
        "--icase-pathspecs",
        "--no-replace-objects",
        "--no-advice",
    }
)
# Global options that point git at another repository, or another program.
_GIT_VALUE_GLOBALS = frozenset(
    {"-C", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--config-env"}
)
_GIT_BRANCH_WRITES = frozenset(
    {
        "-m",
        "-M",
        "-c",
        "-C",
        "--move",
        "--copy",
        "-u",
        "--set-upstream-to",
        "--unset-upstream",
        "--edit-description",
        "-f",
        "--force",
        "-t",
        "--track",
        "--no-track",
        "--create-reflog",
    }
)
_GIT_BRANCH_LISTS = frozenset(
    {"--list", "-l", "--contains", "--no-contains", "--merged", "--no-merged", "--points-at"}
)
_GH_READ = frozenset({"view", "list", "status", "diff", "checks"})
_DESTRUCTIVE_COMMANDS = frozenset(
    {
        "rm",
        "rmdir",
        "shred",
        "unlink",
        "dd",
        "truncate",
        "sudo",
        "doas",
        "su",
        "eval",
        "reboot",
        "shutdown",
        "halt",
        "poweroff",
        "kill",
        "killall",
        "pkill",
        "crontab",
    }
)
_PUBLISH = {
    "npm": {"publish", "unpublish", "deprecate"},
    "pnpm": {"publish"},
    "yarn": {"publish"},
    "twine": {"upload"},
    "uv": {"publish"},
    "poetry": {"publish"},
    "cargo": {"publish", "yank"},
    "gem": {"push", "yank"},
    "docker": {"push", "rm", "rmi", "prune", "system"},
    "podman": {"push", "rm", "rmi", "prune", "system"},
    "kubectl": {"delete", "apply", "replace", "patch", "scale", "drain", "cordon", "rollout"},
    "helm": {"install", "upgrade", "uninstall", "delete", "rollback"},
    "terraform": {"apply", "destroy", "import", "taint"},
    "tofu": {"apply", "destroy", "import", "taint"},
    "pulumi": {"up", "destroy", "import"},
    "aws": {"rm", "delete", "terminate-instances", "delete-bucket"},
    "vercel": {"deploy", "--prod", "remove", "rm", "promote"},
}
_SQL_CLIENTS = frozenset({"psql", "mysql", "sqlite3", "mariadb", "duckdb", "clickhouse-client"})
_SQL_DESTRUCTIVE = ("drop ", "truncate ", "delete ", "alter ", "grant ", "revoke ")
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "mksh", "ash", "fish"})
_INTERPRETER = re.compile(
    r"^(?:sh|bash|zsh|dash|ksh|mksh|ash|fish|python[0-9.]*|pypy[0-9.]*|node|nodejs|deno|bun"
    r"|perl[0-9.]*|ruby[0-9.]*|php[0-9.]*|lua[0-9.]*|tclsh[0-9.]*|Rscript|osascript)$"
)
_SOURCE = frozenset({"source", "."})
_RESERVED = frozenset(
    {"!", "{", "}", "if", "then", "else", "elif", "fi", "do", "done", "while", "until", "esac"}
)
_HEADERS = frozenset({"for", "select", "case"})  # the rest of the line is words, not a command
# Transparent wrappers, with the options that take a separate value.
_WRAPPERS: dict[str, frozenset[str]] = {
    "time": frozenset({"-o", "--output", "-f", "--format"}),
    "nice": frozenset({"-n", "--adjustment"}),
    "ionice": frozenset(
        {"-c", "-n", "-p", "-P", "-u", "--class", "--classdata", "--pid", "--pgid", "--uid"}
    ),
    "stdbuf": frozenset({"-i", "-o", "-e", "--input", "--output", "--error"}),
    "timeout": frozenset({"-s", "--signal", "-k", "--kill-after"}),
    "exec": frozenset({"-a"}),
    "command": frozenset(),
    "builtin": frozenset(),
    "nohup": frozenset(),
    "coproc": frozenset(),
    "busybox": frozenset(),
    "toybox": frozenset(),
}
_WRAPPER_WRITES = {"time": frozenset({"-o", "--output"})}  # the wrapper writes a file itself
_XARGS_VALUES = frozenset(
    {
        "-n",
        "-P",
        "-L",
        "-s",
        "-d",
        "-E",
        "-a",
        "-I",
        "--max-args",
        "--max-procs",
        "--max-lines",
        "--max-chars",
        "--delimiter",
        "--eof",
        "--arg-file",
        "--replace",
    }
)
_SCAN_TRIGGERS = (
    _DESTRUCTIVE_COMMANDS
    | _SHELLS
    | _SQL_CLIENTS
    | frozenset(_PUBLISH)
    | {"git", "gh", "curl", "wget", "find", "xargs", "chmod", "chown", "chgrp", "env"}
)
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\[[^\]]*\])?\+?=")
_MARK = "\x01"  # stands in for a substitution once its command is pulled out
_BODY = "\x02"  # stands in for a here-document's delimiter: the index of its body
_HEREDOC = re.compile("\x02(\\d+)\x02")
_MARKER = re.compile("\x01[^\x01]*\x01")
_GLOB = re.compile(r"[*?\[]")
_BRACE = re.compile(r"\{[^{}]*(?:,|\.\.)[^{}]*\}")
_WORD_BREAK = " \t\n;&|()<>"
_REDIRECT_OPS = ("<<<", "<<-", "&>>", "<<", "<>", "<&", ">>", ">&", ">|", "&>", "<", ">")


# ── lexing ───────────────────────────────────────────────────────────────────


def _backtick_end(text: str, i: int) -> int | None:
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == "`":
            return i
        i += 1
    return None


def _heredoc_op(text: str, i: int) -> tuple[int, str, bool, bool] | None:
    """Read ``<<WORD`` (or ``<<-``) at ``i``: (end, delimiter, quoted, strip_tabs)."""
    n = len(text)
    j = i + 2
    strip = text.startswith("-", j)
    j += strip
    while j < n and text[j] in " \t":
        j += 1
    k, parts, quoted = j, [], False
    while k < n and text[k] not in " \t\n;&|()<>":
        ch = text[k]
        if ch in "'\"":
            end = text.find(ch, k + 1)
            if end < 0:
                return None
            parts.append(text[k + 1 : end])
            quoted, k = True, end + 1
            continue
        if ch == "\\":
            parts.append(text[k + 1 : k + 2])
            quoted, k = True, k + 2
            continue
        parts.append(ch)
        k += 1
    delimiter = "".join(parts)
    if not delimiter:
        return None
    return k, delimiter, quoted, strip


def _bodies(text: str, i: int, pending: list[tuple[str, bool]]) -> tuple[list[str], int] | None:
    """The here-document bodies that start at ``i``, and where the line after them starts."""
    bodies: list[str] = []
    for delimiter, strip in pending:
        lines: list[str] = []
        while True:
            nl = text.find("\n", i)
            line = text[i:] if nl < 0 else text[i:nl]
            if strip:
                line = line.lstrip("\t")
            i = len(text) if nl < 0 else nl + 1
            if line == delimiter:
                break
            if nl < 0:
                return None  # no closing line: the shell would read to the end
            lines.append(line)
        bodies.append("\n".join(lines))
    return bodies, i


def _paren_end(text: str, i: int) -> int | None:
    """The index of the ``)`` that closes a ``(`` opened just before ``i``."""
    depth, n, start = 1, len(text), i
    in_double = False
    pending: list[tuple[str, bool]] = []
    while i < n:
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if in_double:
            if c == '"':
                in_double = False
            elif text.startswith("$(", i):
                end = _paren_end(text, i + 2)
                if end is None:
                    return None
                i = end
            elif c == "`":
                end = _backtick_end(text, i + 1)
                if end is None:
                    return None
                i = end
            i += 1
            continue
        if c == "'":
            end = text.find("'", i + 1)
            if end < 0:
                return None
            i = end + 1
            continue
        if c == "\n" and pending:
            found = _bodies(text, i + 1, pending)
            if found is None:
                return None
            i, pending = found[1], []
            continue
        if text.startswith("<<", i) and not text.startswith("<<<", i):
            op = _heredoc_op(text, i)
            if op is None:
                return None
            pending.append((op[1], op[3]))
            i = op[0]
            continue
        if c == '"':
            in_double = True
        elif c == "`":
            end = _backtick_end(text, i + 1)
            if end is None:
                return None
            i = end
        elif c == "#" and (i == start or text[i - 1] in _WORD_BREAK):
            end = text.find("\n", i)
            if end < 0:
                return None
            i = end
            continue
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


Lexed = tuple[str, list[str], list[str], list[str]]


def _lex(command: str, mode: str = "shell") -> Lexed | None:
    """Drop comments and pull out substitutions, as the shell would see them.

    Returns the text with every command substitution (``$(...)``, backticks)
    and process substitution (``<(...)``, ``>(...)``) replaced by a marker,
    and each here-document's delimiter replaced by its body's index; the
    commands inside substitutions; the bodies of arithmetic ``$((...))`` (to
    be looked through, in case the shell reads one as a subshell); and the
    here-document bodies. None when a quote, a substitution or a
    here-document is not closed.

    ``mode`` is ``shell``, ``body`` (an unquoted here-document: quotes are
    plain text, substitutions still run) or ``arith`` (no here-documents).
    """
    out: list[str] = []
    inner: list[str] = []
    scans: list[str] = []
    bodies: list[str] = []
    pending: list[tuple[int, str, bool, bool]] = []
    shell = mode == "shell"
    i, n = 0, len(command)
    end: int | None
    in_double = False
    while i < n:
        c = command[i]
        if c == "\\":
            out.append(command[i : i + 2])
            i += 2
            continue
        if mode != "body" and not in_double and c == "'":
            end = command.find("'", i + 1)
            if end < 0:
                return None
            out.append(command[i : end + 1])
            i = end + 1
            continue
        if shell and not in_double and c == "#" and (not out or out[-1][-1] in _WORD_BREAK):
            end = command.find("\n", i)  # a comment runs to the end of the line
            i = n if end < 0 else end
            continue
        if shell and not in_double and c == "\n" and pending:
            found = _bodies(command, i + 1, [(d, s) for _, d, _, s in pending])
            if found is None:
                return None
            for (index, _, quoted, _), body in zip(pending, found[0], strict=True):
                if quoted:  # `<<'EOF'`: the body is text, exactly as written
                    bodies[index] = body
                    continue
                lexed = _lex(body, mode="body")
                if lexed is None:
                    return None
                bodies[index] = lexed[0]
                inner.extend(lexed[1])
                scans.extend(lexed[2])
            out.append("\n")
            i, pending = found[1], []
            continue
        if (
            shell
            and not in_double
            and command.startswith("<<", i)
            and not command.startswith("<<<", i)
        ):
            op = _heredoc_op(command, i)
            if op is None:
                return None
            end, delimiter, quoted, strip = op
            pending.append((len(bodies), delimiter, quoted, strip))
            out.append(f"<< {_BODY}{len(bodies)}{_BODY}")
            bodies.append("")
            i = end
            continue
        if mode != "body" and c == '"':
            in_double = not in_double
            out.append(c)
            i += 1
            continue
        if c == "`":
            end = _backtick_end(command, i + 1)
            if end is None:
                return None
            # Inside backticks the shell removes the backslash before ` \ $.
            inner.append(re.sub(r"\\([`\\$])", r"\1", command[i + 1 : end]))
            out.append(f"{_MARK}{len(inner) - 1}{_MARK}")
            i = end + 1
            continue
        if command.startswith("$((", i):
            end = _paren_end(command, i + 2)
            if end is None:
                return None
            closes = _paren_end(command, i + 3)
            if closes == end - 1:  # $(( ... )): arithmetic
                lexed = _lex(command[i + 3 : end - 1], mode="arith")
                if lexed is None:
                    return None
                body, nested, more, _ = lexed
                inner.extend(nested)
                scans.extend([*more, body])
                out.append(f"{_MARK}a{_MARK}" if nested else "0")
                i = end + 1
                continue
        if command.startswith("$(", i) or (
            shell and not in_double and command.startswith(("<(", ">("), i)
        ):
            end = _paren_end(command, i + 2)
            if end is None:
                return None
            inner.append(command[i + 2 : end])
            out.append(f"{_MARK}{len(inner) - 1}{_MARK}")
            i = end + 1
            continue
        out.append(c)
        i += 1
    if in_double or pending:
        return None
    return "".join(out), inner, scans, bodies


def _split(text: str) -> list[tuple[str, bool]] | None:
    """Split on ``; & && || | |& ( )`` and newlines outside quotes.

    Returns ``(segment, piped_into)`` pairs, where ``piped_into`` is True when
    the segment reads a pipe (also through a group opened right after it).
    """
    segments: list[tuple[str, bool]] = []
    buf: list[str] = []
    piped = False
    in_double = False
    i, n = 0, len(text)

    def flush(next_piped: bool) -> None:
        nonlocal buf, piped
        segments.append(("".join(buf), piped))
        buf, piped = [], next_piped

    while i < n:
        c = text[i]
        if c == "\\":
            buf.append(text[i : i + 2])
            i += 2
            continue
        if in_double:
            buf.append(c)
            in_double = c != '"'
            i += 1
            continue
        if c == "'":
            end = text.find("'", i + 1)
            if end < 0:
                return None
            buf.append(text[i : end + 1])
            i = end + 1
            continue
        if c == '"':
            in_double = True
            buf.append(c)
            i += 1
            continue
        two = text[i : i + 2]
        if two in ("&&", "||"):
            flush(False)
            i += 2
            continue
        if two == "|&":
            flush(True)
            i += 2
            continue
        if c == "|":
            flush(True)
            i += 1
            continue
        if c in ";\n)":
            flush(False)
            i += 1
            continue
        if c == "(":
            if "".join(buf).strip():
                flush(False)
            i += 1  # an empty buffer keeps its pipe: `curl x | (sh)` reads the pipe
            continue
        if c == "&" and not (buf and buf[-1][-1:] in ("<", ">")) and text[i + 1 : i + 2] != ">":
            flush(False)
            i += 1
            continue
        buf.append(c)
        i += 1
    if in_double:
        return None
    segments.append(("".join(buf), piped))
    return [(s, p) for s, p in segments if s.strip()]


def _redirect_at(segment: str, i: int) -> str:
    if segment[i] == "&" and segment[i + 1 : i + 2] != ">":
        return ""
    for op in _REDIRECT_OPS:
        if segment.startswith(op, i):
            return op
    return ""


def _words(segment: str) -> list[tuple[str, bool]] | None:
    """The segment's words with quotes removed; redirections come out as ``(op, True)``."""
    words: list[tuple[str, bool]] = []
    buf: list[str] = []
    started = False
    i, n = 0, len(segment)
    while i < n:
        c = segment[i]
        if c in " \t\n":
            if started:
                words.append(("".join(buf), False))
                buf, started = [], False
            i += 1
            continue
        if c == "\\":
            if i + 1 < n and segment[i + 1] != "\n":
                buf.append(segment[i + 1])
                started = True
            i += 2
            continue
        if c == "'":
            end = segment.find("'", i + 1)
            if end < 0:
                return None
            buf.append(segment[i + 1 : end])
            started = True
            i = end + 1
            continue
        if c == '"':
            j, part = i + 1, []
            while j < n and segment[j] != '"':
                if segment[j] == "\\" and j + 1 < n and segment[j + 1] in '"\\$`\n':
                    if segment[j + 1] != "\n":
                        part.append(segment[j + 1])
                    j += 2
                    continue
                part.append(segment[j])
                j += 1
            if j >= n:
                return None
            buf.append("".join(part))
            started = True
            i = j + 1
            continue
        op = _redirect_at(segment, i) if c in "<>&" else ""
        if op:
            text = "".join(buf)
            if started and not ascii_digits(text):  # digits right before it are its descriptor
                words.append((text, False))
            buf, started = [], False
            words.append((op, True))
            i += len(op)
            continue
        buf.append(c)
        started = True
        i += 1
    if started:
        words.append(("".join(buf), False))
    return words


def _redirects(words: list[tuple[str, bool]]) -> tuple[list[str], list[tuple[str, str]]] | None:
    argv: list[str] = []
    redirects: list[tuple[str, str]] = []
    i = 0
    while i < len(words):
        text, is_op = words[i]
        if is_op:
            if i + 1 >= len(words) or words[i + 1][1]:
                return None  # an operator with nothing to redirect to
            redirects.append((text, words[i + 1][0]))
            i += 2
            continue
        argv.append(text)
        i += 1
    return argv, redirects


def _writes(op: str, target: str) -> bool:
    if op in (">", ">>", ">|", "&>", "&>>", "<>"):
        return target != "/dev/null"
    if op == ">&":  # `>&2` duplicates a descriptor; `>&name` writes a file
        return not (ascii_digits(target) or target == "-")
    return False


# ── classing ─────────────────────────────────────────────────────────────────


def _unknowable(word: str) -> bool:
    """A command name the shell works out at run time."""
    return bool(
        _MARK in word
        or "$" in word
        or _GLOB.search(word)
        or _BRACE.search(word)
        or word.endswith("()")
    )


def _hidden_destructive(text: str, depth: int) -> bool:
    """True when ``text``, read as a command line, is destructive."""
    text = _MARKER.sub(" ", text)  # substitutions were classed where they were found
    if not text.strip():
        return False
    return _classify(text, depth + 1, strict=False) is DESTRUCTIVE


def _skip_options(
    args: list[str], takes_value: frozenset[str], writes: frozenset[str] = frozenset()
) -> tuple[list[str], bool]:
    """Drop leading options (and the values of options that take one)."""
    wrote = False
    while args and args[0].startswith("-") and args[0] != "-":
        flag = args[0]
        if flag == "--":
            return args[1:], wrote
        wrote |= flag.split("=", 1)[0] in writes
        args = args[1:]
        if flag in takes_value and args:
            args = args[1:]
    return args, wrote


def _env_options(args: list[str]) -> tuple[list[str], str | None]:
    """``env``'s options; returns the rest and a ``-S`` string, if any."""
    while args:
        a = args[0]
        if a == "--":
            return args[1:], None
        if a in ("--split-string", "-S"):
            return args[2:], (args[1] if len(args) > 1 else "")
        if a.startswith("--split-string="):
            return args[1:], a.split("=", 1)[1]
        if a in ("--unset", "--chdir"):
            args = args[2:]
            continue
        if a.startswith("--"):
            args = args[1:]
            continue
        if a.startswith("-") and len(a) > 1:
            for k, letter in enumerate(a[1:], start=1):
                if letter == "S":
                    rest = a[k + 1 :]
                    return (
                        (args[1:], rest) if rest else (args[2:], args[1] if len(args) > 1 else "")
                    )
                if letter in "uCP":  # takes a value: the rest of the word, or the next word
                    if not a[k + 1 :]:
                        args = args[1:]
                    break
            args = args[1:]
            continue
        if a == "-":
            args = args[1:]
            continue
        break
    return args, None


def _program_given(args: list[str]) -> bool:
    return any(
        a in ("-c", "-e", "-m") or (a.startswith("-") and not a.startswith("--") and a[-1:] in "ce")
        for a in args
    ) or any(not a.startswith("-") for a in args)


def _shell_string(args: list[str]) -> str | None:
    """The command string of ``sh -c STRING`` (also ``-lc``, ``-ec``), or None."""
    has_c = False
    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith("--command="):
            return a.split("=", 1)[1]
        if a in ("-o", "-O", "+o", "+O", "--rcfile", "--init-file"):
            i += 2
            continue
        if a == "--command":
            has_c = True
        elif a == "--":
            i += 1
            break
        elif a.startswith("-") and not a.startswith("--") and "c" in a[1:]:
            has_c = True
        elif not a.startswith(("-", "+")):
            break
        i += 1
    if not has_c:
        return None
    return args[i] if i < len(args) else ""


def _interpreter(
    cmd: str,
    args: list[str],
    redirects: list[tuple[str, str]],
    piped_into: bool,
    depth: int,
    strict: bool,
) -> ScopeClass:
    """Running a program: ``write``, or ``destructive`` when nobody can see the program."""
    if any(_MARK in a for a in args):  # `bash <(curl ...)`, `sh -c "$(...)"`
        return DESTRUCTIVE
    if cmd in _SOURCE:
        return WRITE
    shell = cmd in _SHELLS
    if shell:
        script = _shell_string(args)
        if script is not None:
            return max(WRITE, _classify(script, depth + 1, strict))
    for op, target in redirects:
        if op in ("<<<", "<<"):  # a here-string or here-document is the program
            if _MARK in target:
                return DESTRUCTIVE  # code built from another command's output
            if shell and target.strip():
                return max(WRITE, _classify(target, depth + 1, strict))
        if op == "<" and _MARK in target and not _program_given(args):
            return DESTRUCTIVE  # `sh < <(curl ...)`
    if piped_into and (not _program_given(args) or "-s" in args or "-" in args):
        # Reading a program from the pipe runs whatever arrived: download-and-execute.
        return DESTRUCTIVE
    return WRITE


_ALIAS_HOPS = 16  # git refuses a loop; a longer chain than this is treated as one


def _git_aliases_from_env(assigns: dict[str, str]) -> list[tuple[str, str | None]]:
    """The config settings a line gives git in its environment, as (key, value)."""
    found: list[tuple[str, str | None]] = []
    try:
        count = int(assigns.get("GIT_CONFIG_COUNT", "0"))
    except ValueError:
        count = 0
    for k in range(min(max(count, 0), 64)):
        key = assigns.get(f"GIT_CONFIG_KEY_{k}")
        if key is not None:
            found.append((key, assigns.get(f"GIT_CONFIG_VALUE_{k}")))
    params = assigns.get("GIT_CONFIG_PARAMETERS")
    if params:
        try:
            words = shlex.split(params)
        except ValueError:
            words = []
        for word in words:
            key, sep, value = word.partition("=")
            if sep:
                found.append((key, value))
    return found


def _git(
    args: list[str], depth: int, tainted: bool, assigns: dict[str, str] | None = None
) -> ScopeClass:
    assigns = assigns or {}
    # name -> body; None when the body sits in a variable the line does not show
    aliases: dict[str, str | None] = {}

    def setting(key: str, value: str | None) -> bool:
        """Record one config setting; True when its value runs something destructive."""
        if value is not None and _hidden_destructive(
            value[1:] if value.startswith("!") else value, depth
        ):
            return True  # `-c core.fsmonitor=...`, `-c alias.x='!...'` run commands
        section, dot, name = key.partition(".")
        if dot and name and section.lower() == "alias" and not (value or "").startswith("!"):
            aliases[name.lower()] = value  # git reads config names in any case
        return False

    for key, value in _git_aliases_from_env(assigns):
        if setting(key, value):
            return DESTRUCTIVE
    i = 0
    while i < len(args) and args[i].startswith("-") and args[i] != "-":
        flag = args[i]
        if flag == "-c" or flag == "--config-env" or flag.startswith("--config-env="):
            joined = flag.startswith("--config-env=")
            spec = flag.split("=", 1)[1] if joined else args[i + 1] if i + 1 < len(args) else ""
            key, sep, value = spec.partition("=")
            if sep and flag != "-c":
                value = assigns.get(value)  # `--config-env=alias.x=VAR` reads $VAR
            if sep and setting(key, value):
                return DESTRUCTIVE
            tainted = True
            i += 1 if joined else 2
            continue
        if flag in _GIT_VALUE_GLOBALS:
            tainted = True
            i += 2
            continue
        if flag not in _GIT_SAFE_GLOBALS:
            tainted = True  # --git-dir=, --exec-path=, --bare, anything unknown
        i += 1
    args = args[i:]
    sub = args[0] if args else ""
    rest = args[1:]
    name, hops = sub.lower(), 0
    while name in aliases and hops < _ALIAS_HOPS:  # `alias.y=x` with `alias.x=push` runs push
        body = aliases[name]
        if body is None:
            return DESTRUCTIVE  # the alias is a variable's value: nobody can tell what runs
        if _classify("git " + body, depth + 1, strict=True) is DESTRUCTIVE:
            return DESTRUCTIVE  # `git -c alias.x=push x` runs git push
        try:
            words = shlex.split(body)
        except ValueError:
            return DESTRUCTIVE
        name, hops = (words[0].lower() if words else ""), hops + 1
    if sub in _GIT_DESTRUCTIVE:
        return DESTRUCTIVE
    if sub == "reset" and "--hard" in rest:
        return DESTRUCTIVE
    if sub == "clean" and any(a.startswith("-") and "f" in a for a in rest):
        return DESTRUCTIVE
    if sub in ("branch", "tag") and any(a in ("-d", "-D", "--delete") for a in rest):
        return DESTRUCTIVE
    if sub == "stash" and any(a in ("drop", "clear") for a in rest):
        return DESTRUCTIVE
    if sub == "checkout" and ("--" in rest or "." in rest):
        return DESTRUCTIVE
    if sub == "restore":
        return DESTRUCTIVE
    if sub == "branch":
        if any(a.split("=", 1)[0] in _GIT_BRANCH_WRITES for a in rest):
            return WRITE
        listing = any(a.split("=", 1)[0] in _GIT_BRANCH_LISTS for a in rest)
        if not listing and any(not a.startswith("-") for a in rest):
            return WRITE  # `git branch NAME` creates a branch
    if any(a == "--output" or a.startswith("--output=") for a in rest):
        return WRITE
    return READ if sub in _GIT_READ and not tainted else WRITE


def _find(args: list[str], depth: int, strict: bool) -> ScopeClass:
    if "-delete" in args:
        return DESTRUCTIVE
    worst = READ
    i = 0
    while i < len(args):
        if args[i] in _FIND_EXEC:
            j = i + 1
            while j < len(args) and args[j] not in (";", "+"):
                j += 1
            clause = [a for a in args[i + 1 : j] if a != "{}"]
            worst = max(worst, WRITE, _command(clause, [], False, depth + 1, strict))
            i = j + 1
            continue
        if args[i] in _FIND_WRITES:
            worst = max(worst, WRITE)
        i += 1
    return worst


def _read_blocked(cmd: str, args: list[str]) -> bool:
    if cmd == "uniq":  # `uniq IN OUT` writes OUT
        return len([a for a in args if not a.startswith("-") or a == "-"]) >= 2
    for a in args:
        for opt in _READ_BLOCKERS.get(cmd, ()):
            if opt.startswith("--"):
                if a == opt or a.startswith(opt + "="):
                    return True
            elif a == opt or (a.startswith("-") and not a.startswith("--") and opt[1] in a[1:]):
                return True
    return False


def _hidden_in_args(args: list[str], depth: int) -> bool:
    """A recognisable destructive command among an unknown command's arguments.

    ``setsid rm -rf x``, ``busybox``-style wrappers, ``ssh host 'rm -rf x'``,
    ``[[ 'a[$(rm -rf x)]' -eq 0 ]]``: whatever the command is, an argument
    that is itself a destructive command line makes the line destructive.
    """
    scans = 0
    for k, arg in enumerate(args):
        if _MARK in arg:
            continue
        if any(ch in arg for ch in " \t\n") or "$(" in arg or "`" in arg:
            scans += 1
            if scans > MAX_SCANS or _hidden_destructive(arg, depth):
                return True
            continue
        base = os.path.basename(arg)
        if base in _SCAN_TRIGGERS or base.startswith("mkfs"):
            scans += 1
            if scans > MAX_SCANS:
                return True
            if _command(args[k:], [], False, depth + 1, strict=False, scan=False) is DESTRUCTIVE:
                return True
    return False


def _command(
    argv: list[str],
    redirects: list[tuple[str, str]],
    piped_into: bool,
    depth: int,
    strict: bool,
    scan: bool = True,
) -> ScopeClass:
    """The class of one simple command, from its words."""
    if depth > MAX_DEPTH:
        return DESTRUCTIVE
    tainted = False  # something on the line changes which program runs: never read
    while argv and argv[0] in _RESERVED:
        argv = argv[1:]
    if argv and argv[0] in _HEADERS:
        return READ
    if argv and argv[0] == "function":
        argv = [a for a in argv[2:] if a not in _RESERVED]
    assigns: dict[str, str] = {}  # the line's own variables, which git may read
    while argv and _ASSIGNMENT.match(argv[0]):
        tainted = True  # PATH=, LD_PRELOAD=, GIT_*=: the program or its behaviour changes
        var, _, value = argv[0].partition("=")
        if _hidden_destructive(value, depth):
            return DESTRUCTIVE
        assigns[var] = value
        argv = argv[1:]
    while argv:
        base = os.path.basename(argv[0])
        if _unknowable(argv[0]):
            break
        if base in _WRAPPERS:
            tainted |= "/" in argv[0] or base == "nohup"  # nohup may write nohup.out
            if base == "command" and len(argv) > 1 and argv[1] in ("-v", "-V"):
                return WRITE if tainted else READ
            argv, wrote = _skip_options(
                argv[1:], _WRAPPERS[base], _WRAPPER_WRITES.get(base, frozenset())
            )
            tainted |= wrote
            if base == "timeout" and argv:
                argv = argv[1:]  # the duration
            continue
        if base == "env":
            if len(argv) == 1:
                return WRITE if tainted or "/" in argv[0] else READ  # prints the environment
            tainted = True
            argv, split = _env_options(argv[1:])
            if split is not None:  # `env -S STRING`: the string is a command line
                line = " ".join([split, *(shlex.quote(a) for a in argv)])
                return max(WRITE, _classify(line, depth + 1, strict))
            while argv and _ASSIGNMENT.match(argv[0]):
                var, _, value = argv[0].partition("=")
                if _hidden_destructive(value, depth):
                    return DESTRUCTIVE
                assigns[var] = value
                argv = argv[1:]
            continue
        break
    if not argv:
        return WRITE if tainted else READ
    word = argv[0]
    args = argv[1:]
    if word not in ("[", "[[") and _unknowable(word):
        return DESTRUCTIVE if strict else WRITE
    if any(ch in word for ch in " \t\n"):
        # No program has this name; a string like this is a command line in a program.
        return DESTRUCTIVE if _hidden_destructive(word, depth) else WRITE
    tainted |= "/" in word  # a path: possibly a program the workspace ships
    cmd = os.path.basename(word)
    if cmd in _DESTRUCTIVE_COMMANDS or cmd.startswith("mkfs"):
        return DESTRUCTIVE
    if _INTERPRETER.match(cmd) or cmd in _SOURCE:
        return _interpreter(cmd, args, redirects, piped_into, depth, strict)
    if cmd == "xargs":
        rest, _ = _skip_options(args, _XARGS_VALUES)
        return max(WRITE, _command(rest, [], False, depth + 1, strict)) if rest else WRITE
    if cmd == "find":
        return max(_find(args, depth, strict), WRITE if tainted else READ)
    if cmd == "git":
        return _git(args, depth, tainted, assigns)
    if cmd == "gh":
        verbs = [a for a in args if not a.startswith("-")]
        if len(verbs) >= 2 and verbs[1] in _GH_READ:
            return WRITE if tainted else READ
        return DESTRUCTIVE
    if cmd in _PUBLISH and any(a in _PUBLISH[cmd] for a in args):
        return DESTRUCTIVE
    if cmd in ("chmod", "chown", "chgrp") and any(
        a == "--recursive" or (a.startswith("-") and not a.startswith("--") and "R" in a)
        for a in args
    ):
        return DESTRUCTIVE
    fed = [t for op, t in redirects if op in ("<<<", "<<")]  # text on its stdin
    if cmd in _SQL_CLIENTS:
        text = " ".join(a.lower() for a in [*args, *fed]) + " "
        return DESTRUCTIVE if any(k in text for k in _SQL_DESTRUCTIVE) else WRITE
    if cmd in ("curl", "wget"):
        return DESTRUCTIVE if _sends_data(args) else WRITE
    if cmd in _READ_COMMANDS:
        return WRITE if tainted or _read_blocked(cmd, args) else READ
    if scan and _hidden_in_args([*args, *fed], depth):
        return DESTRUCTIVE  # `ssh host <<EOF rm -rf x EOF` runs its stdin
    return WRITE


_SEND_FLAGS = frozenset(
    {
        "-d",
        "-F",
        "-T",
        "--data",
        "--data-binary",
        "--data-raw",
        "--data-urlencode",
        "--form",
        "--upload-file",
        "--post-data",
        "--post-file",
        "--body-data",
        "--body-file",
    }
)
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _sends_data(args: list[str]) -> bool:
    """True when a curl/wget call sends a body or uses a non-read method.

    Flags are case-sensitive (curl ``-f`` is ``--fail``, ``-x`` is ``--proxy``).
    """
    for i, arg in enumerate(args):
        if arg in _SEND_FLAGS or arg.startswith(("--data", "--form", "--post-")):
            return True
        method: str | None = None
        if arg in ("-X", "--request", "--method"):
            method = args[i + 1] if i + 1 < len(args) else ""
        elif arg.startswith("-X") and len(arg) > 2:
            method = arg[2:]
        elif arg.startswith(("--request=", "--method=")):
            method = arg.split("=", 1)[1]
        if method is not None and method.upper() not in _SAFE_METHODS:
            return True
    return False


def _segment(
    segment: str, piped_into: bool, depth: int, strict: bool, bodies: list[str]
) -> ScopeClass:
    fail = DESTRUCTIVE if strict else READ
    words = _words(segment)
    parsed = _redirects(words) if words is not None else None
    if parsed is None:
        return fail
    argv, raw = parsed
    redirects: list[tuple[str, str]] = []
    for op, target in raw:
        match = _HEREDOC.fullmatch(target) if op == "<<" else None
        if match and int(match.group(1)) < len(bodies):
            target = bodies[int(match.group(1))]
        redirects.append((op, target))
    cls = _command(argv, redirects, piped_into, depth, strict)
    if any(_writes(op, target) for op, target in redirects):
        cls = max(cls, WRITE)
    if any(_MARK in w for w in argv) or any(_MARK in t for _, t in redirects):
        cls = max(cls, WRITE)  # words made by another command
    return cls


def _classify(command: str, depth: int, strict: bool) -> ScopeClass:
    """``strict``: a line that will run. Otherwise a string that might, where
    only a destructive answer counts and what cannot be parsed is ignored."""
    fail = DESTRUCTIVE if strict else READ
    if depth > MAX_DEPTH:
        return DESTRUCTIVE
    if not isinstance(command, str) or not command.strip():
        return fail
    lexed = _lex(command)
    if lexed is None:
        return fail
    text, inner, scans, bodies = lexed
    worst = READ
    for sub in inner:
        worst = max(worst, _classify(sub, depth + 1, strict))
        if worst is DESTRUCTIVE:
            return worst
    for body in scans:
        if _classify(body, depth + 1, strict=False) is DESTRUCTIVE:
            return DESTRUCTIVE
    segments = _split(text)
    if segments is None:
        return fail
    for segment, piped_into in segments:
        worst = max(worst, _segment(segment, piped_into, depth, strict, bodies))
        if worst is DESTRUCTIVE:
            break
    return worst


def classify_shell(command: str) -> ScopeClass:
    """The class of a shell command line; never raises."""
    try:
        return _classify(command, 0, strict=True)
    except Exception:  # a classifier bug must fail closed
        return DESTRUCTIVE


_ENGINE_READ_TOOLS = frozenset(
    {"Read", "Glob", "Grep", "LS", "NotebookRead", "WebSearch", "TodoWrite", "Task", "Agent"}
)
_ENGINE_WRITE_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit", "WebFetch"})


def classify_engine_tool(tool_name: str, tool_input: dict[str, Any]) -> ScopeClass:
    """The class of one of an engine's own tools, for its permission prompts."""
    if tool_name == "Bash":
        command = tool_input.get("command") if isinstance(tool_input, dict) else None
        return classify_shell(command if isinstance(command, str) else "")
    if tool_name in _ENGINE_READ_TOOLS:
        return ScopeClass.READ
    if tool_name in _ENGINE_WRITE_TOOLS:
        return ScopeClass.WRITE
    return resolve_scope_class(tool_name)
