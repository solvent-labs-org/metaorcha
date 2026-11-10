#!/usr/bin/env python3
"""mbq.py — MetaBot queries, run under the MetaBot venv's python (contract 8).

Prints one JSON object on stdout and exits 0. On any exception it prints
``{"error": "<type>: <msg>"}`` and exits 1. ``METABOT_HOME`` is pinned by the
caller (mb.py), so every reader below looks at the configured home.

Subcommands:
  runs         {"rows": [...], "total": N, "error": ...}  from web_data.run_rows()
  live         {"alive": [...], "stale": [...]}            from live.live_entries()
  run <id>     web_data.run_view(id), or null
  logins       [{"name","engine","home","max_at_once"}, ...] from metabot.logins._logins()
  state <id>   {"state": web_data.receipt_state(id)}
  plugins      {"plugins": [...]}        25.3 B6, names only (see below)
  marketplaces {"marketplaces": [...]}
  engines      {"engines": [...], "error": str|None}
  serve        a warm worker: one JSON request per stdin line (see serve())

Facts read from MetaBot 0.3.0 source (apps/metabot/src/metabot):
- the logins list function is ``metabot.logins._logins()`` -> {name: Login}
  (``cmd_login_list`` prints it); Login has name, engine, home, max_at_once.
- the receipt digest in an index row (``receipts.seal_and_store`` ->
  ``_append_index``) is the field ``receipt_sha256``: the sha256 hex of the
  receipt file's text as written. mb.py compares it with the file bytes.

Facts read from MetaBot 0.4.0 source (25.3 B6):
- ``metabot.plugins.installed()`` -> [InstalledPlugin]: tree, name, market,
  version, enabled, hooks_allowed, connections (ids it added: --add-servers),
  bridges (name -> spec, empty unless --allow-bridges); ``.ref`` is
  NAME@MARKET and ``.root`` the installed copy. ``inspect_plugin(root)`` ->
  PluginFacts: skills (name -> description), commands, agents, hooks (event ->
  command lines), servers (name -> command line or url), bridges (name ->
  command line). Only names and counts leave this file: a command line can
  carry a password, as an MCP server's args do.
- ``marketplaces()`` -> {name: folder}, the built-in "metabot" first;
  ``market_plugins(folder)`` -> [(name, description)].
- ``metabot.engines.ENGINES`` (claude, codex, cursor, openai) and
  ``bridge_table()`` -> {name: (spec, InstalledPlugin or None)}; a run's model
  is ``Config.models[engine]``, and ``openai_model`` for openai
  (workflow_commands); ``metabot.ladder.is_local_url(openai_base_url)`` says
  whether openai is a model on this machine.
"""
import json
import os
import sys
import time


def _plugins():
    from metabot import plugins
    return plugins


def _plugin(p):
    """One installed plugin: names and counts, never a command line."""
    P = _plugins()
    row = {"name": p.name, "ref": p.ref, "marketplace": p.market, "tree": p.tree, "version": p.version,
           "enabled": bool(p.enabled), "hooks_allowed": bool(p.hooks_allowed),
           "servers_added": bool(p.connections), "bridges_on": bool(p.bridges),
           "skills": [], "commands": [], "agents": [], "hooks": {}, "servers": [], "bridges": [],
           "error": None}
    try:
        facts = P.inspect_plugin(p.root)
    except (P.PluginError, OSError, ValueError, RecursionError):
        row["error"] = "its installed copy does not read"
        return row
    row.update(skills=sorted(facts.skills), commands=list(facts.commands), agents=list(facts.agents),
               hooks={event: len(cmds) for event, cmds in facts.hooks.items()},
               servers=sorted(facts.servers), bridges=sorted(facts.bridges))
    return row


def _engines():
    """The built-in engines, then the bridges a run may name (engine_names()'s order)."""
    from metabot.config import ConfigError, load_config
    from metabot.engines import ENGINES, bridge_table
    from metabot.ladder import is_local_url
    try:
        cfg, err = load_config(), None
    except ConfigError as exc:
        cfg, err = None, f"ConfigError: {exc}"[:300]
    out = []
    for name in ENGINES:
        models, local = [], False
        if cfg is not None:
            chosen = cfg.models.get(name) if isinstance(cfg.models, dict) else None
            for m in (chosen, cfg.openai_model if name == "openai" else None):
                if isinstance(m, str) and m and m not in models:
                    models.append(m)
            local = name == "openai" and bool(is_local_url(cfg.openai_base_url))
        out.append({"name": name, "kind": "builtin", "local": local, "models": models, "plugin": None})
    for name, (spec, plugin) in bridge_table().items():
        if name in ENGINES:
            continue
        models = spec.get("models") if isinstance(spec, dict) else None
        out.append({"name": name, "kind": "bridge", "local": False,
                    "models": [m for m in models if isinstance(m, str)] if isinstance(models, list) else [],
                    "plugin": plugin.ref if plugin is not None else None})
    return {"engines": out, "error": err}


