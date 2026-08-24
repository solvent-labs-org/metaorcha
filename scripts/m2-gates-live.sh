#!/usr/bin/env bash
# M2 demo gates — live API verification (no browser required).
# Extends M0 gates 3–5 with an A2A + computer-use leg for the canonical M2 goal
# (MCP finance-dashboard + A2A google-workspace-orchestrator + COMPUTER_USE,
# genuinely 3 protocols).
#
# Usage:
#   GATEWAY_URL=http://localhost/api ./scripts/m2-gates-live.sh
#   M2_RUNS=5 M2_PASS=4 ./scripts/m2-gates-live.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GATEWAY="${GATEWAY_URL:-http://localhost:8080}"
GOAL_FILE="$ROOT/scripts/m2-demo-goal.txt"
GOAL="$(tr -d '\n' < "$GOAL_FILE")"
RUNS="${M2_RUNS:-1}"
PASS_THRESHOLD="${M2_PASS:-1}"

_pass() { echo "  ✅ $1"; }
_fail() { echo "  ❌ $1"; }
header() { echo; echo "── $1 ──"; }

analyze_stream() {
  local path="$1"
  local elapsed="$2"
  python3 - "$path" "$elapsed" <<'PY'
import json, sys
path, elapsed = sys.argv[1], int(sys.argv[2])
text = open(path, errors="replace").read()

# Parse invocation_result / canvas_manifest events properly instead of loose
# substring matching — a misconfigured system tool can share a substring with
# an agent name, so string checks alone would pass on a failed call to the
# wrong tool.
successful_tools = set()
for line in text.splitlines():
    if not line.startswith("data:"):
        continue
    payload = line[5:].strip()
    if not payload or payload == "[DONE]":
        continue
    try:
        ev = json.loads(payload)
    except json.JSONDecodeError:
        continue

    if ev.get("type") == "invocation_result" and ev.get("status") == "success":
        successful_tools.add(ev.get("tool_name", ""))

checks = {
    "finance_dashboard_mcp": any(
        "finance-dashboard" in t for t in successful_tools
    ),
    # A2A leg — must be the real delegate call to our agent, not a system
    # tool that happens to share a substring.
    "a2a_gws_orchestrator": "delegate__did_orcha_agent_google-workspace-orchestrator"
    in successful_tools,
    "computer_use": any(
        "computer-use" in t for t in successful_tools
    ),
    "canvas_manifest": "canvas_manifest" in text,
    "no_graph_error": '"type": "error"' not in text,
}
required_types = {"metric_card", "line_chart", "data_table", "alert_feed"}
found_types = set()
for line in text.splitlines():
    if not line.startswith("data:"):
        continue
    payload = line[5:].strip()
    if not payload or payload == "[DONE]":
        continue
    try:
        ev = json.loads(payload)
    except json.JSONDecodeError:
        continue
    if ev.get("type") != "canvas_manifest":
        continue
    manifest = ev.get("manifest") or {}
    for comp in manifest.get("components", []):
        t = comp.get("type", "")
        if t:
            found_types.add(t)
checks["canvas_components"] = required_types <= found_types
ok = all(checks.values())
print(json.dumps({"elapsed_s": elapsed, "checks": checks, "pass": ok}))
sys.exit(0 if ok else 1)
PY
}

