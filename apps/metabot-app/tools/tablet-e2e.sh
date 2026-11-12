#!/usr/bin/env bash
# The tablet walk, scripted, over USB with no touch input beyond adb's injected taps:
#
#   remote_pair       install the debug APK, pair by typed code against this PC's TLS listener
#                     (reached on the tablet's loopback through adb reverse), compare the four
#                     words the tablet logged with the PC's, see the page over TLS, revoke from
#                     the CLI, see the pairing screen come back, pair again.
#   perf_scroll_type  seed a lane with 200 messages, fling it (median fps from framestats
#                     Vsync deltas), then inject 20 keys (keydown->present per key from the
#                     frames that consumed them). Record-only: budgets stay null until three
#                     tablet runs set them (plan T2).
#   sheet_taps        the missed y/n key-strip taps, reproduced: a shell desk waits at a
#                     y/n prompt, the script taps where the y key sits, and the server tells
#                     whether the answer landed. A screenshot per tap; the miss rate is the data.
#
#   MB_STATE=... MB_CONFIG=... tools/tablet-e2e.sh JOURNEY [--apk PATH] [--port 8471] [--tls 8473]
#
# perf_scroll_type and sheet_taps need the app already paired (run remote_pair first). Taps are
# coordinate-based (the WebView exposes no nodes to uiautomator); the defaults were calibrated
# on the tablet by flood-filling screencaps for the chip colours (see the calibrated-taps
# block), and MB_TAP_WAITROW / MB_TAP_YKEY / MB_TAP_COMPOSER ("x y") override them —
# the before/after shots show where a tap actually landed.
# Needs: the app server running on this PC with its phone listener (the config's phone_port),
# a debuggable APK (android/build.sh with MB_DEBUGGABLE=1), the tablet attached with USB
# debugging on, and nobody using the tablet (this takes the screen).
set -uo pipefail   # not -e: every adb call is a recorded step, never a silent exit
ADB=${ADB:-$HOME/phone-security/platform-tools/adb}
APP=$(cd "$(dirname "$0")/.." && pwd)
PKG=ai.metaorcha.metabot
OUT=${TABLET_OUT:-/var/tmp/metabot-app/tablet}/$(date -u +%Y%m%dT%H%M%SZ); mkdir -p "$OUT"
REPORT="$OUT/report.jsonl"
JOURNEY=${1:-remote_pair}; shift || true
APK=${MB_APK:-/var/tmp/metabot-app/apk/$PKG.apk}; PORT=8471; TLS=8473
while [ $# -gt 0 ]; do case "$1" in --apk) APK=$2; shift 2;; --port) PORT=$2; shift 2;; --tls) TLS=$2; shift 2;; *) echo "unknown $1" >&2; exit 2;; esac; done
case "$JOURNEY" in remote_pair|perf_scroll_type|sheet_taps) ;; *) echo "journeys: remote_pair | perf_scroll_type | sheet_taps" >&2; exit 2;; esac
: "${MB_STATE:?set MB_STATE to the state folder of the running server}"; : "${MB_CONFIG:?set MB_CONFIG to its config.json}"
export METACHAT_STATE=$MB_STATE METACHAT_CONFIG=$MB_CONFIG PYTHONPATH=$APP/src
SERIAL=$("$ADB" devices | awk 'NR>1 && $2=="device"{print $1; exit}')
[ -n "$SERIAL" ] || { echo "no tablet attached (adb devices is empty or unauthorised)" >&2; exit 2; }
A() { "$ADB" -s "$SERIAL" "$@"; }
T0=$(date +%s%N)
step() {  # step NAME OK DETAIL
  local now=$(date +%s%N); local ms=$(( (now - T0) / 1000000 )); T0=$now
  printf '{"ts": %s, "session": "%s", "journey": "%s", "step": "%s", "status": "%s", "ok": %s, "ms": %s, "budget_ms": null, "origin": "runner", "run_id": null, "receipt_sha256": null, "detail": "%s"}\n' \
    "$(date +%s)" "$(basename "$OUT")" "$JOURNEY" "$1" "$([ "$2" = true ] && echo pass || echo fail)" "$2" "$ms" "${3//\"/\\\"}" >> "$REPORT"
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
perf() { python3 "$APP/tools/tablet_perf.py" "$@"; }
jsonf() { python3 -c "import json,sys; j=json.load(sys.stdin); $1"; }
SEED_CWD=${MB_SEED_CWD:-$APP}   # the seeded desk's folder; must be one of the server's projects
# the page learns desk state from fetchDesks every ~20 s (no stream event for it — a known
# product gap the lane owns; the desk owner's "missed taps" were taps before the row arrived). The
# journeys wait the cadence out so the taps measure hit-areas, not staleness.
PAGE_WAIT=${MB_PAGE_WAIT_S:-22}

page_open() {  # the paired app on the main page, or a recorded failure
  A shell am start -S -n "$PKG/.MainActivity" >/dev/null 2>&1
  if wait_focus "$PKG/$PKG.MainActivity" 15; then step "page open" true "$(focus)"; return 0
  else step "page open" false "$(focus) — pair the tablet first (remote_pair)"; return 1; fi
}
bounds() {  # sets BX1 BY1 BX2 BY2 DENSITY SCALE CX CY from the WebView on screen
  read BX1 BY1 BX2 BY2 DENSITY < <(perf bounds "$ADB" "$SERIAL" "$PKG") || true
  BX1=${BX1:-0}; BY1=${BY1:-0}; BX2=${BX2:-0}; BY2=${BY2:-0}; DENSITY=${DENSITY:-160}
  SCALE=$(awk "BEGIN{print $DENSITY/160}")
  CX=$(( (BX1 + BX2) / 2 )); CY=$(( (BY1 + BY2) / 2 ))
  if [ "$BX2" -gt "$BX1" ]; then step "webview bounds" true "[${BX1},${BY1}][${BX2},${BY2}] @${DENSITY}"; return 0
  else step "webview bounds" false "no WebView node on screen"; return 1; fi
}
css() { awk "BEGIN{printf \"%d\", $1*$SCALE}"; }   # css px -> device px

# --- the calibrated taps (2800x1752 @320, scale 2; re-derive by flood-filling a screencap
# for the chip colours — the chips are 87x87 device px, found at y 620-707) ---------------
# waiting row card (tree top, opens the lane): css (210,263); its y/n chips: css (141,331)/(191,331);
# the lane's composer pill: css (850,707) — measured 1415 device px, NOT the screen bottom.
TAP_WAITROW() { echo ${MB_TAP_WAITROW:-"$(( BX1 + $(css 210) )) $(( BY1 + $(css 263) ))"}; }
TAP_YKEY()    { echo ${MB_TAP_YKEY:-"$(( BX1 + $(css 141) )) $(( BY1 + $(css 331) ))"}; }
TAP_COMPOSER(){ echo ${MB_TAP_COMPOSER:-"$(( BX1 + $(css 850) )) $(( BY1 + $(css 707) ))"}; }

arm_prompt() {  # arm_prompt DESK MARKER — a [y/n] prompt the classifier reads, answered by the sheet keys
  perf send "$1" "read -p \"proceed [y/n] \" a; echo \"$2:\$a\"" >/dev/null
  local i; for i in $(seq 1 8); do
    [ "$(perf needs-you "$PORT" "$MB_STATE" "$1" 2>/dev/null | awk '{print $1}')" = "True" ] && return 0
    sleep 1
  done
  return 1
}

journey_remote_pair() {
  # 1. install (always -r: the debug build's versionCode is fixed) and reach the listener on loopback
  local inst
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
  local ID
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
}

journey_perf_scroll_type() {
  page_open || return
  local seeded desk
  seeded=$(perf seed "$PORT" "$MB_STATE" "$SEED_CWD" 200) \
    && step seed true "$(echo "$seeded" | jsonf 'print("%(sent)d lane messages in %(ms)d ms, desk %(desk)s" % j)')" \
    || { step seed false "spawn/send refused"; return; }
  desk=$(echo "$seeded" | jsonf 'print(j["desk"])')
  bounds || return
  # a [y/n] prompt makes the desk the waiting row at the tree top — the deterministic
  # way in; tapping the row opens its lane (calibrated: the row card at css (210,263))
  arm_prompt "$desk" "P" && step "waiting row" true "needs_you line" || { step "waiting row" false "prompt not classified"; return; }
  sleep "$PAGE_WAIT"   # the page's desks cadence, then the waiting row renders
  shot waiting-row
  A shell input tap $(TAP_WAITROW); sleep 3
  shot lane-open
  # answer the prompt now: a pending one pins the lane to the newest message (the bottom-pin
  # bites while events arrive), and answering through the y chip is one more calibrated tap
  A shell input tap $(TAP_YKEY); sleep 2
  # the scroll session: slow drags on the bubble column (the gesture the by-hand walk proved:
  # slow and short, not a fling), framed by a Vsync watermark; the shots show the lane moved
  local mark; mark=$(perf watermark "$ADB" "$SERIAL" "$PKG")
  local i; for i in $(seq 1 6); do A shell input swipe $(( BX1 + $(css 400) )) $(( BY1 + $(css 450) )) $(( BX1 + $(css 400) )) $(( BY1 + $(css 250) )) 1500; sleep 0.5; done
  A shell input swipe $(( BX1 + $(css 400) )) $(( BY1 + $(css 450) )) $(( BX1 + $(css 400) )) $(( BY1 + $(css 150) )) 120
  sleep 2   # let the last fling settle before the dump
  perf frames "$ADB" "$SERIAL" "$PKG" "$OUT/frames-scroll.txt" >/dev/null
  shot after-scroll
  local fps deltas; fps=$(perf fps "$OUT/frames-scroll.txt" "$mark"); deltas=$(echo "$fps" | jsonf 'print(j["deltas"])')
  [ "${deltas:-0}" -ge 10 ] && step "scroll fps" true "median $(echo "$fps" | jsonf 'print("%.1f" % j["fps_median"])') fps over $deltas frame deltas (gesture-path render; visible scroll unverified — see after-scroll.png)" \
    || step "scroll fps" false "only ${deltas:-0} frame deltas — the lane did not render for the flings; see shots"
  # the typing session: the composer pill (css (850,707) — measured, not the screen bottom),
  # 20 keys, the frames that consumed them
  A shell input tap $(TAP_COMPOSER); sleep 1
  local mark2; mark2=$(perf watermark "$ADB" "$SERIAL" "$PKG")
  perf keys "$ADB" "$SERIAL" 20 300 > "$OUT/keys.json"
  sleep 2
  perf frames "$ADB" "$SERIAL" "$PKG" "$OUT/frames-type.txt" >/dev/null
  local lat matched; lat=$(perf type-latency "$OUT/frames-type.txt" "$OUT/keys.json" "$mark2"); matched=$(echo "$lat" | jsonf 'print(j["matched_frames"])')
  [ "${matched:-0}" -ge 10 ] && step "typing latency" true "$(echo "$lat" | jsonf 'print("p50 %.1f ms, p95 %.1f ms (handle-input to present, one clock), %d frames for %d keys, worst injection window %.1f ms" % (j["p50_ms"], j["p95_ms"], j["matched_frames"], j["keys"], j["window_ms_max"]))')" \
    || step "typing latency" false "only ${matched:-0} input frames for 20 keys — did the composer have focus? see shots"
}

journey_sheet_taps() {
  page_open || return
  local desk; desk=$(perf spawn "$PORT" "$MB_STATE" "$SEED_CWD" 2>/dev/null | jsonf 'print(j["desk"])')
  [ -n "$desk" ] && step "shell desk" true "$desk" || { step "shell desk" false "spawn refused"; return; }
  bounds || return
  # the waiting row's own y chip (css (141,331)): the prompt re-arms each round, the tap is
  # where a finger goes, and the desk's pty says whether the answer landed — the miss rate
  # is the repro data, a screenshot per tap
  local hits=0 i k hit what
  for i in $(seq 1 10); do
    perf send "$desk" "" >/dev/null; sleep 1   # flush a read the last miss left pending
    arm_prompt "$desk" "H$i" || { step "tap y $i" false "prompt would not arm"; continue; }
    sleep "$PAGE_WAIT"   # the page's desks cadence, then the chips render
    shot "tap-$i-before"
    A shell input tap $(TAP_YKEY)
    hit=false; what=MISSED
    for k in $(seq 1 8); do
      perf tail "$desk" 2>/dev/null | grep -q "^H$i:y" && { hit=true; break; }
      perf tail "$desk" 2>/dev/null | grep -q "^H$i:n" && { what="hit the n chip"; break; }
      sleep 1
    done
    shot "tap-$i-after"
    [ "$hit" = true ] && hits=$((hits + 1))
    step "tap y $i" true "$([ "$hit" = true ] && echo landed || echo "$what")"
  done
  [ "$hits" -ge 1 ] && step "sheet taps" true "$hits of 10 y taps landed (miss rate is the repro data; shots per tap)" \
    || step "sheet taps" false "0 of 10 landed — the tap coordinates are off, not the strip; see shots"
}

journey_$JOURNEY
A logcat -d 2>/dev/null | grep -E "chromium|Console|WebView" | grep -iE "error|uncaught|failed" | tail -15 > "$OUT/webview-errors.txt" || true
printf '{"summary": true, "ts": %s, "session": "%s", "pass": %s, "fail": %s, "skipped": 0, "invalid": 0, "budget_misses": [], "caps": {}, "build": "", "model_source": "none", "device": {"model": "%s", "os": "android", "abi": "%s"}, "mode": "remote"}\n' \
  "$(date +%s)" "$(basename "$OUT")" "$(grep -c '"status": "pass"' "$REPORT")" "$(grep -c '"status": "fail"' "$REPORT")" \
  "$(A shell getprop ro.product.model 2>/dev/null | tr -d '\r')" "$(A shell getprop ro.product.cpu.abi 2>/dev/null | tr -d '\r')" >> "$REPORT"
python3 "$APP/tools/selftest_report.py" "$REPORT" --md "$OUT/report.md" >/dev/null || true
echo "report: $OUT/report.md (shots beside it)"
exit $FAILED
