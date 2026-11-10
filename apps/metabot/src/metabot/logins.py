"""``metabot login``: the person's own claude and codex sign-in folders, by name.

A login names a folder the vendor's CLI keeps its sign-in in (``CLAUDE_CONFIG_DIR``,
``CODEX_HOME``). MetaBot points an engine at it and never opens anything in it: every
look here is a ``stat``. Signing in is the person's own step, in a terminal; MetaBot
prints the line and never runs it.

The folder must be there, be the person's, be owner-only, and sit outside every project
and outside MetaBot's own home: a run's tools work in a project, so a sign-in folder in
one is in reach of the agent.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import fcntl
import os
import shlex
import stat
import sys
from collections.abc import AsyncIterator, Callable
from pathlib import Path

from metabot import sandbox
from metabot.config import (
    LOGIN_ENGINES,
    MAX_AT_ONCE,
    ON_SIGN_IN,
    Login,
    load_config,
    login_name_ok,
    login_of,
    save_config,
)
from metabot.paths import metabot_home

# The file each CLI keeps its sign-in in, looked at with stat alone (never opened).
SIGN_IN_FILES = {"claude": ".credentials.json", "codex": "auth.json"}
HOME_VARIABLES = {"claude": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME"}
SIGN_IN_COMMANDS = {"claude": "claude", "codex": "codex login"}

# The vendors' rules, printed by ``login add``: facts and links, never advice on the law.
# A vendor line says only what that vendor's own page says (read 2026-11-03); MetaBot's
# own behaviour and rules are said apart, as its own.
VENDOR_NOTE = """\
About logins: a login is one of your own accounts, kept apart, such as work and personal.
What the vendors' pages say:
  - The claude docs describe signing in to several accounts, each in its own folder,
    and say its plan limits "assume ordinary, individual usage".
  - With file storage, codex keeps its sign-in in auth.json under CODEX_HOME.
  - The codex docs call an API key "the recommended default for automation".
MetaBot's rules:
  - MetaBot gives each login its own folder (CLAUDE_CONFIG_DIR or CODEX_HOME).
  - Never use extra accounts to get around a plan's limits.
  - For unattended runs, such as schedules, use an API key.
Each vendor's own terms apply to each account:
  https://code.claude.com/docs/en/legal-and-compliance
  https://developers.openai.com/codex/auth"""


CODEX_ONE_AT_A_TIME = (
    "a codex login runs one job at a time: codex keeps its sign-in in one file in the"
    " folder, and its own guide says not to share that file across concurrent jobs"
)
SLOT_POLL_S = 0.2


def _err(message: str) -> None:
    print(f"metabot: {message}", file=sys.stderr)


def sign_in_line(engine: str, home: str) -> str:
    """The line the person runs, in a terminal, to sign in to ``home``."""
    return f"{HOME_VARIABLES[engine]}={shlex.quote(home)} {SIGN_IN_COMMANDS[engine]}"


def _resolved(path: str) -> Path:
    try:
        return Path(path).resolve()
    except (OSError, RuntimeError):  # a loop of links
        return Path(os.path.abspath(path))


def _holds(outer: Path, inner: Path) -> bool:
    return inner == outer or inner.is_relative_to(outer)


def folder_problems(home: str) -> list[str]:
    """What is wrong with ``home`` as a sign-in folder now, each with its mend; stat only."""
    q = shlex.quote(home)
    try:
        st = os.stat(home)
    except FileNotFoundError:
        return [f"{home} is not there: make it with `mkdir -m 700 {q}`, then sign in"]
    except (OSError, ValueError) as exc:
        return [f"{home} cannot be looked at: {exc}"]
    if not stat.S_ISDIR(st.st_mode):
        return [f"{home} is not a folder"]
    problems = []
    if st.st_uid != os.getuid():
        problems.append(f"{home} belongs to another user")
    if st.st_mode & 0o077:
        problems.append(f"others can enter {home}: close it with `chmod 700 {q}`")
    return problems


def run_problems(home: str) -> list[str]:
    """What stops a run on the folder ``home`` now, each with its mend; stat only.

    The checks ``login add`` made, made again as a run starts: a folder can be opened up,
    moved or taken into a project after it was added."""
    problems = folder_problems(home)
    if problems:
        return problems
    placed = _placement_problem(home, {})
    return [placed] if placed else []


