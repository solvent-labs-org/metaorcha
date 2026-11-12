#!/bin/sh
# Prove the desktop shell binary without a display: with a fake metabot-app on PATH it must
# start the server and report the port (exit 0); with a live record it must attach; with
# nothing on PATH it must say missing (exit 3). The token is never printed.
# usage: tools/desktop-smoke.sh PATH_TO_metabot-desktop
set -eu
BIN="$1"
T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT
mkdir -p "$T/bin" "$T/home" "$T/state"
cat > "$T/bin/metabot-app" <<FAKE
#!/bin/sh
# a stand-in server: records itself, then waits
printf '{"pid": %s, "port": 8499, "build": "smoke"}' \$\$ > "$T/state/server.json"
printf 'smoke-token\n' > "$T/state/token"
exec sleep 30
FAKE
chmod +x "$T/bin/metabot-app"

out=$(HOME="$T/home" METACHAT_STATE="$T/state" PATH="$T/bin:/usr/bin:/bin" METABOT_SHELL_SMOKE=1 "$BIN")
echo "start: $out"
[ "$out" = "start port=8499" ] || { echo "expected 'start port=8499'"; exit 1; }

out=$(HOME="$T/home" METACHAT_STATE="$T/state" PATH="$T/bin:/usr/bin:/bin" METABOT_SHELL_SMOKE=1 "$BIN")
echo "attach: $out"
[ "$out" = "attach port=8499" ] || { echo "expected 'attach port=8499'"; exit 1; }

rm -f "$T/state/server.json" "$T/state/token"
set +e
out=$(HOME="$T/home" METACHAT_STATE="$T/state" PATH="/usr/bin:/bin" METABOT_SHELL_SMOKE=1 "$BIN"); code=$?
set -e
echo "missing: $out (exit $code)"
[ "$out" = "missing port=-" ] && [ "$code" -eq 3 ] || { echo "expected 'missing port=-' and exit 3"; exit 1; }
echo "desktop smoke ok"
