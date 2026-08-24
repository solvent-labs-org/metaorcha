#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# Orcha DEMO SMOKE — headless proof that the live sandbox hero path works.
#
# Run this 10 minutes before any demo. Non-zero exit = fall back to the
# pre-recorded gif (see WS6 runbook).
#
#   1  Edge        — frontend served, guest JWT issued (nginx → gateway)
#   2  Session     — guest creates a session
#   3  Hero run    — portfolio goal → SSE stream opens → canvas_manifest seen
#   4  Audit       — evidence package retrievable for the run
#
# Uses a disposable guest session only — no account, no prod data mutation.
# Guests are capped at one message (SandboxGuard); this script sends exactly one.
#
# Usage:
#   ./scripts/demo-smoke.sh
#   SANDBOX_URL=https://sandbox.example.com ./scripts/demo-smoke.sh
#   SMOKE_SSE_TIMEOUT=90 ./scripts/demo-smoke.sh
# ═══════════════════════════════════════════════════════════════════════════
set -uo pipefail

BASE="${SANDBOX_URL:-https://sandbox.metaorcha.ai}"
SSE_TIMEOUT="${SMOKE_SSE_TIMEOUT:-120}"
GOAL="${SMOKE_GOAL:-Show me my portfolio dashboard}"

FAILED=0
say() { echo; echo "━━ Stage $1 — $2 ━━"; }
ok()  { echo "  ✅ $1"; }
bad() { echo "  ❌ $1"; FAILED=$((FAILED + 1)); }

TMPDIR_SMOKE="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_SMOKE"' EXIT

# ─── Stage 1 — Edge ──────────────────────────────────────────────────────────
say 1 "Edge — frontend + guest auth"
if curl -sf -o /dev/null --max-time 15 "$BASE/"; then
  ok "frontend served: $BASE/"
else
  bad "frontend unreachable: $BASE/"
fi

TOKEN=$(curl -sf --max-time 20 "$BASE/api/auth/guest" |
  python3 -c "import json,sys; print(json.load(sys.stdin)['access_token'])" 2>/dev/null) || TOKEN=""
if [[ -n "$TOKEN" ]]; then
  ok "guest JWT issued (nginx → /api/auth/* rewrite → gateway)"
else
  bad "guest JWT not issued — gateway/auth path down"
  echo; echo "SMOKE: FAIL — aborting, no token"; exit 1
fi

# ─── Stage 2 — Session ───────────────────────────────────────────────────────
say 2 "Session — guest session create"
SID=$(curl -sf --max-time 20 -X POST "$BASE/api/v1/sessions" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{}' |
  python3 -c "import json,sys; print(json.load(sys.stdin)['session_id'])" 2>/dev/null) || SID=""
if [[ -n "$SID" ]]; then
  ok "session created: $SID"
else
  bad "session create failed"
  echo; echo "SMOKE: FAIL — aborting, no session"; exit 1
fi

# ─── Stage 3 — Hero run ──────────────────────────────────────────────────────
say 3 "Hero run — goal → SSE → canvas_manifest (~30-60s)"
SSE="$TMPDIR_SMOKE/sse.txt"
curl -sf -N --max-time "$SSE_TIMEOUT" -X POST "$BASE/api/v1/sessions/$SID/message" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d "{\"message\":\"$GOAL\"}" > "$SSE" 2>/dev/null || true

if [[ ! -s "$SSE" ]]; then
  bad "SSE stream empty — no events within ${SSE_TIMEOUT}s"
elif grep -q '"canvas_manifest"' "$SSE"; then
  ok "canvas_manifest received — dashboard payload is live"
  grep -q '"invocation_result"' "$SSE" \
    && ok "invocation_result events present" \
    || echo "  ℹ  no invocation_result events (canvas arrived without agent steps)"
else
  bad "SSE streamed but no canvas_manifest (see $SSE)"
fi

# ─── Stage 4 — Audit ─────────────────────────────────────────────────────────
say 4 "Audit — evidence package"
if curl -sf --max-time 20 "$BASE/api/v1/sessions/$SID/audit" \
    -H "Authorization: Bearer $TOKEN" > "$TMPDIR_SMOKE/audit.json" \
    && python3 -c "import json; json.load(open('$TMPDIR_SMOKE/audit.json'))" 2>/dev/null; then
  ok "audit evidence package retrievable"
else
  bad "audit endpoint failed for session $SID"
fi

# ─── Summary ─────────────────────────────────────────────────────────────────
echo
echo "═══════════════════════════════════════════════"
if [[ "$FAILED" -eq 0 ]]; then
  echo "  SMOKE: GREEN — $BASE hero path is demo-ready"
  exit 0
fi
echo "  SMOKE: FAIL ($FAILED stage(s)) — fall back to the gif"
exit 1
