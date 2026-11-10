#!/usr/bin/env bash
# The Linux self-test: the runner against this tree's app server and the MetaBot core,
# in a network namespace with only loopback (nothing can leave the machine), with a
# scratch home, key and state. This is the former fresh-user harness.
#
#   METABOT_PYTHON=/path/to/venv/bin/python tools/selftest-linux.sh [--journeys a,b] [--enforce-budgets]
#
# METABOT_PYTHON: an interpreter with the metaorcha core installed (its metabot and
# orcha scripts sit beside it). SELFTEST_OUT: where the session folder goes
# (default /var/tmp/metabot-app/selftest). Exit code = the report's verdict.
set -euo pipefail
APP=$(cd "$(dirname "$0")/.." && pwd)
PY=${METABOT_PYTHON:-python3}
BIN=$(dirname "$(command -v "$PY" || echo "$PY")")
MB=${METABOT_BIN:-$BIN/metabot}; ORCHA=${ORCHA_BIN:-$BIN/orcha}
[ -x "$MB" ] || { echo "selftest: no metabot beside $PY (set METABOT_PYTHON to a venv with the core)" >&2; exit 3; }
OUT=${SELFTEST_OUT:-/var/tmp/metabot-app/selftest}; mkdir -p "$OUT"
ISO=()
if unshare --user --map-root-user --net true 2>/dev/null; then
  ISO=(unshare --user --map-root-user --net sh -c 'ip link set lo up >/dev/null 2>&1 || true; exec "$@"' --)
else
  echo "selftest: no network namespace here; running with the network on" >&2
fi
exec env PYTHONPATH="$APP/src" "${ISO[@]}" "$PY" -m metabot_app.selftest.run --out "$OUT" \
  --metabot-bin "$MB" --orcha-bin "$ORCHA" --metabot-python "$PY" --app-python "$PY" "$@"
