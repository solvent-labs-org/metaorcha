#!/usr/bin/env bash
# The tablet walk, scripted: `tools/tablet-e2e.sh remote_pair` repeats the typed-code pairing
# over USB with no touch input: install the debug APK, pair by typed code against this PC's
# TLS listener (reached on the tablet's loopback through adb reverse), compare the four words
# the tablet logged with the PC's, see the page over TLS, revoke from the CLI, see the
# pairing screen come back, pair again. Screenshots and a jsonl report go to the out folder.
#
#   MB_STATE=... MB_CONFIG=... tools/tablet-e2e.sh remote_pair [--apk PATH] [--port 8471] [--tls 8473]
#
# Needs: the app server running on this PC with its phone listener (the config's phone_port),
# a debuggable APK (android/build.sh with MB_DEBUGGABLE=1), the tablet attached with USB
# debugging on, and nobody using the tablet (this takes the screen; it never types).
set -uo pipefail   # not -e: every adb call is a recorded step, never a silent exit
ADB=${ADB:-$HOME/phone-security/platform-tools/adb}
APP=$(cd "$(dirname "$0")/.." && pwd)
PKG=ai.metaorcha.metabot
OUT=${TABLET_OUT:-/var/tmp/metabot-app/tablet}/$(date -u +%Y%m%dT%H%M%SZ); mkdir -p "$OUT"
REPORT="$OUT/report.jsonl"
JOURNEY=${1:-remote_pair}; shift || true
APK=${MB_APK:-/var/tmp/metabot-app/apk/$PKG.apk}; PORT=8471; TLS=8473
while [ $# -gt 0 ]; do case "$1" in --apk) APK=$2; shift 2;; --port) PORT=$2; shift 2;; --tls) TLS=$2; shift 2;; *) echo "unknown $1" >&2; exit 2;; esac; done
[ "$JOURNEY" = remote_pair ] || { echo "only remote_pair is scripted today" >&2; exit 2; }
: "${MB_STATE:?set MB_STATE to the state folder of the running server}"; : "${MB_CONFIG:?set MB_CONFIG to its config.json}"
export METACHAT_STATE=$MB_STATE METACHAT_CONFIG=$MB_CONFIG PYTHONPATH=$APP/src
SERIAL=$("$ADB" devices | awk 'NR>1 && $2=="device"{print $1; exit}')
[ -n "$SERIAL" ] || { echo "no tablet attached (adb devices is empty or unauthorised)" >&2; exit 2; }
A() { "$ADB" -s "$SERIAL" "$@"; }
T0=$(date +%s%N)
step() {  # step NAME OK DETAIL
  local now=$(date +%s%N); local ms=$(( (now - T0) / 1000000 )); T0=$now
  printf '{"ts": %s, "session": "%s", "journey": "remote_pair", "step": "%s", "status": "%s", "ok": %s, "ms": %s, "budget_ms": null, "origin": "runner", "run_id": null, "receipt_sha256": null, "detail": "%s"}\n' \
    "$(date +%s)" "$(basename "$OUT")" "$1" "$([ "$2" = true ] && echo pass || echo fail)" "$2" "$ms" "${3//\"/\\\"}" >> "$REPORT"
  echo "$([ "$2" = true ] && echo PASS || echo FAIL) $1 ${3:+— $3}"
  [ "$2" = true ] || FAILED=1
}
FAILED=0
shot() { A exec-out screencap -p > "$OUT/$1.png" 2>/dev/null || true; }
focus() { A shell dumpsys window 2>/dev/null | grep -m1 mCurrentFocus | sed 's/.*mCurrentFocus=//'; }
wait_focus() {  # wait_focus ACTIVITY SECONDS
  local i; for i in $(seq 1 "$2"); do focus | grep -q "$1" && return 0; sleep 1; done; return 1
}
pair_cli() { python3 -m metabot_app.cli pair --port "$PORT"; }
devices_cli() { python3 -m metabot_app.cli devices --port "$PORT" "$@"; }

# 1. install (always -r: the debug build's versionCode is fixed) and reach the listener on loopback
inst=$(A install -r "$APK" 2>&1)
if printf '%s' "$inst" | grep -q INSTALL_FAILED_UPDATE_INCOMPATIBLE; then
  A uninstall $PKG >/dev/null 2>&1   # our test package only (a build signed with another key); never any other package
  inst=$(A install -r "$APK" 2>&1)
fi
printf '%s' "$inst" | grep -q Success && step install true "$APK" || step install false "$(printf '%s' "$inst" | tail -1)"
A reverse "tcp:$TLS" "tcp:$TLS" >/dev/null && step reverse true "tcp:$TLS" || step reverse false "adb reverse failed"
A shell am force-stop $PKG >/dev/null 2>&1 || true   # our test package only; never any other package
A logcat -c >/dev/null 2>&1 || true

pair_once() {  # pair_once LABEL
  local out code words
  out=$(pair_cli) || { step "$1 code" false "pair command failed"; return; }
  code=$(printf '%s\n' "$out" | sed -n 's/^  Code:    \([0-9A-Z]*\) \([0-9A-Z]*\).*/\1\2/p')
  words=$(printf '%s\n' "$out" | sed -n 's/^  Words:   //p')
  [ -n "$code" ] && [ -n "$words" ] && step "$1 code" true "code issued" || { step "$1 code" false "no code in the pair output"; return; }
  # the launcher activity forwards the extras to the pairing screen (debuggable builds only)
  A shell am start -S -n "$PKG/.MainActivity" --es address "127.0.0.1:$TLS" --es code "$code" --ez confirm true >/dev/null 2>&1
  local i got=""
  for i in $(seq 1 20); do
    got=$(A logcat -d -s MetaBot:I 2>/dev/null | sed -n 's/.*pair words: //p' | tail -1); [ -n "$got" ] && break; sleep 1
  done
  [ "$got" = "$words" ] && step "$1 words match" true "$words" || step "$1 words match" false "tablet said '${got:-nothing}'"
  wait_focus "$PKG/$PKG.MainActivity" 20 && step "$1 page opens" true "$(focus)" || step "$1 page opens" false "$(focus) $(A logcat -d -s MetaBot:I 2>/dev/null | sed -n 's/.*pair message: //p' | tail -1)"
  sleep 3; shot "$1-page"
}

devices_cli > "$OUT/devices-0.txt" 2>/dev/null || true   # the phones paired before this walk (never revoked here)
pair_once first
devices_cli | tee "$OUT/devices-1.txt" | grep -q . && step "device listed" true "$(grep -c . "$OUT/devices-1.txt") line(s)" || step "device listed" false "no paired phones"
# revoke the device this walk just paired: the id in the list now that was not in the list before
ids() { sed -n 's/^\([A-Za-z0-9_-]\{4,\}\) .*/\1/p' "$1"; }
ID=$(grep -vxF -f <(ids "$OUT/devices-0.txt"; echo __none__) <(ids "$OUT/devices-1.txt") | head -1)
if [ -n "$ID" ]; then
  devices_cli --remove "$ID" >/dev/null && step revoke true "$ID" || step revoke false "remove failed"
  A shell am start -S -n "$PKG/.MainActivity" >/dev/null 2>&1   # relaunch: the main-frame 403 sends it to pairing
  wait_focus "$PKG/$PKG.PairActivity" 20 && step "pairing screen after revoke" true "$(focus)" || step "pairing screen after revoke" false "$(focus)"
  shot after-revoke
  pair_once again
else
  step revoke false "no device id parsed"
fi
A logcat -d 2>/dev/null | grep -E "chromium|Console|WebView" | grep -iE "error|uncaught|failed" | tail -15 > "$OUT/webview-errors.txt" || true
printf '{"summary": true, "ts": %s, "session": "%s", "pass": %s, "fail": %s, "skipped": 0, "invalid": 0, "budget_misses": [], "caps": {}, "build": "", "model_source": "none", "device": {"model": "%s", "os": "android", "abi": "%s"}, "mode": "remote"}\n' \
  "$(date +%s)" "$(basename "$OUT")" "$(grep -c '"status": "pass"' "$REPORT")" "$(grep -c '"status": "fail"' "$REPORT")" \
  "$(A shell getprop ro.product.model 2>/dev/null | tr -d '\r')" "$(A shell getprop ro.product.cpu.abi 2>/dev/null | tr -d '\r')" >> "$REPORT"
python3 "$APP/tools/selftest_report.py" "$REPORT" --md "$OUT/report.md" >/dev/null || true
echo "report: $OUT/report.md (shots beside it)"
exit $FAILED
