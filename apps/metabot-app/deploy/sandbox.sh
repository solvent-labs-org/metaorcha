#!/usr/bin/env bash
# deploy/sandbox.sh up [--tablet] | down | status | token-file   (contract 25.6)
#
# The branch server in a box, for live walks that cannot touch the real app:
#   - its own HOME ($ROOT/home): the names folder, ~/.claude, ~/.orcha
#     and ~/.config/cursor it sees are all scratch;
#   - its own tmux server (a `tmux` wrapper on PATH adds -L mb-sandbox);
#   - its own METABOT_HOME and signing key, and its own token;
#   - port 8471 on 127.0.0.1 (with --tablet, also MB_SANDBOX_TABLET_IP), never 8460.
# `down` stops only the server whose pid file and environment say it is this box,
# and only the mb-sandbox tmux server.
set -euo pipefail
HERE=$(cd "$(dirname "$0")/.." && pwd)
REAL_HOME=$HOME
ROOT=${MB_SANDBOX:-$REAL_HOME/.local/state/metachat-sandbox}
PORT=${MB_SANDBOX_PORT:-8471}
SOCK=${MB_SANDBOX_SOCK:-mb-sandbox}   # a second box needs its own socket name too
TABLET_IP=${MB_SANDBOX_TABLET_IP:-}
CFG="$ROOT/config.json"

ours() {   # is pid $1 this box's server?
  local pid=$1
  [ -r "/proc/$pid/cmdline" ] || return 1
  tr '\0' ' ' < "/proc/$pid/cmdline" | grep -q "$HERE/server.py" || return 1
  tr '\0' '\n' < "/proc/$pid/environ" 2>/dev/null | grep -qx "METACHAT_CONFIG=$CFG"
}

case "${1:-status}" in
up)
  [ "$PORT" != 8460 ] || { echo "sandbox: refusing port 8460 (the live app)"; exit 2; }
  if [ -f "$ROOT/server.pid" ] && ours "$(cat "$ROOT/server.pid")"; then echo "sandbox: already up (pid $(cat "$ROOT/server.pid"))"; exit 0; fi
  mkdir -p -m 700 "$ROOT" "$ROOT/home" "$ROOT/state" "$ROOT/mbhome" "$ROOT/bin" "$ROOT/projects/sandbox"
  printf '#!/bin/sh\nexec /usr/bin/tmux -L %s "$@"\n' "$SOCK" > "$ROOT/bin/tmux"; chmod 755 "$ROOT/bin/tmux"
  TABLET=0; [ "${2:-}" = "--tablet" ] && TABLET=1
  if [ "$TABLET" = 1 ] && [ -z "$TABLET_IP" ]; then echo "sandbox: --tablet needs MB_SANDBOX_TABLET_IP"; exit 2; fi
  python3 -I - "$ROOT" "$PORT" "$REAL_HOME" "$TABLET" "$TABLET_IP" <<'PY'
import json, os, sys
root, port, real, tablet, ip = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4] == "1", sys.argv[5]
live = json.load(open(os.path.join(real, ".config/metachat/config.json")))
bind, hosts = ["127.0.0.1"], [f"127.0.0.1:{port}", f"localhost:{port}"]
if tablet and ip:
    bind.append(ip); hosts.append(f"{ip}:{port}")
cfg = {
    "port": port, "bind": bind, "allowed_hosts": hosts,
    "state_dir": f"{root}/state",
    "default_desk": "", "claude_name_prefixes": ["claude"], "tmux_socket": "mb-sandbox",
    "metabot_bin": live["metabot_bin"], "orcha_bin": live["orcha_bin"],
    "metabot_python": live["metabot_python"], "metabot_home": f"{root}/mbhome",
    "projects": [{"name": "sandbox", "paths": [f"{root}/projects/sandbox"]}],
    "project_roots": [f"{root}/projects"],
    "default_folder": f"{root}/projects/sandbox",
    "max_desks": 12, "max_runs": 3, "spawn_per_minute": 20,
}
with open(f"{root}/config.json", "w") as f:
    json.dump(cfg, f, indent=1)
os.chmod(f"{root}/config.json", 0o600)
PY
  ( cd "$ROOT" && exec env -i HOME="$ROOT/home" USER="$(id -un)" LANG=C.UTF-8 TERM=xterm-256color \
      PATH="$ROOT/bin:$REAL_HOME/.local/bin:/usr/local/bin:/usr/bin:/bin" \
      METACHAT_CONFIG="$CFG" METABOT_HOME="$ROOT/mbhome" ORCHA_KEY_PATH="$ROOT/home/.orcha/key" \
      setsid python3 -B "$HERE/server.py" >"$ROOT/server.out" 2>&1 < /dev/null ) &
  echo $! > "$ROOT/server.pid"
  for _ in $(seq 1 40); do
    T=$(cat "$ROOT/state/token" 2>/dev/null || true)
    if [ -n "$T" ] && curl -s -o /dev/null -w '%{http_code}' -H "Host: 127.0.0.1:$PORT" "http://127.0.0.1:$PORT/api/meta?t=$T" | grep -q 200; then
      unset T; echo "sandbox: up on 127.0.0.1:$PORT (pid $(cat "$ROOT/server.pid")); token in $ROOT/state/token"; exit 0
    fi
    sleep 0.25
  done
  unset T; echo "sandbox: did not answer; see $ROOT/server.out"; exit 1 ;;
down)
  if [ -f "$ROOT/server.pid" ]; then
    pid=$(cat "$ROOT/server.pid")
    if ours "$pid"; then kill "$pid" && echo "sandbox: stopped pid $pid"; else echo "sandbox: pid $pid is not this box's server; left alone"; fi
    rm -f "$ROOT/server.pid"
  fi
  /usr/bin/tmux -L "$SOCK" kill-server 2>/dev/null && echo "sandbox: mb-sandbox tmux server stopped" || true ;;
status)
  if [ -f "$ROOT/server.pid" ] && ours "$(cat "$ROOT/server.pid")"; then echo "sandbox: up (pid $(cat "$ROOT/server.pid")) on $PORT"; else echo "sandbox: down"; fi
  /usr/bin/tmux -L "$SOCK" ls 2>/dev/null || echo "sandbox: no desks" ;;
token-file) echo "$ROOT/state/token" ;;
*) echo "usage: $0 up [--tablet] | down | status | token-file"; exit 2 ;;
esac
