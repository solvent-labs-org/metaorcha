"""catalog.py — the command catalogue and the module manifests (contract 4.9, 4.12).

Every write a module offers is a catalogue entry: an id, an argv template, typed
params with validators, and a kind. ``read`` entries run captured; ``desk``
entries open in a new terminal desk where the person answers any prompt.
Modules are data (``modules/*.json``, then ``<state>/modules/*.json``) and may
only point at catalogue ids that exist.

Placeholders are whole argv elements: ``{metabot}`` and ``{orcha}`` expand to
the configured binaries, ``{name}`` to a validated param. A placeholder inside a
longer string is a template error, caught by ``check_templates()``.

Templates that differ from the contract's sketch, adjusted to MetaBot 0.3.0's
real arguments (ids unchanged):
- ``plugin.import-skills`` needs ``--name``: params ``name:id`` + ``folder:path``.
- ``routine.save`` has no file form in 0.3.0: params ``name:slug``,
  ``cron:text``, ``goal:text`` -> ``routine save --cron <cron> <name> <goal>``.
- ``roster.show`` is the bare ``metabot roster`` (it has no ``show`` action).
- ``desks.pick`` takes ``RUN/DESK`` (a slash), which the ``id`` type refuses, so
  its ``name`` param is ``text``; the argv is still one element.
- ``office.run`` is ``office run --dir <folder> <task>``; ``login.add`` is
  ``login add --engine <e> --home <h> <name>``; ``connect.add`` is
  ``connect add --url <url> <name>`` (the token is asked for in the desk).
- ``sdk.init`` takes ``name:slug`` (``orcha init --help`` shows a required
  positional ``name``; the contract lists it with no params). The slug type
  refuses names with spaces or capitals that ``orcha init`` itself accepts.
- ``status`` and ``cost`` run with ``--json`` (0.3.0 has the flag; the
  contract's sketch shows the bare commands) so the dock gets one object.
- ``desks.list`` / ``desks.pick`` / ``desks.clean`` and ``office.check`` /
  ``office.trust`` take ``folder:path`` -> ``--dir <folder>``: reads run in
  the home folder, which is never a repository or an office, so without the
  flag they always failed from the tablet (review server-flows-6). ``plugin.new``
  and ``sdk.init`` write into the desk's ``cwd`` (the UI sends the project
  folder as ``cwd`` on ``/api/cli/desk``); ``/api/cli/read`` takes ``cwd`` too.
- ``login.add``'s ``home`` is type ``account_home``: a slug that expands to
  ``<state_dir>/accounts/<engine>/<slug>`` (made 0700 on the way). A ``path``
  must already exist under a project root, which no sign-in folder does
  (review server-flows-8).
- ``workflow.new`` (21.3): ``workflow new -e <engine> --cwd <folder> <name>
  <prompt>``, params ``name:id engine:slug folder:path prompt:text``.
- ``sdk.verify`` takes ``run:receipt_path``: the same safe id and containment
  check as ``receipt``, but its argv text is the resolved
  ``<metabot_home>/receipts/<id>.json`` because ``orcha verify`` wants the
  file, not the run id (review SEC-6). The ``metabot receipts`` subcommands
  keep the plain ``receipt`` type and get the id.

25.3 B2 / B7 (MetaBot 0.4.0, read from its ``cli.py``): ``login.set``
``login set --max-at-once N NAME``; reads ``propose`` (``--json --folder DIR
TASK``: rules only, no model call), ``suggest``, ``ladder``, ``engines
--manifest``; desks ``plugin new --bridge``, ``plugin marketplace
add|remove``, ``plugin enable`` (every enable entry says ``--allow-*`` or
``--without-*`` for both hooks and bridges: MetaBot refuses a plugin that has
either until each is allowed or left out, and takes ``--without-*`` on one that
has neither), ``workflow new --ladder`` / ``--office DIR``, ``config set KEY
VALUE`` (both enums), ``serve`` (one at a time: mb.py refuses a second while
one runs), ``doctor --models`` (it probes each local model with real tool calls
and can outlast the 60 s read timeout). An installed plugin and a marketplace
to remove are named by MetaBot's own rule, so they take the ``id`` type and
MetaBot checks the rest; a new bridge and a new marketplace take the ``slug``
type. ``propose``, ``suggest`` and ``workflow new`` take their
text as a required ``nargs="+"`` positional, so a text that argparse reads as a
flag leaves that positional empty and MetaBot refuses before anything runs.

Section 18 (palette commands from modules): a manifest may carry
``commands: [{slash, label, hint?, args?: [{name, type: desk|text|enum,
values?}], run: {catalog: <id>} | {send: {kind: message|handoff}}}]``. A slash
is a slug, unique within the module, and never one of ``BUILTIN_SLASHES``
(the composer's own commands cannot be overridden). ``catalog`` runs name a
catalogue entry and every arg must be one of its params; ``send`` runs need a
``desk`` arg and a ``text`` arg and go through ``POST /api/send``.
"""
import json
import os
import re
from pathlib import Path

SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{0,23}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
DECIMAL_RE = re.compile(r"^[0-9]{1,6}(\.[0-9]{1,2})?$")
URL_RE = re.compile(r"^https?://[^\s]{1,2000}$")
CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
# half of a UTF-16 pair on its own: JSON's "\ud800" escape makes one, and it can
# never be encoded (an argv, a log line, a file): refused in every string param
SURROGATE_RE = re.compile(r"[\ud800-\udfff]")
PLACEHOLDER_RE = re.compile(r"\{([a-z_]+)\}")
TEXT_LIMIT = 2000
MODULE_PAGES = ("runs", "accounts", "desktop", "sdk", "tools")
MODULE_KINDS = ("page", "catalog", "card")
# section 15/18: the composer's own slash commands; a module may not take them
BUILTIN_SLASHES = frozenset({"handoff", "to", "arm", "start", "rename", "desks", "run",
                             "new", "all", "desktop"})   # 21.3 / 22.5 composer built-ins
MODULE_MODES = ("user", "dev")
COMMAND_ARG_TYPES = ("desk", "text", "enum")
SEND_KINDS = ("message", "handoff")


def is_safe_id(value):
    return isinstance(value, str) and bool(SAFE_ID_RE.fullmatch(value)) and ".." not in value


def _p(name, type_, label, required=True, **extra):
    d = {"name": name, "type": type_, "label": label, "required": required}
    d.update(extra)
    return d


def _e(id_, label, module, kind, argv, params=()):
    return {"id": id_, "label": label, "module": module, "kind": kind,
            "argv": list(argv), "params": list(params)}


M, O = "{metabot}", "{orcha}"
_ID = lambda n, lab: _p(n, "id", lab)      # noqa: E731
_PATH = lambda n, lab: _p(n, "path", lab)  # noqa: E731

