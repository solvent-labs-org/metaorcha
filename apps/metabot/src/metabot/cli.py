"""The ``metabot`` command line: the parser and dispatch.

The commands live in :mod:`metabot.commands` and the chat loop in
:mod:`metabot.repl`.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from collections.abc import Sequence

from metabot import __version__
from metabot.engines import BINARIES as ENGINE_BINARIES
from metabot.engines import ENGINES
from metabot.paths import metabot_home

TOKEN_FLAG_REFUSED = (
    "metabot: tokens are never taken on the command line (shell history keeps them); "
    "pipe the token on stdin, or type it when asked"
)


def _binary_version(path: str) -> str:
    try:
        out = subprocess.run(
            [path, "--version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "version unknown"
    line = (out.stdout or out.stderr).strip().splitlines()
    return line[0] if line else "version unknown"


def cmd_doctor(_args: argparse.Namespace) -> int:
    from metabot.needs import doctor_lines
    from metabot.plugins import PluginError
    from metabot.receipts import key_problem, resolve_key_path

    home = metabot_home()
    print(f"python   {sys.version.split()[0]}")
    print(f"home     {home} (mode {oct(os.stat(home).st_mode & 0o777)})")
    key = resolve_key_path()
    there = key.exists() or key.is_symlink()
    problem = key_problem(key) if there else None  # a missing key is made on first use
    if problem:
        print(f"key      {key} (does not read: {problem})")
    else:
        print(f"key      {key} ({'present' if there else 'missing: run metabot init'})")
    for name, binary in ENGINE_BINARIES.items():
        path = shutil.which(binary)
        if path:
            print(f"engine   {name:<7} on PATH {path} {_binary_version(path)}")
        else:
            print(f"engine   {name:<7} missing")
    try:
        needs = doctor_lines()
    except PluginError as exc:  # a run that takes the plugins refuses them too
        print(f"plugins  {exc}")
        return 1
    for line in needs:
        print(line)
    return 1 if problem else 0


def cmd_hook(args: argparse.Namespace) -> int:
    from metabot import hook

    return hook.main(args.control, args.prefix, args.switchboard)


def cmd_mcp_serve(args: argparse.Namespace) -> int:
    import asyncio
    from pathlib import Path

    from metabot.switchboard import serve

    os.environ["METABOT_HOME"] = args.home
    only = args.only or None  # no --only: every active connection
    asyncio.run(serve(Path(args.run_dir), Path(args.control), not args.no_connections, only))
    return 0


def _lazy(module: str, name: str):
    """Import a command only when it runs, so ``metabot --version`` stays fast."""

    def run(args: argparse.Namespace) -> int:
        import importlib

        return int(getattr(importlib.import_module(module), name)(args))

    return run


def _model_name(value: str) -> str:
    """``-m``: a model name as an engine takes it. It follows a flag on the engine's argv,
    so it is never a flag itself."""
    from metabot.config import MODEL_RE

    if not MODEL_RE.match(value):
        raise argparse.ArgumentTypeError(f"{value[:60]!r} is not a model name")
    return value


def _run_options(p: argparse.ArgumentParser) -> None:
    p.add_argument("-e", "--engine", choices=list(ENGINES), help="default: metabot engines")
    p.add_argument(
        "-m", "--model", type=_model_name, help="the engine's model (default: the engine's own)"
    )
    p.add_argument(
        "--allow-write", action="store_true", help="run writes without asking (never destructive)"
    )
    p.add_argument(
        "--allow",
        action="append",
        default=[],
        metavar="TOOL",
        help="run this write tool without asking, e.g. Edit or gh__create_issue (repeatable)",
    )
    p.add_argument("--read-only", action="store_true", help="the engine reads and changes nothing")
    p.add_argument("--cwd", help="the directory to work in (default: here)")
    p.add_argument(
        "--no-connections", action="store_true", help="leave the connections out of this run"
    )
    p.add_argument(
        "--no-plugins", action="store_true", help="leave the enabled plugins out of this run"
    )
    p.add_argument(
        "--role",
        metavar="ROLE",
        help="run on a role's engine and model from metabot roster; -e and -m win",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="metabot",
        description="A local bot that runs on the plan you already have and proves its work.",
        allow_abbrev=False,
    )
    parser.add_argument("--version", action="store_true", help="print the version and exit")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    def add(name: str, **kw) -> argparse.ArgumentParser:
        return sub.add_parser(name, allow_abbrev=False, **kw)

    init = add("init", help="create the signing key and pick a default engine")
    init.add_argument("--engine", help="the default engine")
    init.set_defaults(func=_lazy("metabot.commands", "cmd_init"))

    add("engines", help="which engines are here").set_defaults(
        func=_lazy("metabot.commands", "cmd_engines")
    )

    ask = add("ask", help="run one prompt and print the answer")
    _run_options(ask)
    ask.add_argument("--resume", metavar="SESSION", help="continue an engine session")
    ask.add_argument("--json", action="store_true", help="print the result as one JSON object")
    ask.add_argument("-q", "--quiet", action="store_true", help="no trace on stderr")
    ask.add_argument(
        "--suggest",
        action="store_true",
        help="run on the role metabot suggest picks for this prompt; -e and -m win",
    )
    ask.add_argument(
        "--project-receipt",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="also copy the receipt to <project>/.orcha/receipts/ "
        "(default: metabot config, project_receipts)",
    )
    ask.add_argument("prompt", nargs="+", help="the prompt; '-' reads it from stdin")
    ask.set_defaults(func=_lazy("metabot.commands", "cmd_ask"))

    config = add("config", help="this machine's settings")
    csub = config.add_subparsers(dest="config_command", metavar="ACTION", required=True)
    shown = "the settings config set changes"
    csub.add_parser("show", allow_abbrev=False, help=shown).set_defaults(
        func=_lazy("metabot.commands", "cmd_config_show")
    )
    c_set = csub.add_parser("set", allow_abbrev=False, help="change one setting")
    c_set.add_argument("key", help="project_receipts or fan_cap")
    c_set.add_argument("value")
    c_set.set_defaults(func=_lazy("metabot.commands", "cmd_config_set"))

    sug = add("suggest", help="which role, engine and model suit a task, and the rule that says so")
    sug.add_argument("task", nargs="+", help="the task; '-' reads it from stdin")
    sug.set_defaults(func=_lazy("metabot.suggest", "cmd_suggest"))

    chat = add("chat", help="talk to the bot in this terminal (also: metabot with no command)")
    _run_options(chat)
    chat.set_defaults(func=_lazy("metabot.repl", "cmd_chat"))

    connect = add("connect", help="connections to MCP servers, scoped and revocable")
    csub = connect.add_subparsers(dest="connect_command", metavar="ACTION", required=True)
    c_add = csub.add_parser(
        "add",
        allow_abbrev=False,
        help="add a connection (the token is read from stdin or asked for, never an argument)",
    )
    c_add.add_argument("name")
    c_add.add_argument("--token-env", metavar="VAR", help="a stdio server reads its token here")
    c_add.add_argument("--header", help="an http server's token header (default Authorization)")
    c_add.add_argument("--no-token", action="store_true", help="this server needs no token")
    c_add.add_argument(
        "--browser",
        action="store_true",
        help="it drives a browser: each call is classed by the origin it acts on",
    )
    c_add.add_argument(
        "--origin",
        action="append",
        metavar="ORIGIN",
        help="with --browser, where it may act besides this machine, as http(s)://host[:port|:*]"
        " (repeatable)",
    )
    target = c_add.add_mutually_exclusive_group(required=True)
    target.add_argument("--url", help="an http(s) MCP server")
    target.add_argument(
        "--command", nargs=argparse.REMAINDER, help="a stdio MCP server: everything after it"
    )
    c_add.set_defaults(func=_lazy("metabot.commands", "cmd_connect_add"))
    csub.add_parser("list", allow_abbrev=False, help="the connections").set_defaults(
        func=_lazy("metabot.commands", "cmd_connect_list")
    )
    for action, helptext in (
        ("test", "list a connection's tools"),
        ("revoke", "revoke a connection and delete its token"),
    ):
        p = csub.add_parser(action, allow_abbrev=False, help=helptext)
        p.add_argument("name")
        p.set_defaults(func=_lazy("metabot.commands", f"cmd_connect_{action}"))
    scope = csub.add_parser(
        "scope", allow_abbrev=False, help="each tool's class: read, write or destructive"
    )
    scope.add_argument("name")
    scope.add_argument(
        "--set",
        action="append",
        metavar="TOOL=CLASS",
        help="tighten a tool's class (repeatable)",
    )
    scope.set_defaults(func=_lazy("metabot.commands", "cmd_connect_scope"))

    office = add("office", help="run a Code Desks office headless")
    osub = office.add_subparsers(dest="office_command", metavar="ACTION", required=True)
    o_check = osub.add_parser(
        "check", allow_abbrev=False, help="the desks and checks MetaBot reads, and what cannot run"
    )
    o_check.add_argument("--dir", help="the office's folder (default: here or above)")
    o_check.set_defaults(func=_lazy("metabot.office_commands", "cmd_office_check"))
    o_run = osub.add_parser(
        "run",
        allow_abbrev=False,
        help="run desks one after another on a task, each in its own worktree",
    )
    o_run.add_argument("--dir", help="the office's folder (default: here or above)")
    o_run.add_argument(
        "--fan",
        action="store_true",
        help="run the desks at once from one base, to compare their work, not one after another; "
        "advisors do not run in a fan",
    )
    o_run.add_argument(
        "--no-advisors",
        action="store_true",
        help="run the desks without the advisor desks that review their work",
    )
    o_run.add_argument(
        "--checks",
        action="store_true",
        help="after each desk that changed a file, run the office's gate checks on its work"
        " (after metabot office trust records that you agree)",
    )
    o_run.add_argument(
        "--desks", metavar="A,B,C", help="the desks to run, in order (default: all but advisors)"
    )
    o_run.add_argument(
        "--allow-write", action="store_true", help="run writes without asking (never destructive)"
    )
    o_run.add_argument(
        "--allow",
        action="append",
        default=[],
        metavar="TOOL",
        help="run this write tool without asking",
    )
    o_run.add_argument(
        "--no-connections", action="store_true", help="leave the connections out of this run"
    )
    o_run.add_argument(
        "--no-plugins", action="store_true", help="leave the enabled plugins out of this run"
    )
    o_run.add_argument("-q", "--quiet", action="store_true", help="no trace on stderr")
    o_run.add_argument("task", nargs="+", help="the task; '-' reads it from stdin")
    o_run.set_defaults(func=_lazy("metabot.office_commands", "cmd_office_run"))
    o_trust = osub.add_parser(
        "trust",
        allow_abbrev=False,
        help="show the office's gate checks and agree to run them on this machine",
    )
    o_trust.add_argument("--dir", help="the office's folder (default: here or above)")
    o_trust.add_argument("--yes", action="store_true", help="trust them without asking")
    o_trust.set_defaults(func=_lazy("metabot.office_commands", "cmd_office_trust"))

    desks = add("desks", help="desk worktrees: list them, pick one's work, clean a run")
    dsub = desks.add_subparsers(dest="desks_command", metavar="ACTION", required=True)
    d_list = dsub.add_parser("list", allow_abbrev=False, help="every desk branch here")
    d_pick = dsub.add_parser(
        "pick", allow_abbrev=False, help="stage one desk's work as a squash; never commits"
    )
    d_pick.add_argument("desk", metavar="RUN/DESK")
    d_clean = dsub.add_parser(
        "clean", allow_abbrev=False, help="remove a run's desk worktrees and branches"
    )
    d_clean.add_argument("run")
    d_clean.add_argument("--yes", action="store_true", help="do not ask to confirm")
    for action, parser_ in (("list", d_list), ("pick", d_pick), ("clean", d_clean)):
        parser_.add_argument("--dir", help="a folder in the repository (default: here)")
        parser_.set_defaults(func=_lazy("metabot.office_commands", f"cmd_desks_{action}"))

    plugin = add("plugin", help="plugins: skills, commands and agents, pinned by hash")
    psub = plugin.add_subparsers(dest="plugin_command", metavar="ACTION", required=True)
    market = psub.add_parser("marketplace", allow_abbrev=False, help="folders that list plugins")
    msub = market.add_subparsers(dest="marketplace_command", metavar="ACTION", required=True)
    m_add = msub.add_parser("add", allow_abbrev=False, help="add a marketplace folder")
    m_add.add_argument("name")
    m_add.add_argument("path")
    m_list = msub.add_parser("list", allow_abbrev=False, help="each marketplace and its plugins")
    m_remove = msub.add_parser("remove", allow_abbrev=False, help="forget a marketplace")
    m_remove.add_argument("name")
    for action, parser_ in (("add", m_add), ("list", m_list), ("remove", m_remove)):
        parser_.set_defaults(
            func=_lazy("metabot.plugin_commands", f"cmd_plugin_marketplace_{action}")
        )
    p_install = psub.add_parser(
        "install", allow_abbrev=False, help="show what a plugin carries, then install it, off"
    )
    p_install.add_argument("ref", metavar="NAME@MARKETPLACE")
    p_import = psub.add_parser(
        "import-skills", allow_abbrev=False, help="make a plugin of a folder of skills"
    )
    p_import.add_argument("dir", help="a folder of skill folders, each with a SKILL.md")
    p_import.add_argument("--name", required=True, help="the plugin's name")
    p_list = psub.add_parser("list", allow_abbrev=False, help="the installed plugins")
    p_show = psub.add_parser("show", allow_abbrev=False, help="what an installed plugin carries")
    p_enable = psub.add_parser(
        "enable", allow_abbrev=False, help="turn a plugin on for every run; other versions go off"
    )
    hooks = p_enable.add_mutually_exclusive_group()
    hooks.add_argument(
        "--allow-hooks", action="store_true", help="run its hooks, which run outside the gate"
    )
    hooks.add_argument("--without-hooks", action="store_true", help="leave its hooks out")
    p_enable.add_argument(
        "--add-servers", action="store_true", help="make its MCP servers connections, gated"
    )
    p_disable = psub.add_parser("disable", allow_abbrev=False, help="turn a plugin off")
    p_remove = psub.add_parser("remove", allow_abbrev=False, help="delete an installed plugin")
    for parser_ in (p_show, p_enable, p_disable, p_remove):
        parser_.add_argument("ref", metavar="PLUGIN", help="NAME, NAME@MARKETPLACE or a hash")
    for parser_ in (p_install, p_import, p_remove):
        parser_.add_argument("--yes", action="store_true", help="do not ask to confirm")
    for action, parser_ in (
        ("install", p_install),
        ("import_skills", p_import),
        ("list", p_list),
        ("show", p_show),
        ("enable", p_enable),
        ("disable", p_disable),
        ("remove", p_remove),
    ):
        parser_.set_defaults(func=_lazy("metabot.plugin_commands", f"cmd_plugin_{action}"))

    routine = add("routine", help="goals that run on a schedule, with no person there to ask")
    rtsub = routine.add_subparsers(dest="routine_command", metavar="ACTION", required=True)
    rt_save = rtsub.add_parser(
        "save", allow_abbrev=False, help="check a routine and save it; save it again to change it"
    )
    rt_save.add_argument("name")
    rt_save.add_argument(
        "--cron",
        required=True,
        metavar="'M H DOM MON DOW'",
        help="when it runs, e.g. '0 9 * * 1-5'",
    )
    rt_save.add_argument("--tz", metavar="ZONE", help="the cron's zone (default: this machine's)")
    rt_save.add_argument("-e", "--engine", choices=list(ENGINES), help="default: metabot engines")
    rt_save.add_argument("-m", "--model", help="the engine's model (default: the engine's own)")
    rt_save.add_argument("--cwd", help="the directory to work in (default: here)")
    rt_save.add_argument(
        "--connection",
        action="append",
        default=[],
        metavar="NAME",
        help="a connection its firings may use (repeatable; default: none)",
    )
    rt_save.add_argument(
        "--allow",
        action="append",
        default=[],
        metavar="CONNECTION__CAPABILITY",
        help="a write that runs without asking, e.g. gh__create_issue (repeatable)",
    )
    rt_save.add_argument(
        "--criteria", metavar="A,B", help="judged on every step: exit_zero, citations_required"
    )
    rt_save.add_argument("--turns", type=int, help="the most tool calls a firing makes (50)")
    rt_save.add_argument(
        "--budget", type=int, metavar="TOKENS", help="the most tokens a firing uses"
    )
    rt_save.add_argument(
        "--no-plugins", action="store_true", help="leave the enabled plugins out of its firings"
    )
    rt_save.add_argument("--off", action="store_true", help="save it switched off")
    rt_save.add_argument("goal", nargs="+", help="the goal; '-' reads it from stdin")
    rt_list = rtsub.add_parser("list", allow_abbrev=False, help="the routines and when each is due")
    rt_run = rtsub.add_parser("run", allow_abbrev=False, help="fire a routine now, as serve would")
    rt_run.add_argument("name")
    rt_run.add_argument("-q", "--quiet", action="store_true", help="no trace on stderr")
    rt_enable = rtsub.add_parser("enable", allow_abbrev=False, help="switch a routine on")
    rt_disable = rtsub.add_parser("disable", allow_abbrev=False, help="switch a routine off")
    rt_remove = rtsub.add_parser(
        "remove", allow_abbrev=False, help="forget a routine; its firings and receipts stay"
    )
    rt_remove.add_argument("--yes", action="store_true", help="do not ask to confirm")
    for parser_ in (rt_enable, rt_disable, rt_remove):
        parser_.add_argument("name")
    rt_firings = rtsub.add_parser("firings", allow_abbrev=False, help="what each firing did")
    rt_firings.add_argument("name", nargs="?", help="one routine (default: all)")
    rt_firings.add_argument("-n", "--limit", type=int, default=20)
    for action, parser_ in (
        ("save", rt_save),
        ("list", rt_list),
        ("run", rt_run),
        ("enable", rt_enable),
        ("disable", rt_disable),
        ("remove", rt_remove),
        ("firings", rt_firings),
    ):
        parser_.set_defaults(func=_lazy("metabot.routine_commands", f"cmd_routine_{action}"))

    roster = add("roster", help="each role's engine and model; yours win over the defaults")
    roster.set_defaults(func=_lazy("metabot.roster", "cmd_roster"))
    ro_sub = roster.add_subparsers(dest="roster_command", metavar="ACTION")
    ro_set = ro_sub.add_parser("set", allow_abbrev=False, help="give a role an engine and model")
    ro_set.add_argument("role", help="fast, build, review, second or private")
    ro_set.add_argument("--engine", required=True, help=f"one of {', '.join(ENGINES)}")
    ro_set.add_argument("--model", help="the engine's model (default: the engine's own)")
    ro_set.set_defaults(func=_lazy("metabot.roster", "cmd_roster_set"))
    ro_unset = ro_sub.add_parser("unset", allow_abbrev=False, help="give a role back its default")
    ro_unset.add_argument("role")
    ro_unset.set_defaults(func=_lazy("metabot.roster", "cmd_roster_unset"))

    add("serve", help="fire routines as they come due, one at a time").set_defaults(
        func=_lazy("metabot.serve", "cmd_serve")
    )

    ui = add("ui", help="a local web page for runs, offices, connections and routines")
    ui.add_argument(
        "--port", type=int, default=0, help="the port on 127.0.0.1 (default: a free one)"
    )
    ui.set_defaults(func=_lazy("metabot.web_commands", "cmd_ui"))

    receipts = add("receipts", help="the receipts of past runs")
    rsub = receipts.add_subparsers(dest="receipts_command", metavar="ACTION", required=True)
    r_list = rsub.add_parser("list", allow_abbrev=False, help="recent runs")
    r_list.add_argument("-n", "--limit", type=int, default=20)
    r_list.set_defaults(func=_lazy("metabot.commands", "cmd_receipts_list"))
    r_show = rsub.add_parser("show", allow_abbrev=False, help="what a run did")
    r_show.add_argument("run")
    r_show.set_defaults(func=_lazy("metabot.commands", "cmd_receipts_show"))
    r_verify = rsub.add_parser("verify", allow_abbrev=False, help="check a receipt offline")
    r_verify.add_argument("ref", metavar="RUN_OR_FILE")
    r_verify.set_defaults(func=_lazy("metabot.commands", "cmd_receipts_verify"))

    mail = add("mail", help="notes between sessions, in any harness")
    msub = mail.add_subparsers(dest="mail_command", metavar="ACTION", required=True)
    mailbox_help = "your mailbox (or METABOT_MAILBOX)"
    m_send = msub.add_parser("send", allow_abbrev=False, help="leave a note in another mailbox")
    m_send.add_argument("to", help="the mailbox to write to")
    m_send.add_argument("-m", "--message", metavar="TEXT", help="the note (default: stdin)")
    m_send.add_argument("--from", dest="sender", metavar="NAME", help=mailbox_help)
    m_send.add_argument("--thread", help="a word that ties the notes of one task together")
    m_read = msub.add_parser("read", allow_abbrev=False, help="print your mail and mark it read")
    m_read.add_argument("--for", dest="name", metavar="NAME", help=mailbox_help)
    m_read.add_argument("--peek", action="store_true", help="print it and leave it unread")
    m_wait = msub.add_parser("wait", allow_abbrev=False, help="wait for mail, print it, exit")
    m_wait.add_argument("--for", dest="name", metavar="NAME", help=mailbox_help)
    m_wait.add_argument(
        "--timeout", type=float, default=600.0, help="seconds (default 600); then exit 1"
    )
    m_wait.add_argument("--every", type=float, default=1.0, help=argparse.SUPPRESS)
    m_hook = msub.add_parser("hook", allow_abbrev=False, help="a harness hook that adds your mail")
    m_hook.add_argument("harness", choices=["claude", "cursor"])
    m_hook.add_argument("event", choices=["prompt", "tool", "stop"], help="tool: after a tool call")
    m_hook.add_argument("--for", dest="name", metavar="NAME", help=mailbox_help)
    for action, parser_ in (("send", m_send), ("read", m_read), ("wait", m_wait), ("hook", m_hook)):
        parser_.set_defaults(func=_lazy("metabot.mail", f"cmd_mail_{action}"))

    doctor = add("doctor", help="check engines, key and home")
    doctor.set_defaults(func=cmd_doctor)

    hook = sub.add_parser("hook", allow_abbrev=False)
    hook_sub = hook.add_subparsers(dest="hook_event", required=True)
    pre = hook_sub.add_parser("pre-tool-use", allow_abbrev=False)
    pre.add_argument("--control", required=True)
    pre.add_argument("--prefix", default="claude-code")
    pre.add_argument("--switchboard", action="store_true")
    pre.set_defaults(func=cmd_hook)
    mcp = sub.add_parser("mcp", allow_abbrev=False)
    mcp_sub = mcp.add_subparsers(dest="mcp_command", required=True)
    mcp_serve = mcp_sub.add_parser("serve", allow_abbrev=False)
    mcp_serve.add_argument("--home", required=True)
    mcp_serve.add_argument("--run-dir", required=True)
    mcp_serve.add_argument("--control", required=True)
    mcp_serve.add_argument("--no-connections", action="store_true")
    mcp_serve.add_argument("--only", action="append", default=[], metavar="CONNECTION_DID")
    mcp_serve.set_defaults(func=cmd_mcp_serve)
    return parser


def _token_flag_given(argv: list[str]) -> bool:
    """A ``--token``-like flag before a stdio server's own command line.

    Checked before parsing because a parse error would print the value back.
    """
    for word in argv:
        if word == "--command":
            return False
        flag = word.split("=", 1)[0]
        if flag.startswith("--tok") and flag != "--token-env":
            return True
    return False


def main(argv: Sequence[str] | None = None) -> int:
    words = list(argv) if argv is not None else sys.argv[1:]
    if _token_flag_given(words):
        print(TOKEN_FLAG_REFUSED, file=sys.stderr)
        return 2
    parser = build_parser()
    args = parser.parse_args(words)
    if args.version:
        print(f"metabot {__version__}")
        return 0
    func = getattr(args, "func", None)
    if func is None:
        if sys.stdin.isatty() and sys.stdout.isatty():
            return main(["chat"])
        parser.print_help()
        return 0
    from metabot.config import ConfigError
    from metabot.connections import ConnectionFileError
    from metabot.plugins import PluginError
    from metabot.receipts import KeyFileError

    try:
        return int(func(args))
    except (
        ConfigError,
        ConnectionFileError,
        PluginError,
        KeyFileError,
    ) as exc:  # a file a run needs
        print(f"metabot: {exc}", file=sys.stderr, flush=True)
        return 1