def main(argv):
    if not argv:
        raise ValueError("usage: mbq.py runs|live|run ID|logins|state ID|plugins|marketplaces|engines")
    sub, rest = argv[0], argv[1:]
    if sub == "runs":
        from metabot import web_data
        rows, total, err = web_data.run_rows()
        return {"rows": rows, "total": total, "error": err}
    if sub == "live":
        from metabot import live
        alive, stale = live.live_entries()
        return {"alive": alive, "stale": stale}
    if sub == "run":
        from metabot import web_data
        if len(rest) != 1 or not web_data.is_safe_id(rest[0]):
            return None
        return web_data.run_view(rest[0])
    if sub == "logins":
        from metabot import logins
        out = []
        for login in logins._logins().values():
            out.append({"name": login.name, "engine": login.engine, "home": login.home,
                        "max_at_once": int(login.max_at_once)})
        return out
    if sub == "state":
        from metabot import web_data
        if len(rest) != 1:
            raise ValueError("state needs one id")
        return {"state": web_data.receipt_state(rest[0])}
    if sub == "plugins":
        return {"plugins": [_plugin(p) for p in _plugins().installed()]}
    if sub == "marketplaces":
        P = _plugins()
        out = []
        for name, folder in P.marketplaces().items():
            row = {"name": name, "folder": str(folder), "plugins": [], "error": None}
            try:
                row["plugins"] = [{"name": n, "description": d} for n, d in P.market_plugins(folder)]
            except (P.PluginError, OSError, ValueError, RecursionError) as exc:
                row["error"] = f"{type(exc).__name__}: {exc}"[:300]
            out.append(row)
        return {"marketplaces": out}
    if sub == "engines":
        return _engines()
    raise ValueError(f"unknown subcommand {sub!r}")


def _memo_receipt_state():
    """In a warm worker, a receipt's state is read once per (receipt file,
    run journal) stat pair: a sealed receipt does not change unless its files
    do. A seal's members are other files, so every answer also expires after
    MEMO_TTL seconds."""
    from metabot import web_data
    orig = getattr(web_data, "receipt_state", None)
    if orig is None or getattr(orig, "_memo", False):
        return
    try:
        from metabot.paths import receipts_dir, run_path
    except ImportError:
        receipts_dir = run_path = None
    seen = {}

    def stat_of(path):
        try:
            st = os.stat(path)
            return (st.st_mtime_ns, st.st_size, st.st_ino)
        except (OSError, TypeError, ValueError):
            return None

    def memo(run_id):
        key = None
        if receipts_dir is not None and isinstance(run_id, str):
            try:
                key = (run_id, stat_of(receipts_dir() / f"{run_id}.json"), stat_of(run_path(run_id)))
            except Exception:   # noqa: BLE001 - an odd id: no memo, ask MetaBot
                key = None
        hit = seen.get(key) if key is not None else None
        now = time.monotonic()
        if hit is not None and now - hit[0] < MEMO_TTL:
            return hit[1]
        out = orig(run_id)
        if key is not None:
            seen[key] = (now, out)
        return out

    memo._memo = True
    web_data.receipt_state = memo


MEMO_TTL = 60.0


def serve():
    """Speed (11-09): one warm process. One JSON request per stdin line,
    ``{"id": n, "args": [sub, ...]}``; one reply per stdout line, ``{"id": n,
    "result": ...}`` or ``{"id": n, "error": "<type>: <msg>"}``. A bad request
    never ends the worker; EOF on stdin does. ``ping`` answers ``{"pong": true}``."""
    memo_ready = False
    for line in sys.stdin:
        rid = None
        try:
            q = json.loads(line)
            rid = q.get("id") if isinstance(q, dict) else None
            args = q.get("args") if isinstance(q, dict) else None
            if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
                raise ValueError("args must be a list of strings")
            if args == ["ping"]:
                out = {"pong": True}
            else:
                if not memo_ready and args[:1] in (["runs"], ["state"]):
                    _memo_receipt_state()
                    memo_ready = True
                out = main(args)
            reply = {"id": rid, "result": out}
        except BaseException as exc:  # noqa: BLE001 - one bad request never ends the worker
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            reply = {"id": rid, "error": f"{type(exc).__name__}: {exc}"}
        sys.stdout.write(json.dumps(reply, default=str) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    if sys.argv[1:] == ["serve"]:
        serve()
        sys.exit(0)
    try:
        out = main(sys.argv[1:])
    except BaseException as exc:  # noqa: BLE001 - the caller gets one JSON line either way
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}))
        sys.exit(1)
    print(json.dumps(out, default=str))
    sys.exit(0)
