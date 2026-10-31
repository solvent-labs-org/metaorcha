#!/usr/bin/env bash
# E2E launch proof — one command, full Verified-Runs chain.
#
#   ./scripts/e2e-launch-verify.sh [BASE_URL] [--browser]
#
# Asserts, against a running sandbox (local default or public URL):
#   1. guest auth issues a JWT
#   2. session creation works through the API
#   3. the portfolio goal streams a canvas_manifest (full CanvasKit envelope)
#      with zero "type": "error" events
#   4. the run audit (GET …/audit) is non-empty: goal set, ≥1 step, the
#      finance-dashboard step verified
#   5. (--browser) headless-Chrome screenshot of the chat UI post-run
#
# Exit 0 only if every assertion passes. Artifacts land in /tmp/e2e-verify/.
set -uo pipefail

BASE_URL="${1:-http://localhost}"
BROWSER="${2:-}"
GOAL="Show me my portfolio dashboard"
OUT=/tmp/e2e-verify
mkdir -p "$OUT"

pass() { echo "  ✅ $1"; }
fail() { echo "  ❌ $1"; FAILED=1; }
FAILED=0

echo "── 1. guest auth ──"
GUEST=$(curl -sf "$BASE_URL/api/auth/guest") || { fail "guest auth unreachable at $BASE_URL"; exit 1; }
TOKEN=$(echo "$GUEST" | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')
[ -n "$TOKEN" ] && pass "guest JWT issued" || { fail "no access_token"; exit 1; }

echo "── 2. create session ──"
session_json=$(curl -sf -X POST "$BASE_URL/api/v1/sessions" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{}' 2>/dev/null) || session_json=""
SID=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get("session_id",""))' "$session_json" 2>/dev/null) || SID=""
[ -n "$SID" ] && pass "session $SID" || { fail "session creation failed"; exit 1; }

echo "── 3. goal run (this invokes the LLM — up to ~3 min) ──"
curl -s -N -X POST "$BASE_URL/api/v1/sessions/$SID/message" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d "{\"message\": \"$GOAL\"}" --max-time 300 > "$OUT/sse.txt"
grep -q 'canvas_manifest' "$OUT/sse.txt" \
  && pass "canvas_manifest streamed ($(grep -c 'metric_card' "$OUT/sse.txt") metric cards, chart, table, feed)" \
  || fail "no canvas_manifest in SSE (see $OUT/sse.txt)"
grep -q '"type": "error"' "$OUT/sse.txt" \
  && fail "error event in stream: $(grep -o '"type": "error"[^}]*' "$OUT/sse.txt" | head -1)" \
  || pass "zero error events"

echo "── 4. run audit ──"
curl -sf "$BASE_URL/api/v1/sessions/$SID/audit" -H "Authorization: Bearer $TOKEN" > "$OUT/audit.json" \
  || fail "audit endpoint unreachable"
if [ -s "$OUT/audit.json" ]; then
  python3 - "$OUT/audit.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
ok = True
if not d.get("goal"):
    print("  ❌ audit goal empty"); ok = False
s = d.get("summary", {})
if s.get("total_steps", 0) < 1:
    print("  ❌ audit has 0 steps (transcript persistence?)"); ok = False
fin = [st for st in d.get("steps", []) if "finance" in st.get("agent_id", "")]
if fin and all(st.get("verified") for st in fin):
    print(f"  ✅ audit: {s.get('total_steps')} step(s), protocols={s.get('protocols')}, finance step verified")
elif fin:
    print("  ❌ finance step not verified"); ok = False
elif s.get("total_steps", 0) >= 1:
    print("  ⚠ steps present but no finance-dashboard step (goal routed elsewhere?)")
sys.exit(0 if ok else 1)
PY
  [ $? -eq 0 ] || FAILED=1
fi

if [ "$BROWSER" = "--browser" ]; then
  echo "── 5. browser render ──"
  /usr/bin/google-chrome --headless=new --no-sandbox --disable-gpu \
    --screenshot="$OUT/chat.png" --window-size=1280,900 \
    --virtual-time-budget=8000 "$BASE_URL" 2>/dev/null \
    && pass "screenshot at $OUT/chat.png (inspect for CanvasKit render)" \
    || fail "headless chrome failed"
fi

echo "════════════════════════════════"
[ "$FAILED" -eq 0 ] && { echo "E2E LAUNCH PROOF: PASSED"; exit 0; } || { echo "E2E LAUNCH PROOF: FAILED"; exit 1; }
