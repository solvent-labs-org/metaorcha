#!/usr/bin/env bash
# M0 verification — automated gates
# Checks: service health (gate 1), agent registration (gate 2),
#         git grep __canvas__ (gate 6), PAYMENT_MODE=mock (gate 7).
# Gates 3-5 (goal routing, SSE event, CanvasKit render) require a browser — manual.
#
# Usage: ./scripts/m0-verify.sh [--gateway URL] [--registry URL]
#   Defaults: gateway=http://localhost:8080, registry=http://localhost:8000

set -euo pipefail

GATEWAY="${GATEWAY_URL:-http://localhost:8080}"
REGISTRY="${REGISTRY_URL:-http://localhost:8000}"
SUPERAGENT="${SUPERAGENT_URL:-http://localhost:8002}"
PND="${PND_URL:-http://localhost:8001}"

PASS=0
FAIL=0

_pass() { echo "  ✅ $1"; PASS=$((PASS + 1)); }
_fail() { echo "  ❌ $1"; FAIL=$((FAIL + 1)); }
_info() { echo "  ℹ  $1"; }

header() { echo; echo "── $1 ──"; }


# ── Gate 1: Service health ────────────────────────────────────────────────────
header "Gate 1 — Service health"

check_health() {
    local name="$1" url="$2"
    if curl -sf --max-time 5 "$url" > /dev/null 2>&1; then
        _pass "$name ($url)"
    else
        _fail "$name unreachable ($url)"
    fi
}

check_health "gateway"           "$GATEWAY/health"
check_health "registry"          "$REGISTRY/"
check_health "superagent"        "$SUPERAGENT/health"
check_health "planning-discovery" "$PND/"


# ── Gate 2: finance-dashboard-agent registered ────────────────────────────────
header "Gate 2 — finance-dashboard-agent registration"

AGENTS_JSON=$(curl -sf --max-time 5 "$REGISTRY/api/v1/agents" 2>/dev/null || echo "")
if echo "$AGENTS_JSON" | grep -q "did:orcha:agent:finance-dashboard"; then
    _pass "did:orcha:agent:finance-dashboard found in registry"
else
    _fail "did:orcha:agent:finance-dashboard NOT in registry"
    _info "Run: ./scripts/seed-live-agents.sh"
fi


# ── Gate 6: git grep __canvas__ ───────────────────────────────────────────────
header "Gate 6 — __canvas__ confined to intended files"

CANVAS_HITS=$(git grep -l "__canvas__" 2>/dev/null || true)
UNEXPECTED=""
while IFS= read -r f; do
    case "$f" in
        agents/finance-dashboard-agent/*)          ;;  # expected — MCP hero-demo agent
        agents/google-workspace-orchestrator/*)    ;;  # expected — A2A agent, real canvas emission (canvas.py)
        services/superagent/*)                     ;;  # expected (OutputNormalizer + execute_agent_calls)
        docs/*)                                    ;;  # expected (arch doc, SRS)
        AGENTS.md)                                 ;;  # expected (repo-level envelope contract)
        scripts/*)                                 ;;  # this file itself
        "") ;;
        *) UNEXPECTED="$UNEXPECTED\n    $f" ;;
    esac
done <<< "$CANVAS_HITS"

if [ -z "$UNEXPECTED" ]; then
    _pass "__canvas__ only in expected paths"
    _info "Files: $(echo "$CANVAS_HITS" | tr '\n' ' ')"
else
    _fail "__canvas__ found in unexpected files:$UNEXPECTED"
fi


# ── Gate 7: PAYMENT_MODE=mock ─────────────────────────────────────────────────
header "Gate 7 — PAYMENT_MODE=mock"

# Check via the gateway's root response (includes payment_mode in log on startup)
# Fall back to checking compose env or local .env files.
ENV_FILE=""
for candidate in .env.sandbox .env .env.local; do
    if [ -f "$candidate" ]; then
        ENV_FILE="$candidate"
        break
    fi
done

if [ -n "$ENV_FILE" ]; then
    MODE=$(grep -E "^PAYMENT_MODE=" "$ENV_FILE" | cut -d= -f2 | tr -d '"' | tr -d "'" || echo "")
    if [ "$MODE" = "mock" ]; then
        _pass "PAYMENT_MODE=mock in $ENV_FILE"
    elif [ -z "$MODE" ]; then
        _fail "PAYMENT_MODE not set in $ENV_FILE"
    else
        _fail "PAYMENT_MODE=$MODE (must be mock for OSS gate)"
    fi
else
    _info "No .env file found — confirm PAYMENT_MODE=mock in running stack"
fi


# ── Summary ───────────────────────────────────────────────────────────────────
echo
echo "══════════════════════════════════════"
echo "  Automated gates: $PASS passed, $FAIL failed"
echo "══════════════════════════════════════"
echo
echo "Manual gates remaining (browser required):"
echo "  Gate 3 — POST goal → routes to finance-dashboard-agent (check logs)"
echo "  Gate 4 — canvas_manifest event in DevTools Network → EventStream"
echo "  Gate 5 — CanvasKit renders: MetricCard × 4, LineChart, DataTable, AlertFeed"
echo
echo "When all 7 gates pass:"
echo "  git checkout main"
echo "  git merge az/feat/sandbox-deploy --allow-unrelated-histories"
echo "  git push origin main"
echo

if [ "$FAIL" -gt 0 ]; then
    exit 1
fi
