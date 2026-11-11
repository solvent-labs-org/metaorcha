"""The ``metabot`` command line: the parser and dispatch.

The commands live in :mod:`metabot.commands` and the chat loop in
:mod:`metabot.repl`.
"""

from __future__ import annotations

import argparse
import io
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
    """The first line ``path --version`` says, whatever its bytes, as a name's are read."""
    try:
        out = subprocess.run(
            [path, "--version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            encoding="utf-8",
            errors="surrogateescape",
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "version unknown"
    line = (out.stdout or out.stderr).strip().splitlines()
    return line[0] if line else "version unknown"


def cmd_doctor(args: argparse.Namespace) -> int:
    if getattr(args, "models", None) is not None:
        return _doctor_models(args.models)
    from metabot.approvals import printable
    from metabot.needs import doctor_lines
    from metabot.plugins import PluginError
    from metabot.receipts import key_problem, resolve_key_path

    home = metabot_home()
    print(f"python   {sys.version.split()[0]}")
    print(f"home     {printable(str(home), 2000)} (mode {oct(os.stat(home).st_mode & 0o777)})")
    key = resolve_key_path()
    named = printable(str(key), 2000)
    there = key.exists() or key.is_symlink()
    problem = key_problem(key) if there else None  # a missing key is made on first use
    if problem:
        print(f"key      {named} (does not read: {printable(problem, 2000)})")
    else:
        print(f"key      {named} ({'present' if there else 'missing: run metabot init'})")
    for name, binary in ENGINE_BINARIES.items():
        path = shutil.which(binary)
        if path:  # the version is the binary's own words: shown escaped, as an argument is
            said = printable(_binary_version(path), 200)
            print(f"engine   {name:<7} on PATH {printable(path, 2000)} {said}")
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


def _doctor_models(names: list[str]) -> int:
    """Probe each local model with a real tool call, and keep what each one can do."""
    import asyncio

    from metabot.approvals import printable
    from metabot.config import load_config
    from metabot.models import probe_model, save_probe
    from metabot.prices import is_local
    from metabot.roster import listed_models

    cfg = load_config()
    if not is_local(cfg.openai_base_url):
        print(f"models   {cfg.openai_base_url} is remote; not probed: a probe there costs money")
        return 0
    names = names or listed_models(cfg.openai_base_url)
    if not names:
        print(f"models   {cfg.openai_base_url} lists no models, or does not answer")
        return 0
    print(f"models   probing {len(names)} on {cfg.openai_base_url}, on this machine")
    for name in names:
        probe = asyncio.run(probe_model(cfg.openai_base_url, name, key_env=cfg.openai_key_env))
        if probe.mode != "unchecked":
            save_probe(cfg.openai_base_url, name, probe.mode, probe.detail)
        print(f"model    {name}  {probe.mode}: {printable(probe.detail, 200)}", flush=True)
    return 0


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


def _dollars(value: str) -> int:
    """``--max-cost``: dollars, as whole micro-dollars; never rounded up past what was given."""
    from decimal import Decimal, InvalidOperation

    from metabot.spend import MUSD

    try:
        amount = Decimal(value)
    except InvalidOperation:
        amount = Decimal("NaN")
    if not amount.is_finite() or int(amount * MUSD) < 1:
        raise argparse.ArgumentTypeError(f"{value[:60]!r} is not an amount of dollars")
    return int(amount * MUSD)


def _run_options(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "-e", "--engine", help="a built-in engine or a bridge (default: metabot engines)"
    )
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


def _workflow_parser(add) -> None:
    """``metabot workflow``: files of steps, kept by the sha256 of their bytes."""
    workflow = add("workflow", help="steps that run one after another, kept by their sha256")
    wsub = workflow.add_subparsers(dest="workflow_command", metavar="ACTION", required=True)
    w_new = wsub.add_parser(
        "new", allow_abbrev=False, help="make a workflow of one step from these flags, and keep it"
    )
    w_new.add_argument("name", help="the workflow's name")
    w_new.add_argument(
        "-e", "--engine", help="an ask step's engine: built-in or a bridge (default: here's)"
    )
    w_new.add_argument("-m", "--model", type=_model_name, help="an ask step's model")
    w_new.add_argument(
        "--role", metavar="ROLE", help="an ask step runs on a role from metabot roster"
    )
    w_new.add_argument("--login", metavar="NAME", help="an ask step runs on one of your logins")
    w_new.add_argument("--ladder", action="store_true", help="the step is a ladder")
    w_new.add_argument(
        "--check",
        action="append",
        metavar="CMD",
        help="with --ladder: a command judged on each rung's work (repeatable)",
    )
    w_new.add_argument("--office", metavar="DIR", help="the step is a run of the office in DIR")
    w_new.add_argument("--desks", metavar="A,B,C", help="with --office: the desks, in order")
    w_new.add_argument(
        "--checks", action="store_true", help="with --office: run the office's gate checks"
    )
    w_new.add_argument(
        "--until-checks",
        action="store_true",
        help="with --office: run a desk again while its checks fail",
    )
    w_new.add_argument(
        "--rounds", type=int, metavar="N", help="with --until-checks: the most rounds (1-10)"
    )
    w_new.add_argument("--fan", action="store_true", help="with --office: the desks run at once")
    w_new.add_argument(
        "--no-advisors", action="store_true", help="with --office: leave out the advisors"
    )
    w_new.add_argument(
        "--read-only", action="store_true", help="the step reads and changes nothing"
    )
    w_new.add_argument(
        "--allow-write", action="store_true", help="writes run without asking (never destructive)"
    )
    w_new.add_argument(
        "--allow", action="append", default=[], metavar="TOOL", help="a write tool that runs "
        "without asking (repeatable)",
    )  # fmt: skip
    w_new.add_argument(
        "--max-cost", metavar="DOLLARS", help="end the step's run once it has cost this much"
    )
    w_new.add_argument(
        "--no-connections", action="store_true", help="leave the connections out of the step"
    )
    w_new.add_argument(
        "--no-plugins", action="store_true", help="leave the enabled plugins out of the step"
    )
    w_new.add_argument("--cwd", help="the folder it works in (default: here)")
    w_new.add_argument(
        "--cron",
        metavar="'M H DOM MON DOW'",
        help="when it is due; checked and kept, and nothing fires it yet",
    )
    w_new.add_argument("--tz", metavar="ZONE", help="the cron's zone (default: this machine's)")
    w_new.add_argument("--firings", type=int, metavar="N", help="ceiling: the most times it fires")
    w_new.add_argument("--expires", metavar="YYYY-MM-DD", help="ceiling: the last day it fires")
    w_new.add_argument(
        "--month-cap", metavar="DOLLARS", help="ceiling: the most it spends in a month"
    )
    w_new.add_argument("prompt", nargs="+", help="the step's prompt; '-' reads it from stdin")
    w_add = wsub.add_parser(
        "add", allow_abbrev=False, help="keep a workflow file as written, by its sha256"
    )
    w_add.add_argument("file", metavar="FILE")
    w_check = wsub.add_parser(
        "check", allow_abbrev=False, help="whether a file or a kept workflow reads and runs here"
    )
    w_check.add_argument("ref", metavar="FILE_OR_NAME", help="a file, a name or a sha256")
    w_show = wsub.add_parser(
        "show", allow_abbrev=False, help="what a workflow runs: each step, its checks and limits"
    )
    w_show.add_argument("ref", metavar="NAME_OR_SHA", help="a name or a sha256 (12 digits or more)")
    w_show.add_argument("--raw", action="store_true", help="print its exact bytes")
    w_list = wsub.add_parser("list", allow_abbrev=False, help="every workflow kept, each version")
    w_approve = wsub.add_parser(
        "approve",
        allow_abbrev=False,
        help="sign your approval of a workflow's bytes; asks at a terminal",
    )
    w_approve.add_argument("--yes", action="store_true", help="at a terminal: do not ask")
    w_revoke = wsub.add_parser(
        "revoke", allow_abbrev=False, help="sign the end of a workflow's approval"
    )
    w_revoke.add_argument("--yes", action="store_true", help="do not ask to confirm")
    w_run = wsub.add_parser(
        "run", allow_abbrev=False, help="run an approved workflow's steps, then seal them"
    )
    w_run.add_argument("-q", "--quiet", action="store_true", help="no trace on stderr")
    w_schedule = wsub.add_parser(
        "schedule",
        allow_abbrev=False,
        help="a routine that fires an approved workflow on its schedule, under its ceiling",
    )
    w_schedule.add_argument(
        "--name", help="the routine's name (default: the workflow's); save it again to change it"
    )
    w_schedule.add_argument("--off", action="store_true", help="save it switched off")
    for parser_ in (w_approve, w_revoke, w_run, w_schedule):
        parser_.add_argument(
            "ref", metavar="NAME_OR_SHA", help="a name (every version, for revoke) or a sha256"
        )
    for action, parser_ in (
        ("new", w_new), ("add", w_add), ("check", w_check), ("show", w_show), ("list", w_list),
        ("approve", w_approve), ("revoke", w_revoke), ("run", w_run), ("schedule", w_schedule),
    ):  # fmt: skip
        parser_.set_defaults(func=_lazy("metabot.workflow_commands", f"cmd_workflow_{action}"))


def command_name() -> str:
    """The name this was started by: ``metabot`` stays an alias of ``metaorcha``."""
    typed = os.path.basename(sys.argv[0]) if sys.argv else ""
    return typed if typed in ("metaorcha", "metabot") else "metaorcha"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=command_name(),
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

    eng = add("engines", help="which engines are here")
    eng.add_argument(
        "--manifest",
        action="store_true",
        help="with each engine: how it reads without writing, its models, the sandbox here, "
        "and what still reaches it",
    )
    eng.add_argument("--cwd", help="the directory the sandbox line is for (default: here)")
    eng.set_defaults(func=_lazy("metabot.commands", "cmd_engines"))

    ask = add("ask", help="run one prompt and print the answer")
    _run_options(ask)
    ask.add_argument("--resume", metavar="SESSION", help="continue an engine session")
    ask.add_argument(
        "--login",
        metavar="NAME",
        help="run on one of your own logins (metabot login list); default: this sign-in",
    )
    ask.add_argument(
        "--max-cost",
        type=_dollars,
        metavar="DOLLARS",
        help=(
            "end the run once it has cost this much; a run on a model MetaBot cannot price "
            "is refused, or ends at its next tool call when its engine names the model as it starts"
        ),
    )
    ask.add_argument(
        "--ladder",
        action="store_true",
        help="run on the cheapest rung of metabot ladder first; climb only on a recorded failure",
    )
    ask.add_argument(
        "--check",
        action="append",
        metavar="CMD",
        help="with --ladder: a command judged on each rung's work; a failing one climbs "
        "(repeatable)",
    )
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
    c_set.add_argument("key", help="project_receipts, fan_cap, ladder_attempts or sandbox")
    c_set.add_argument("value")
    c_set.set_defaults(func=_lazy("metabot.commands", "cmd_config_set"))

    add("ladder", help="the rungs ask --ladder climbs, cheapest first").set_defaults(
        func=_lazy("metabot.ladder", "cmd_ladder")
    )

    sug = add("suggest", help="which role, engine and model suit a task, and the rule that says so")
    sug.add_argument("task", nargs="+", help="the task; '-' reads it from stdin")
    sug.set_defaults(func=_lazy("metabot.suggest", "cmd_suggest"))

    prop = add(
        "propose",
        help="draft a workflow for a task by rules: one ask, a ladder or an office run; "
        "it runs, approves and keeps nothing",
    )
    prop.add_argument("--folder", metavar="DIR", help="the folder it works in (default: here)")
    prop.add_argument(
        "--out", metavar="FILE", help="write the draft to FILE, never over a file that is there"
    )
    prop.add_argument("--json", action="store_true", help="print the proposal as one JSON object")
    prop.add_argument("task", nargs="+", help="the task; '-' reads it from stdin")
    prop.set_defaults(func=_lazy("metabot.propose", "cmd_propose"))

    chat = add("chat", help="talk to the bot in this terminal (also: metabot with no command)")
    _run_options(chat)
    chat.add_argument(
        "--login",
        metavar="NAME",
        help="every turn runs on one of your own logins (metabot login list)",
    )
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
        "--until-checks",
        action="store_true",
        help="run the checks, and while a desk's checks fail, run that desk again with what "
        "they said, up to --rounds times",
    )
    o_run.add_argument(
        "--rounds",
        type=int,
        metavar="N",
        help="with --until-checks, the most rounds a desk runs (default 3, at most 10)",
    )
    o_run.add_argument(
        "--max-cost",
        type=_dollars,
        metavar="DOLLARS",
        help="one cap over every desk run and round; a desk MetaBot cannot price never starts",
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
    bridges = p_enable.add_mutually_exclusive_group()
    bridges.add_argument(
        "--allow-bridges",
        action="store_true",
        help="make its bridges engines; they run outside the gate",
    )
    bridges.add_argument("--without-bridges", action="store_true", help="leave its bridges out")
    p_new = psub.add_parser("new", allow_abbrev=False, help="write a plugin to start from")
    p_new.add_argument("name", help="the plugin's name")
    p_new.add_argument(
        "--bridge", action="store_true", help="a bridge, an engine of your own; else a skill"
    )
    p_new.add_argument("--dir", help="the folder to write it in (default: ./NAME)")
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
        ("new", p_new),
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
    rt_save.add_argument(
        "-e", "--engine", help="a built-in engine or a bridge (default: metabot engines)"
    )
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
    rt_show = rtsub.add_parser(
        "show", allow_abbrev=False, help="one routine: what it fires, its approval and ceiling"
    )
    rt_run = rtsub.add_parser("run", allow_abbrev=False, help="fire a routine now, as serve would")
    rt_run.add_argument("name")
    rt_run.add_argument("-q", "--quiet", action="store_true", help="no trace on stderr")
    rt_enable = rtsub.add_parser("enable", allow_abbrev=False, help="switch a routine on")
    rt_disable = rtsub.add_parser("disable", allow_abbrev=False, help="switch a routine off")
    rt_remove = rtsub.add_parser(
        "remove", allow_abbrev=False, help="forget a routine; its firings and receipts stay"
    )
    rt_remove.add_argument("--yes", action="store_true", help="do not ask to confirm")
    for parser_ in (rt_show, rt_enable, rt_disable, rt_remove):
        parser_.add_argument("name")
    rt_firings = rtsub.add_parser("firings", allow_abbrev=False, help="what each firing did")
    rt_firings.add_argument("name", nargs="?", help="one routine (default: all)")
    rt_firings.add_argument("-n", "--limit", type=int, default=20)
    for action, parser_ in (
        ("save", rt_save),
        ("list", rt_list),
        ("show", rt_show),
        ("run", rt_run),
        ("enable", rt_enable),
        ("disable", rt_disable),
        ("remove", rt_remove),
        ("firings", rt_firings),
    ):
        parser_.set_defaults(func=_lazy("metabot.routine_commands", f"cmd_routine_{action}"))

    _workflow_parser(add)

    roster = add("roster", help="each role's engine and model; yours win over the defaults")
    roster.set_defaults(func=_lazy("metabot.roster", "cmd_roster"))
    ro_sub = roster.add_subparsers(dest="roster_command", metavar="ACTION")
    ro_set = ro_sub.add_parser("set", allow_abbrev=False, help="give a role an engine and model")
    ro_set.add_argument("role", help="fast, build, review, second or private")
    ro_set.add_argument("--engine", required=True, help=f"one of {', '.join(ENGINES)}, or a bridge")
    ro_set.add_argument("--model", help="the engine's model (default: the engine's own)")
    ro_set.add_argument(
        "--login",
        metavar="NAME",
        help="run the role on one of your own logins (metabot login list)",
    )
    ro_set.set_defaults(func=_lazy("metabot.roster", "cmd_roster_set"))
    ro_unset = ro_sub.add_parser("unset", allow_abbrev=False, help="give a role back its default")
    ro_unset.add_argument("role")
    ro_unset.set_defaults(func=_lazy("metabot.roster", "cmd_roster_unset"))

    login = add("login", help="your own claude and codex sign-in folders, by name")
    lsub = login.add_subparsers(dest="login_command", metavar="ACTION", required=True)
    l_add = lsub.add_parser("add", allow_abbrev=False, help="name a sign-in folder")
    l_add.add_argument("name", help="a short lower-case word, such as work")
    l_add.add_argument("--engine", required=True, help="claude or codex")
    l_add.add_argument(
        "--home", required=True, metavar="FOLDER", help="the sign-in folder's absolute path"
    )
    l_add.add_argument(
        "--max-at-once",
        type=int,
        default=1,
        metavar="N",
        help="runs on this login at once, 1 to 5 (default 1)",
    )
    l_add.add_argument(
        "--on-sign-in",
        choices=("refuse", "clear"),
        default="refuse",
        help="when an API key or another sign-in is set here: refuse the run (default), "
        "or clear it for the run",
    )
    l_set = lsub.add_parser("set", allow_abbrev=False, help="change a login's settings")
    l_set.add_argument("name")
    l_set.add_argument(
        "--on-sign-in",
        choices=("refuse", "clear"),
        help="when an API key or another sign-in is set here: refuse the run, or clear it",
    )
    l_set.add_argument("--max-at-once", type=int, metavar="N", help="runs on it at once, 1 to 5")
    l_list = lsub.add_parser("list", allow_abbrev=False, help="each login and its folder")
    l_check = lsub.add_parser(
        "check", allow_abbrev=False, help="whether each folder is ready; nothing in it is opened"
    )
    l_check.add_argument("name", nargs="?")
    l_remove = lsub.add_parser(
        "remove", allow_abbrev=False, help="forget a login; its folder is left as it is"
    )
    l_remove.add_argument("name")
    for action, parser_ in (
        ("add", l_add),
        ("set", l_set),
        ("list", l_list),
        ("check", l_check),
        ("remove", l_remove),
    ):
        parser_.set_defaults(func=_lazy("metabot.logins", f"cmd_login_{action}"))

    add("serve", help="fire routines as they come due, one at a time").set_defaults(
        func=_lazy("metabot.serve", "cmd_serve")
    )

    ui = add("ui", help="a local web page for runs, offices, connections and routines")
    ui.add_argument(
        "--port", type=int, default=0, help="the port on 127.0.0.1 (default: a free one)"
    )
    ui.set_defaults(func=_lazy("metabot.web_commands", "cmd_ui"))

    app = add("app", help="the web UI in a window of its own")
    app.add_argument(
        "--port", type=int, default=0, help="the port on 127.0.0.1 (default: a free one)"
    )
    app.add_argument(
        "--install",
        action="store_true",
        help="add MetaBot to your applications menu, to open this window",
    )
    app.set_defaults(func=_lazy("metabot.web_window", "cmd_app"))

    cost = add("cost", help="what past runs spent, by engine and model")
    cost.add_argument("--json", action="store_true", help="print the totals as one JSON object")
    cost.set_defaults(func=_lazy("metabot.commands", "cmd_cost"))

    status = add("status", help="what runs now: runs, office runs, ladders, routines and serve")
    status.add_argument("--json", action="store_true", help="print it as one JSON object")
    status.set_defaults(func=_lazy("metabot.live", "cmd_status"))

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
    r_verify.add_argument(
        "--chain",
        action="store_true",
        help="RUN_OR_FILE is a bundle that receipts export wrote, a folder or a tarball: check "
        "each receipt in it, its seal's links to its members, its journals, and a workflow's "
        "file and approval",
    )
    r_verify.set_defaults(func=_lazy("metabot.commands", "cmd_receipts_verify"))
    r_export = rsub.add_parser(
        "export",
        allow_abbrev=False,
        help="write a run, an office run, a ladder, a workflow run or a firing to one bundle "
        "anyone can check offline",
    )
    r_export.add_argument(
        "run",
        metavar="RUN",
        help="the id of a run, an office run, a ladder, a workflow run or a firing",
    )
    r_export.add_argument(
        "--out",
        metavar="DIR|FILE.tar.gz",
        help="a folder or a tarball to write, never over one that is there "
        "(default: ./RUN.bundle.tar.gz)",
    )
    r_export.add_argument(
        "--journals",
        action="store_true",
        help="put in the journals too: the prompts, answers and tool calls as kept "
        "(default: the receipts alone, which hold hashes and no text)",
    )
    r_export.set_defaults(func=_lazy("metabot.bundles", "cmd_receipts_export"))

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
    doctor.add_argument(
        "--models",
        nargs="*",
        metavar="MODEL",
        help="instead, check whether each local model can call MetaBot's tools, and how "
        "(default: every model the local server lists)",
    )
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
    if isinstance(sys.stdout, io.TextIOWrapper) and not sys.stdout.closed:
        # a name whose bytes are not UTF-8, as a folder's can be, prints escaped, as on
        # stderr, and never as a traceback
        sys.stdout.reconfigure(errors="backslashreplace")
    stdin = sys.stdin
    if isinstance(stdin, io.TextIOWrapper) and not stdin.closed and stdin.errors == "strict":
        # such a name's bytes are read as an argument's are: a prompt or a note piped in
        # holds it, and never stops with a traceback
        stdin.reconfigure(errors="surrogateescape")
    words = list(argv) if argv is not None else sys.argv[1:]
    if _token_flag_given(words):
        print(TOKEN_FLAG_REFUSED, file=sys.stderr)
        return 2
    parser = build_parser()
    args = parser.parse_args(words)
    if args.version:
        print(f"{command_name()} {__version__}")
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
