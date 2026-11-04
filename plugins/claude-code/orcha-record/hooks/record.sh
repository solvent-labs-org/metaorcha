#!/usr/bin/env bash
# orcha-record: the Claude Code hook entry. The hook payload on stdin goes
# straight to `orcha record hook claude-code` in the published SDK; nothing
# is read or decided here.
#
# A hook must never break the session it records: the SDK exits 0 (recorded
# or sealed) or 1 (a non-blocking error the harness shows you), never 2 —
# a receipt never blocks a tool call or a Stop. Any failure of this launcher
# itself (no uv, a stale package, a resolver error) is mapped to 1 for the
# same reason.
set -u

# The SDK release that carries `orcha record`. Override to pin, or to point
# at a checkout while developing: ORCHA_SDK_SPEC=/path/to/sdk
spec="${ORCHA_SDK_SPEC:-orcha-sdk>=0.2.0}"
# Extra arguments for the hook, e.g. "--criteria exit_zero" to declare that
# every Bash call must exit 0. Word-split on purpose.
# shellcheck disable=SC2086
if command -v uvx >/dev/null 2>&1; then
  uvx --quiet --from "$spec" orcha record hook claude-code ${ORCHA_RECORD_ARGS:-}
elif command -v orcha >/dev/null 2>&1; then
  orcha record hook claude-code ${ORCHA_RECORD_ARGS:-}
else
  echo "orcha-record: neither uvx nor orcha found; install uv or 'pip install orcha-sdk'" >&2
  exit 1
fi
rc=$?
[ "$rc" -eq 0 ] || exit 1