run_once() {
  local run_num="$1"
  local OUT
  OUT=$(mktemp)

  local EMAIL="m2gate-${run_num}-$(date +%s)-$$@example.com"
  local REG TOKEN SID HTTP_CODE
  REG=$(curl -sS -o /tmp/m2gate_reg.$$ -w '%{http_code}' -X POST "$GATEWAY/auth/register" \
    -H 'Content-Type: application/json' \
    -d "{\"email\":\"$EMAIL\",\"password\":\"m2gate-test-123\",\"display_name\":\"M2 Gate\"}")
  HTTP_CODE="$REG"
  if [[ "$HTTP_CODE" != "201" ]]; then
    echo "{\"elapsed_s\": 0, \"checks\": {\"register_failed_http\": \"$HTTP_CODE\"}, \"pass\": false}"
    rm -f "$OUT" "/tmp/m2gate_reg.$$"
    return 1
  fi
  TOKEN=$(python3 -c "import json; print(json.load(open('/tmp/m2gate_reg.$$'))['access_token'])" 2>/dev/null)
  rm -f "/tmp/m2gate_reg.$$"
  if [[ -z "$TOKEN" ]]; then
    echo '{"elapsed_s": 0, "checks": {"token_parse_failed": true}, "pass": false}'
    rm -f "$OUT"
    return 1
  fi

  SID=$(curl -sS -o /tmp/m2gate_sess.$$ -w '%{http_code}' -X POST "$GATEWAY/api/v1/sessions" \
    -H "Authorization: Bearer $TOKEN" \
    -H 'Content-Type: application/json' \
    -d '{}')
  HTTP_CODE="$SID"
  if [[ "$HTTP_CODE" != "201" ]]; then
    echo "{\"elapsed_s\": 0, \"checks\": {\"session_create_failed_http\": \"$HTTP_CODE\"}, \"pass\": false}"
    rm -f "$OUT" "/tmp/m2gate_sess.$$"
    return 1
  fi
  SID=$(python3 -c "import json; print(json.load(open('/tmp/m2gate_sess.$$'))['session_id'])" 2>/dev/null)
  rm -f "/tmp/m2gate_sess.$$"
  if [[ -z "$SID" ]]; then
    echo '{"elapsed_s": 0, "checks": {"session_id_parse_failed": true}, "pass": false}'
    rm -f "$OUT"
    return 1
  fi

  local start end elapsed
  start=$(date +%s)
  curl -sf -N -X POST "$GATEWAY/api/v1/sessions/$SID/message" \
    -H "Authorization: Bearer $TOKEN" \
    -H 'Content-Type: application/json' \
    -d "{\"message\":$(python3 -c "import json,sys; print(json.dumps(sys.argv[1]))" "$GOAL")}" \
    --max-time 180 > "$OUT" 2>/dev/null || true
  end=$(date +%s)
  elapsed=$((end - start))

  local rc
  set +e
  analyze_stream "$OUT" "$elapsed"
  rc=$?
  set -e
  rm -f "$OUT"
  return "$rc"
}

header "M2 gates — 3-protocol goal + CanvasKit"
echo "Goal: $GOAL"
echo "Runs: $RUNS (pass if >= $PASS_THRESHOLD succeed)"
echo

PASS_COUNT=0
BEST_ELAPSED=9999
for i in $(seq 1 "$RUNS"); do
  echo "Run $i/$RUNS"
  set +e
  RESULT=$(run_once "$i")
  RC=$?
  set -e
  if [[ "$RC" -eq 0 ]]; then
    PASS_COUNT=$((PASS_COUNT + 1))
    ELAPSED=$(echo "$RESULT" | python3 -c "import json,sys; print(json.load(sys.stdin)['elapsed_s'])")
    _pass "Run $i passed (${ELAPSED}s)"
    if [[ "$ELAPSED" -lt "$BEST_ELAPSED" ]]; then
      BEST_ELAPSED=$ELAPSED
    fi
  else
    _fail "Run $i failed"
    if [[ -n "$RESULT" ]]; then
      echo "$RESULT" | python3 -c '
import json, sys
d = json.loads(sys.stdin.read())
for k, v in d.get("checks", {}).items():
    label = ("ok" if v else "FAIL") if isinstance(v, bool) else str(v)
    print("    " + str(k) + ": " + label)
'
    fi
  fi
  echo
done

echo "══════════════════════════════════════"
echo "  M2 live gates: $PASS_COUNT/$RUNS passed (need >= $PASS_THRESHOLD)"
if [[ "$BEST_ELAPSED" -lt 9999 ]]; then
  echo "  Best wall clock: ${BEST_ELAPSED}s (target <30s for hero clip)"
fi
echo "══════════════════════════════════════"

[[ "$PASS_COUNT" -ge "$PASS_THRESHOLD" ]]