ENTRIES = [
    # ---- reads (captured, 60 s, cwd = home)
    _e("status", "Status", "runs", "read", [M, "status", "--json"]),
    _e("cost", "Cost", "runs", "read", [M, "cost", "--json"]),
    _e("doctor", "Doctor", "tools", "read", [M, "doctor"]),
    _e("config.show", "Show config", "tools", "read", [M, "config", "show"]),
    _e("engines.list", "List engines", "runs", "read", [M, "engines"]),
    _e("login.list", "List logins", "accounts", "read", [M, "login", "list"]),
    _e("login.check", "Check logins", "accounts", "read", [M, "login", "check"]),
    _e("plugin.list", "List plugins", "plugins", "read", [M, "plugin", "list"]),
    _e("plugin.show", "Show plugin", "plugins", "read", [M, "plugin", "show", "{name}"],
       [_ID("name", "Plugin name")]),
    _e("plugin.marketplace.list", "List marketplaces", "plugins", "read",
       [M, "plugin", "marketplace", "list"]),
    _e("workflow.list", "List workflows", "workflows", "read", [M, "workflow", "list"]),
    _e("workflow.show", "Show workflow", "workflows", "read", [M, "workflow", "show", "{ref}"],
       [_ID("ref", "Name or sha256")]),
    _e("workflow.check", "Check workflow", "workflows", "read", [M, "workflow", "check", "{ref}"],
       [_ID("ref", "Name or sha256")]),
    _e("connect.list", "List connections", "connections", "read", [M, "connect", "list"]),
    _e("connect.scope", "Connection scope", "connections", "read",
       [M, "connect", "scope", "{name}"], [_ID("name", "Connection name")]),
    _e("connect.test", "Test connection", "connections", "read",
       [M, "connect", "test", "{name}"], [_ID("name", "Connection name")]),
    _e("routine.list", "List routines", "workflows", "read", [M, "routine", "list"]),
    _e("routine.show", "Show routine", "workflows", "read", [M, "routine", "show", "{name}"],
       [_ID("name", "Routine name")]),
    _e("routine.firings", "Routine firings", "workflows", "read",
       [M, "routine", "firings", "{name}"], [_ID("name", "Routine name")]),
    _e("office.check", "Check office", "office", "read", [M, "office", "check", "--dir", "{folder}"],
       [_PATH("folder", "Office folder")]),
    _e("desks.list", "List desks", "office", "read", [M, "desks", "list", "--dir", "{folder}"],
       [_PATH("folder", "Repository folder")]),
    _e("roster.show", "Show roster", "office", "read", [M, "roster"]),
    _e("receipts.list", "List receipts", "runs", "read", [M, "receipts", "list", "-n", "{limit}"],
       [_p("limit", "int", "How many", min=1, max=200)]),
    _e("receipts.show", "Show receipt", "runs", "read", [M, "receipts", "show", "{run}"],
       [_p("run", "receipt", "Run id")]),
    _e("receipts.verify", "Verify receipt", "runs", "read", [M, "receipts", "verify", "{run}"],
       [_p("run", "receipt", "Run id")]),
    _e("sdk.verify", "SDK verify", "sdk", "read", [O, "verify", "--json", "{run}"],
       [_p("run", "receipt_path", "Run id")]),
    # ---- desk commands (a new terminal desk; the person answers prompts there)
    _e("init", "Init MetaBot", "tools", "desk", [M, "init"]),
    _e("chat", "Chat", "runs", "desk", [M, "chat"]),
    _e("plugin.new", "New plugin", "plugins", "desk", [M, "plugin", "new", "{name}"],
       [_ID("name", "Plugin name")]),
    _e("plugin.install", "Install plugin", "plugins", "desk", [M, "plugin", "install", "{ref}"],
       [_p("ref", "text", "NAME@MARKETPLACE")]),
    # MetaBot 0.4.0 refuses to enable a plugin with hooks or bridges until each is
    # allowed or left out, and takes --without-* on one that has none: every enable
    # entry says both, so it works on any plugin (follow-up to 25.3 B7)
    _e("plugin.enable", "Enable", "plugins", "desk",
       [M, "plugin", "enable", "--without-hooks", "--without-bridges", "{name}"],
       [_ID("name", "Plugin name")]),
    _e("plugin.disable", "Disable plugin", "plugins", "desk", [M, "plugin", "disable", "{name}"],
       [_ID("name", "Plugin name")]),
    _e("plugin.remove", "Remove plugin", "plugins", "desk", [M, "plugin", "remove", "{name}"],
       [_ID("name", "Plugin name")]),
    _e("plugin.import-skills", "Import skills", "plugins", "desk",
       [M, "plugin", "import-skills", "--name", "{name}", "{folder}"],
       [_ID("name", "Plugin name"), _PATH("folder", "Skills folder")]),
    _e("workflow.new", "New workflow", "workflows", "desk",
       [M, "workflow", "new", "-e", "{engine}", "--cwd", "{folder}", "{name}", "{prompt}"],
       [_ID("name", "Workflow name"), _p("engine", "slug", "Engine"),
        _PATH("folder", "Folder it works in"), _p("prompt", "text", "Prompt")]),
    _e("workflow.add", "Add workflow", "workflows", "desk", [M, "workflow", "add", "{file}"],
       [_PATH("file", "Workflow file")]),
    _e("workflow.approve", "Approve workflow", "workflows", "desk",
       [M, "workflow", "approve", "{ref}"], [_ID("ref", "Name or sha256")]),
    _e("workflow.revoke", "Revoke workflow", "workflows", "desk",
       [M, "workflow", "revoke", "{ref}"], [_ID("ref", "Name or sha256")]),
    _e("workflow.run", "Run workflow", "workflows", "desk", [M, "workflow", "run", "{ref}"],
       [_ID("ref", "Name or sha256")]),
    _e("workflow.schedule", "Schedule workflow", "workflows", "desk",
       [M, "workflow", "schedule", "{ref}"], [_ID("ref", "Name or sha256")]),
    _e("connect.add", "Add connection", "connections", "desk",
       [M, "connect", "add", "--url", "{url}", "{name}"],
       [_p("name", "slug", "Connection name"), _p("url", "url", "MCP server URL")]),
    _e("connect.revoke", "Revoke connection", "connections", "desk",
       [M, "connect", "revoke", "{name}"], [_p("name", "slug", "Connection name")]),
    _e("routine.save", "Save routine", "workflows", "desk",
       [M, "routine", "save", "--cron", "{cron}", "{name}", "{goal}"],
       [_p("name", "slug", "Routine name"), _p("cron", "text", "Cron (M H DOM MON DOW)"),
        _p("goal", "text", "Goal")]),
    _e("routine.run", "Run routine", "workflows", "desk", [M, "routine", "run", "{name}"],
       [_ID("name", "Routine name")]),
    _e("routine.enable", "Enable routine", "workflows", "desk",
       [M, "routine", "enable", "{name}"], [_ID("name", "Routine name")]),
    _e("routine.disable", "Disable routine", "workflows", "desk",
       [M, "routine", "disable", "{name}"], [_ID("name", "Routine name")]),
    _e("routine.remove", "Remove routine", "workflows", "desk",
       [M, "routine", "remove", "{name}"], [_ID("name", "Routine name")]),
    _e("office.trust", "Trust office checks", "office", "desk",
       [M, "office", "trust", "--dir", "{folder}"], [_PATH("folder", "Office folder")]),
    _e("office.run", "Run office", "office", "desk",
       [M, "office", "run", "--dir", "{folder}", "{task}"],
       [_p("task", "text", "Task"), _PATH("folder", "Office folder")]),
    _e("login.add", "Add login", "accounts", "desk",
       [M, "login", "add", "--engine", "{engine}", "--home", "{home}", "{name}"],
       [_p("name", "slug", "Login name"),
        _p("engine", "enum", "Engine", values=["claude", "codex"]),
        _p("home", "account_home", "Sign-in folder name")]),
    _e("login.remove", "Remove login", "accounts", "desk", [M, "login", "remove", "{name}"],
       [_p("name", "slug", "Login name")]),
    _e("login.set", "Runs at once", "accounts", "desk",                 # 25.3 B2
       [M, "login", "set", "--max-at-once", "{n}", "{name}"],
       [_p("name", "slug", "Login name"), _p("n", "int", "Runs at once (1-5)", min=1, max=5)]),
    _e("roster.set", "Set role", "office", "desk",
       [M, "roster", "set", "--engine", "{engine}", "--model", "{model}", "{role}"],
       [_p("role", "slug", "Role"), _p("engine", "slug", "Engine"), _p("model", "text", "Model")]),
    _e("roster.unset", "Unset role", "office", "desk", [M, "roster", "unset", "{role}"],
       [_p("role", "slug", "Role")]),
    _e("receipts.export", "Export receipt", "runs", "desk", [M, "receipts", "export", "{run}"],
       [_p("run", "receipt", "Run id")]),
    _e("desks.pick", "Pick desk work", "office", "desk",
       [M, "desks", "pick", "--dir", "{folder}", "{name}"],
       [_p("name", "text", "RUN/DESK"), _PATH("folder", "Repository folder")]),
    _e("desks.clean", "Clean run desks", "office", "desk",
       [M, "desks", "clean", "--dir", "{folder}", "{run}"],
       [_ID("run", "Run id"), _PATH("folder", "Repository folder")]),
    _e("sdk.init", "SDK init", "sdk", "desk", [O, "init", "{name}"],
       [_p("name", "slug", "Agent name")]),
    _e("sdk.record", "SDK record seal", "sdk", "desk", [O, "record", "seal", "{file}"],
       [_PATH("file", "run.json")]),
    # ---- 25.3 B7: reads
    _e("propose", "Propose a workflow", "workflows", "read",
       [M, "propose", "--json", "--folder", "{folder}", "{task}"],
       [_PATH("folder", "Folder it works in"), _p("task", "text", "Task")]),
    _e("suggest", "Suggest a role", "runs", "read", [M, "suggest", "{task}"], [_p("task", "text", "Task")]),
    _e("ladder", "Show the ladder", "runs", "read", [M, "ladder"]),
    _e("engines.manifest", "Engines in detail", "runs", "read", [M, "engines", "--manifest"]),
    # ---- 25.3 B7: desks
    _e("plugin.new-bridge", "New bridge", "plugins", "desk", [M, "plugin", "new", "--bridge", "{name}"],
       [_p("name", "slug", "Bridge name")]),
    _e("plugin.marketplace.add", "Add a marketplace", "plugins", "desk",
       [M, "plugin", "marketplace", "add", "{name}", "{folder}"],
       [_p("name", "slug", "Marketplace name"), _PATH("folder", "Marketplace folder")]),
    # an installed plugin or a marketplace is named by MetaBot's own rule (plugins.NAME:
    # lower-case, digits, '.', '-', '_'), so these take the id type, as plugin.show does,
    # and MetaBot checks the rest; a new bridge and a new marketplace keep the slug
    _e("plugin.marketplace.remove", "Remove a marketplace", "plugins", "desk",
       [M, "plugin", "marketplace", "remove", "{name}"], [_ID("name", "Marketplace name")]),
    _e("plugin.enable-bridges", "Enable as an engine", "plugins", "desk",
       [M, "plugin", "enable", "--allow-bridges", "--without-hooks", "{name}"], [_ID("name", "Plugin name")]),
    _e("plugin.enable-servers", "Enable with its servers", "plugins", "desk",
       [M, "plugin", "enable", "--add-servers", "--without-hooks", "--without-bridges", "{name}"],
       [_ID("name", "Plugin name")]),
    _e("plugin.enable-hooks", "Enable with hooks (developer)", "plugins", "desk",
       [M, "plugin", "enable", "--allow-hooks", "--without-bridges", "{name}"], [_ID("name", "Plugin name")]),
    _e("workflow.new-ladder", "New ladder workflow", "workflows", "desk",
       [M, "workflow", "new", "--ladder", "--cwd", "{folder}", "{name}", "{prompt}"],
       [_ID("name", "Workflow name"), _PATH("folder", "Folder it works in"), _p("prompt", "text", "Prompt")]),
    _e("workflow.new-office", "New office workflow", "workflows", "desk",
       [M, "workflow", "new", "--office", "{folder}", "--cwd", "{folder}", "{name}", "{prompt}"],
       [_ID("name", "Workflow name"), _PATH("folder", "Office folder"), _p("prompt", "text", "Prompt")]),
    _e("config.set", "Change a setting", "tools", "desk", [M, "config", "set", "{key}", "{value}"],
       [_p("key", "enum", "Setting", values=["project_receipts", "fan_cap", "ladder_attempts", "sandbox"]),
        _p("value", "enum", "Value", values=["true", "false", "1", "2", "3", "4", "5"])]),
    _e("serve", "Start serve", "workflows", "desk", [M, "serve"]),
    # follow-up E: probing the local models can outlast the 60 s read cap, so a desk
    _e("doctor.models", "Check local models", "tools", "desk", [M, "doctor", "--models"]),
]