def _placement_problem(home: str, taken: dict[str, Login]) -> str | None:
    """Why ``home`` is in the wrong place for a sign-in folder; None when it is not.

    Each rule holds for the folder as named and as its links resolve: a link that sits in a
    project is in reach of a run there, and a checkout could point it elsewhere. A git
    repository at the home folder itself, as dotfiles keep, is no project."""
    here = _resolved(home)
    forms = list(dict.fromkeys((Path(os.path.normpath(home)), here)))
    user = _resolved(str(sandbox.user_home()))
    own = _resolved(str(metabot_home()))
    for form in forms:
        if _holds(form, user):
            return f"{home} is your home folder, or holds it: name the sign-in folder itself"
        if _holds(own, form) or _holds(form, own):
            return f"{home} is inside MetaBot's own home, or holds it: keep sign-in folders apart"
    if os.path.lexists(here / ".git"):
        return f"{home} is a git repository: a sign-in folder sits outside every project"
    for form in forms:
        root = sandbox.git_root(form.parent)
        if root is not None and not _holds(root, user):
            return f"{home} is inside the project {root}, where a run's tools can reach it"
    for other in taken.values():
        theirs = list(dict.fromkeys((Path(os.path.normpath(other.home)), _resolved(other.home))))
        for mine in forms:
            for their in theirs:
                if mine == their:
                    return f"{home} is already login {other.name}"
                if _holds(mine, their) or _holds(their, mine):
                    return f"{home} is inside or around login {other.name}: keep them apart"
    return None


def _logins() -> dict[str, Login]:
    cfg = load_config()
    out = {}
    for name in cfg.logins:
        login = login_of(cfg, name)
        if login is not None:
            out[name] = login
    return out


def cmd_login_add(args: argparse.Namespace) -> int:
    name, engine = args.name, args.engine
    if not login_name_ok(name):
        _err(f"{name!r} is not a login name: a short lower-case word, letters, digits and -")
        return 2
    if engine not in LOGIN_ENGINES:
        _err(f"logins are for claude or codex; {engine!r} has none in this version")
        return 2
    if not 1 <= args.max_at_once <= MAX_AT_ONCE:
        _err(f"--max-at-once is 1 to {MAX_AT_ONCE}")
        return 2
    if engine == "codex" and args.max_at_once > 1:
        _err(CODEX_ONE_AT_A_TIME)
        return 2
    home = os.path.expanduser(args.home)
    if "\0" in home or not os.path.isabs(home):
        _err(f"--home is the folder's absolute path, such as ~/.{engine}-{name}; not {home!r}")
        return 2
    home = os.path.normpath(home)
    taken = _logins()
    if name in taken:
        _err(f"there is a login {name} already; to point it elsewhere: metabot login remove {name}")
        return 2
    problems = folder_problems(home)
    placement = None if problems else _placement_problem(home, taken)
    if placement is not None:
        problems = [placement]
    if problems:
        for problem in problems:
            _err(problem)
        return 2
    cfg = load_config()
    entry = {
        "engine": engine,
        "home": home,
        "max_at_once": args.max_at_once,
        "on_sign_in": args.on_sign_in,
    }
    cfg.logins = {**cfg.logins, name: entry}
    save_config(cfg)
    print(f"login {name}: {engine}, the folder {home} (MetaBot never opens what is in it)")
    print(f"if another sign-in is set when a run starts: {_choice_said(args.on_sign_in, name)}")
    print("sign in once yourself, in a terminal:")
    print(f"  {sign_in_line(engine, home)}")
    print(f"then: metabot login check {name}")
    print()
    print(VENDOR_NOTE)
    return 0


def _choice_said(choice: str, name: str) -> str:
    if choice == "clear":
        return "clear, MetaBot unsets it for this login's runs, and the receipt counts it"
    return (
        "refuse, the run stops and names it"
        f" (to clear it instead: metabot login set {name} --on-sign-in clear)"
    )


def cmd_login_set(args: argparse.Namespace) -> int:
    cfg = load_config()
    spec = cfg.logins.get(args.name)
    if not isinstance(spec, dict):
        _err(f"there is no login {args.name!r}; see metabot login list")
        return 2
    if args.on_sign_in is None and args.max_at_once is None:
        _err("nothing to set: name --on-sign-in refuse|clear or --max-at-once N")
        return 2
    entry = dict(spec)
    if args.max_at_once is not None:
        if not 1 <= args.max_at_once <= MAX_AT_ONCE:
            _err(f"--max-at-once is 1 to {MAX_AT_ONCE}")
            return 2
        if spec.get("engine") == "codex" and args.max_at_once > 1:
            _err(CODEX_ONE_AT_A_TIME)
            return 2
        entry["max_at_once"] = args.max_at_once
    if args.on_sign_in is not None:
        if args.on_sign_in not in ON_SIGN_IN:
            _err(f"--on-sign-in is {' or '.join(ON_SIGN_IN)}")
            return 2
        entry["on_sign_in"] = args.on_sign_in
    cfg.logins = {**cfg.logins, args.name: entry}
    save_config(cfg)
    login = login_of(cfg, args.name)
    assert login is not None
    print(_listed(login))
    return 0


def _listed(login: Login) -> str:
    return (
        f"{login.name}  {login.engine}  {login.home}  {login.max_at_once} at once"
        f"  another sign-in: {login.on_sign_in}"
    )