BY_ID = {e["id"]: e for e in ENTRIES}
assert len(BY_ID) == len(ENTRIES), "duplicate catalogue id"


def check_templates(entries=ENTRIES):
    """Template errors: a placeholder inside a longer string, or one that names
    no param. Returns a list of messages; empty when every template is sound."""
    errors = []
    for e in entries:
        names = {p["name"] for p in e["params"]}
        for el in e["argv"]:
            found = PLACEHOLDER_RE.findall(el)
            if not found:
                continue
            if el != "{" + found[0] + "}" or len(found) != 1:
                errors.append(f"{e['id']}: placeholder inside a longer string: {el!r}")
            elif found[0] not in names and found[0] not in ("metabot", "orcha"):
                errors.append(f"{e['id']}: placeholder names no param: {el!r}")
        for p in e["params"]:
            if "{" + p["name"] + "}" not in e["argv"]:
                errors.append(f"{e['id']}: param {p['name']} unused")
    return errors


def listing():
    return {"entries": [dict(e, argv=list(e["argv"]), params=[dict(p) for p in e["params"]])
                        for e in ENTRIES]}


# ---------------------------------------------------------------- validators

def home_denylist(state_dir):
    """Folders a tablet-chosen path may never be in or under (resolved form).
    The home folder itself is denied separately, by exact match only: every
    project on this machine lives under it (section 2, review F1)."""
    home = Path.home()
    return [home / ".ssh", home / ".orcha", home / ".metabot", Path(state_dir) / "accounts"]


def path_ok(raw, ctx):
    """Section 2: absolute, resolved, under a project root, outside the denylist.
    Returns the resolved path string or None.

    Denied: the home folder ITSELF; anything in or under ~/.ssh, ~/.orcha,
    ~/.metabot, any ~/.claude* and <state_dir>/accounts. Everything else under
    a configured project root is allowed, including ~/proj when ~ is a root."""
    if not isinstance(raw, str) or not raw or not os.path.isabs(raw) or "\x00" in raw:
        return None
    try:
        p = Path(raw).resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return None
    home = Path.home().resolve()
    if p == home:
        return None
    for d in home_denylist(ctx.get("state_dir", "")):
        try:
            d = d.resolve()
        except (OSError, RuntimeError):
            continue
        if p == d or d in p.parents:
            return None
    for par in [p, *p.parents]:   # ~/.claude, ~/.claude-work, ... and anything under them
        if par.parent == home and par.name.startswith(".claude"):
            return None
    roots = []
    # ``also_ok``: folders of MetaChat's own a desk may open in (25.3 B8: the user modules folder)
    for r in [*(ctx.get("project_roots") or []), *(ctx.get("also_ok") or [])]:
        try:
            roots.append(Path(r).expanduser().resolve())
        except (OSError, RuntimeError):
            continue
    if not any(p == r or r in p.parents for r in roots):
        return None
    return str(p)