def cmd_login_list(args: argparse.Namespace) -> int:
    logins = _logins()
    if not logins:
        print("no logins; add one: metabot login add NAME --engine claude --home FOLDER")
        return 0
    for login in logins.values():
        print(_listed(login))
    return 0


def cmd_login_check(args: argparse.Namespace) -> int:
    logins = _logins()
    if args.name is not None:
        if args.name not in logins:
            _err(f"there is no login {args.name!r}; see metabot login list")
            return 2
        logins = {args.name: logins[args.name]}
    if not logins:
        print("no logins; add one: metabot login add NAME --engine claude --home FOLDER")
        return 0
    failed = False
    for login in logins.values():
        problems = folder_problems(login.home)
        if problems:
            failed = True
            print(f"{login.name}: " + "; ".join(problems))
            continue
        signed = os.path.lexists(os.path.join(login.home, SIGN_IN_FILES[login.engine]))
        if signed:
            print(f"{login.name}: ok; a sign-in file is there (not opened)")
        else:
            print(f"{login.name}: the folder is ok, and holds no sign-in file yet; sign in with:")
            print(f"  {sign_in_line(login.engine, login.home)}")
    return 1 if failed else 0


def cmd_login_remove(args: argparse.Namespace) -> int:
    cfg = load_config()
    if args.name not in cfg.logins:
        _err(f"there is no login {args.name!r}; see metabot login list")
        return 2
    from metabot.roster import roles_on_login

    spec = cfg.logins[args.name]
    engine = spec.get("engine") if isinstance(spec, dict) else None
    for role in roles_on_login(args.name, cfg):
        _err(
            f"the {role} role runs on it; first give the role its own sign-in:"
            f" metabot roster set {role} --engine {engine}"
        )
    if roles_on_login(args.name, cfg):
        return 2
    cfg.logins = {k: v for k, v in cfg.logins.items() if k != args.name}
    save_config(cfg)
    print(f"login {args.name} removed; its folder is left as it is")
    return 0


def session_login(session: str) -> tuple[bool, str | None]:
    """Whether a run here kept the engine session ``session``, and the login it ran on.

    The run index keeps each run's session and login. The last run to keep the session
    says where it lives: an engine keeps a session in the folder it signed in from, so a
    resume has to go back to that login, or to none.
    """
    from metabot.receipts import list_index

    rows = [r for r in list_index() if r.get("session_id") == session]
    if not rows:
        return False, None
    login = rows[-1].get("login")
    return True, login if isinstance(login, str) and login else None


def run_login(
    given: str | None, role: str | None, role_login: str | None, resume: str | None
) -> tuple[str | None, str | None]:
    """The login a run takes, and the line that says why, from ``--login``, the role and
    the session it resumes. ValueError when they disagree.

    A resumed session goes back to the login it ran on, or to none, whatever the role
    says; ``--login`` must agree with it. Otherwise ``--login`` wins over the role's.
    """
    if resume:
        kept, tied = session_login(resume)
        if kept:
            if given is not None and given != tied:
                if tied is None:
                    raise ValueError(
                        f"session {resume} ran with no login; resume it without --login"
                    )
                raise ValueError(
                    f"session {resume} ran on the login {tied}; resume it there:"
                    f" --login {tied}, or leave --login out"
                )
            if tied is not None and given is None:
                return tied, f"[metabot] login {tied}: session {resume} ran there"
            return tied, None
    if given is not None:
        return given, None
    if role_login is not None:
        return role_login, f"[metabot] login {role_login}, from the {role} role"
    return None, None


def slots_of(login: Login) -> int:
    """How many runs a login takes at once: what it says, and one for codex."""
    return 1 if login.engine == "codex" else max(1, int(login.max_at_once))


@contextlib.asynccontextmanager
async def login_slot(
    login: Login,
    *,
    poll: float = SLOT_POLL_S,
    on_wait: Callable[[str], None] | None = None,
) -> AsyncIterator[int]:
    """Hold one of a login's runs while an engine runs on it; wait while all are taken.

    Each run is a lock on a file in MetaBot's home (``locks/login-<name>.<n>``), never in
    the login's folder, so it holds across every MetaBot process here. The lock goes
    with the process: a run that dies gives its place back. ``on_wait`` hears once that
    the run waited.
    """
    folder = metabot_home() / "locks"
    folder.mkdir(mode=0o700, exist_ok=True)
    n = slots_of(login)
    said = False
    while True:
        for i in range(n):
            fd = os.open(folder / f"login-{login.name}.{i}", os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                continue
            try:
                yield i
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
            return
        if not said and on_wait is not None:
            on_wait(f"waited for a free run on the login {login.name} ({n} at once)")
        said = True
        await asyncio.sleep(poll)