def receipt_ok(raw, ctx):
    """A safe id whose receipt file sits inside <metabot_home>/receipts."""
    if not is_safe_id(raw):
        return None
    folder = Path(ctx["metabot_home"]) / "receipts"
    try:
        f = (folder / f"{raw}.json").resolve()
        folder_r = folder.resolve()
    except (OSError, RuntimeError):
        return None
    if folder_r not in f.parents or not f.is_file():
        return None
    return str(f)


def account_home(slug, engine, ctx):
    """``<state_dir>/accounts/<engine>/<slug>``, made 0700 when missing (the
    same folder POST /api/accounts makes). Returns (path, None) or (None, error)."""
    if not SLUG_RE.fullmatch(engine or ""):
        return None, "bad engine"
    folder = Path(str(ctx.get("state_dir") or "")) / "accounts" / engine / slug
    try:
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(folder, 0o700)
    except OSError as exc:
        return None, f"cannot make the folder: {type(exc).__name__}"
    return str(folder), None


def validate_param(spec, value, ctx):
    """One param: returns (ok, argv_text_or_error)."""
    t = spec["type"]
    if t == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            if isinstance(value, str) and value.isdigit():
                value = int(value)
            else:
                return False, "not an int"
        lo, hi = spec.get("min"), spec.get("max")
        if (lo is not None and value < lo) or (hi is not None and value > hi):
            return False, "out of range"
        return True, str(value)
    if not isinstance(value, str):
        return False, "not a string"
    if SURROGATE_RE.search(value):
        return False, "a lone surrogate"
    if t == "id":
        return (True, value) if is_safe_id(value) else (False, "not a safe id")
    if t == "slug":
        return (True, value) if SLUG_RE.fullmatch(value) else (False, "not a slug")
    if t == "sha256":
        return (True, value) if SHA256_RE.fullmatch(value) else (False, "not a sha256")
    if t == "decimal":
        return (True, value) if DECIMAL_RE.fullmatch(value) else (False, "not a decimal")
    if t == "path":
        p = path_ok(value, ctx)
        return (True, p) if p else (False, "bad path")
    if t == "receipt":
        return (True, value) if receipt_ok(value, ctx) else (False, "no such receipt")
    if t == "receipt_path":   # the same check; the argv text is the file (orcha verify)
        f = receipt_ok(value, ctx)
        return (True, f) if f else (False, "no such receipt")
    if t == "account_home":   # a folder name under <state>/accounts/<engine>/; expand() makes the path
        if not SLUG_RE.fullmatch(value) or value == "default":
            return False, "not a slug"
        return True, value
    if t == "text":
        if not (1 <= len(value) <= TEXT_LIMIT) or CTRL_RE.search(value):
            return False, "bad text"
        return True, value
    if t == "url":
        return (True, value) if URL_RE.fullmatch(value) else (False, "not a url")
    if t == "enum":
        return (True, value) if value in spec.get("values", []) else (False, "not a choice")
    return False, f"unknown type {t}"


def expand(entry_id, params, ctx):
    """Catalogue id + raw params -> (argv, None) or (None, error string).
    ``ctx`` carries metabot, orcha, metabot_home, project_roots, state_dir."""
    e = BY_ID.get(entry_id)
    if e is None:
        return None, "unknown id"
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return None, "bad params"
    values = {}
    for spec in e["params"]:
        if spec["name"] not in params or params[spec["name"]] in ("", None):
            if spec.get("required", True):
                return None, f"missing {spec['name']}"
            continue
        ok, text = validate_param(spec, params[spec["name"]], ctx)
        if not ok:
            return None, f"bad {spec['name']}: {text}"
        values[spec["name"]] = text
    for spec in e["params"]:
        if spec["type"] == "account_home" and spec["name"] in values:
            path, err = account_home(values[spec["name"]], values.get("engine") or "claude", ctx)
            if err:
                return None, f"bad {spec['name']}: {err}"
            values[spec["name"]] = path
    argv = []
    for el in e["argv"]:
        if el == "{metabot}":
            argv.append(str(ctx["metabot"]))
        elif el == "{orcha}":
            argv.append(str(ctx["orcha"]))
        elif PLACEHOLDER_RE.fullmatch(el):
            argv.append(values[el[1:-1]])
        else:
            argv.append(el)
    return argv, None


# ---------------------------------------------------------------- modules

def _check_manifest(m):
    """Return an error string, or None when the manifest fits the schema."""
    if not isinstance(m, dict):
        return "not an object"
    mid = m.get("id")
    if not isinstance(mid, str) or not SLUG_RE.fullmatch(mid):
        return "bad id"
    label = m.get("label")
    if not isinstance(label, str) or not (1 <= len(label) <= 32):
        return "bad label"
    icon = m.get("icon")
    if not isinstance(icon, str) or len(icon) != 1:
        return "bad icon"
    if isinstance(m.get("order"), bool) or not isinstance(m.get("order"), int):
        return "bad order"
    kind = m.get("kind")
    if kind not in MODULE_KINDS:
        return "bad kind"
    if m.get("phase") not in (1, 2):
        return "bad phase"
    if "modes" in m:   # 21.4: which UI modes list the module; absent = both
        modes = m["modes"]
        if (not isinstance(modes, list) or not modes or len(set(modes)) != len(modes)
                or not all(x in MODULE_MODES for x in modes)):
            return "bad modes"
    if kind == "page":
        if m.get("page") not in MODULE_PAGES:
            return "bad page"
    elif "page" in m:
        return "page only on kind page"
    if kind == "catalog":
        secs = m.get("sections")
        if not isinstance(secs, list) or not secs:
            return "bad sections"
        for s in secs:
            if not isinstance(s, dict) or not isinstance(s.get("title"), str):
                return "bad section"
            items = s.get("items")
            if not isinstance(items, list):
                return "bad section items"
            for it in items:
                if not isinstance(it, dict) or not isinstance(it.get("label"), str):
                    return "bad item"
                cid = it.get("catalog")
                if cid not in BY_ID:
                    return f"unknown catalog id {cid}"
                ui = it.get("params_ui", {})
                if not isinstance(ui, dict):
                    return "bad params_ui"
                names = {p["name"] for p in BY_ID[cid]["params"]}
                for pname, pv in ui.items():
                    if pname not in names or not isinstance(pv, dict):
                        return f"params_ui names no param: {pname}"
    elif "sections" in m:
        return "sections only on kind catalog"
    if kind == "card":
        card = m.get("card")
        if not isinstance(card, dict):
            return "bad card"
        text = card.get("text")
        if not isinstance(text, str) or len(text) > 600:
            return "bad card text"
        for ln in card.get("links", []):
            if not isinstance(ln, dict) or not isinstance(ln.get("label"), str):
                return "bad card link"
            if "catalog" in ln and ln["catalog"] not in BY_ID:
                return f"unknown catalog id {ln['catalog']}"
    if "commands" in m:
        return _check_commands(m["commands"])
    return None


def _check_commands(cmds):
    """Section 18 palette commands: an error string, or None."""
    if not isinstance(cmds, list):
        return "not a list"
    seen = set()
    for c in cmds:
        if not isinstance(c, dict):
            return "bad command"
        slash = c.get("slash")
        if not isinstance(slash, str) or not SLUG_RE.fullmatch(slash):
            return "bad slash"
        if slash in BUILTIN_SLASHES:
            return "built-in slash"
        if slash in seen:
            return "duplicate slash"
        seen.add(slash)
        label = c.get("label")
        if not isinstance(label, str) or not (1 <= len(label) <= 32):
            return "bad command label"
        if "hint" in c and (not isinstance(c["hint"], str) or len(c["hint"]) > 120):
            return "bad hint"
        args = c.get("args", [])
        if not isinstance(args, list):
            return "bad args"
        names = []
        for a in args:
            if (not isinstance(a, dict) or not isinstance(a.get("name"), str)
                    or not SLUG_RE.fullmatch(a["name"]) or a["name"] in names):
                return "bad arg"
            if a.get("type") not in COMMAND_ARG_TYPES:
                return "bad arg type"
            if a["type"] == "enum" and not (isinstance(a.get("values"), list) and a["values"]
                                            and all(isinstance(v, str) for v in a["values"])):
                return "enum needs values"
            names.append(a["name"])
        run = c.get("run")
        if not isinstance(run, dict) or len(run) != 1 or not ({"catalog", "send"} & set(run)):
            return "bad run"
        if "catalog" in run:
            cid = run["catalog"]
            if cid not in BY_ID:
                return f"unknown catalog id {cid}"
            params = {p["name"] for p in BY_ID[cid]["params"]}
            for n in names:
                if n not in params:
                    return f"arg names no param: {n}"
        else:
            send = run["send"]
            if not isinstance(send, dict) or send.get("kind") not in SEND_KINDS:
                return "bad send"
            types = {a["name"]: a["type"] for a in args}
            if types.get("desk") != "desk" or types.get("text") != "text":
                return "send needs desk and text args"
    return None


def load_modules(builtin_dir, user_dir):
    """Read ./modules/*.json then <state>/modules/*.json. A user module with a
    built-in's id replaces it. Returns {"modules": [...], "errors": [...]};
    a broken file is reported, never raised."""
    modules, errors, _files = _load(builtin_dir, user_dir)
    return {"modules": modules, "errors": errors}


def _load(builtin_dir, user_dir):
    """load_modules' work: (modules, errors, {id: "builtin/x.json" | "user/x.json"}
    of the file each loaded id came from). ``user_dir`` None reads the built-ins alone."""
    found, files, errors = {}, {}, []
    folders = [("builtin", Path(builtin_dir))] + ([("user", Path(user_dir))] if user_dir is not None else [])
    for label, folder in folders:
        if not folder.is_dir():
            continue
        for f in sorted(folder.glob("*.json")):
            rel = f"{label}/{f.name}"
            try:
                m = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                errors.append({"file": rel, "error": f"cannot read: {type(exc).__name__}"})
                continue
            err = _check_manifest(m)
            if err:
                errors.append({"file": rel, "error": err})
                continue
            found[m["id"]] = m
            files[m["id"]] = rel
    modules = []
    owner = {}   # slash -> module id; the earlier-ordered module keeps it (server-flows-10)
    for m in sorted(found.values(), key=lambda m: (m["order"], m["id"])):
        slashes = [c["slash"] for c in m.get("commands", [])]
        taken = [s for s in slashes if s in owner]
        if taken:
            errors.append({"file": files[m["id"]],
                           "error": f"slash /{taken[0]} taken by module {owner[taken[0]]}"})
            continue
        for s in slashes:
            owner[s] = m["id"]
        modules.append(m)
    return modules, errors, files


def _peek_id(path):
    try:
        m = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return m.get("id") if isinstance(m, dict) and isinstance(m.get("id"), str) else None


def module_report(builtin_dir, user_dir):
    """25.3 B6: ``{builtin: [ids], user: [{id, file, ok, error}], folder}``.
    Read from the files as they are now, so ``ok`` says what a Reload would
    load. A user file is ok when it loads and is the file its id came from
    (of two user files with one id, the later one wins)."""
    builtin = [m["id"] for m in _load(builtin_dir, None)[0]]
    modules, errors, files = _load(builtin_dir, user_dir)
    loaded = {m["id"] for m in modules}
    bad = {e["file"]: e["error"] for e in errors}
    user = []
    folder = Path(user_dir)
    for f in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        rel, mid = f"user/{f.name}", _peek_id(f)
        err = bad.get(rel)
        if err is None and (mid not in loaded or files.get(mid) != rel):
            # the id went to a later file (loaded, or dropped for a taken slash: then neither loads)
            other = files.get(mid, "")
            err = ("another file has this id: " + other.split("/", 1)[-1]) \
                if other.startswith("user/") and other != rel else "not loaded"
        user.append({"id": mid, "file": str(f), "ok": err is None, "error": err})
    return {"builtin": builtin, "user": user, "folder": str(folder)}


def builtin_ids(builtin_dir):
    """The ids the built-in module files carry (25.3 B8: New module never takes one)."""
    return {i for i in (_peek_id(f) for f in sorted(Path(builtin_dir).glob("*.json"))) if i}


def free_slash(base, taken):
    """A slash for a starter module's example command: ``base`` (a slug) unless
    the composer or a module has it, else ``base-2``, ``base-3``, ... cut to
    stay a slug."""
    taken = set(taken) | BUILTIN_SLASHES
    if base not in taken:
        return base
    n = 2
    while True:
        tail = f"-{n}"
        cand = base[:24 - len(tail)].rstrip("-") + tail
        if cand not in taken and SLUG_RE.fullmatch(cand):
            return cand
        n += 1


def starter_module(mid, label, taken_slashes=()):
    """25.3 B8: the fixed template POST /api/modules/new writes: a ``catalog``
    module with one section of two reads and one example slash command that
    runs the first of them."""
    slash = free_slash(mid, taken_slashes)
    return {"id": mid, "label": label, "icon": "◇", "order": 110, "kind": "catalog", "phase": 1,
            "modes": ["user", "dev"],
            "sections": [{"title": "Start here", "items": [
                {"catalog": "status", "label": "Status"},
                {"catalog": "workflow.list", "label": "Workflows"}]}],
            "commands": [{"slash": slash, "label": "Status",
                          "hint": f"/{slash} — what MetaBot is doing now (an example: edit this file, then Reload)",
                          "run": {"catalog": "status"}}]}
