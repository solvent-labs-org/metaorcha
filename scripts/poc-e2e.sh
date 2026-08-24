#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# Orcha SYSTEM PoC — end-to-end foundational-operation proof.
#
# Proves the FULL loop with one reproducible script, stage-gated PASS/FAIL:
#
#   1  Infra          — all services healthy, PAYMENT_MODE=mock
#   2  Dev end        — agent registered via the emerge SDK (not curl)
#   3  Discovery      — PnD hybrid search returns the new agent
#   4  Consumer end   — one goal routes to MCP (free) + the paid A2A agent
#   5  Verifier       — every invocation_result carries verified/verdict_reason;
#                       CanvasKit manifest has all 4 component families
#   6  Retry-gate     — injected transient 503 → invocation_retry → verified result
#   7  Settlement     — Transaction row: developer_payout>0, platform_cut>0
#   8  Metrics        — agent execution_count incremented (learning-layer feed)
#   9  Summary        — POC: PROVEN or first failing stage
#
# Usage:
#   ./scripts/poc-e2e.sh
#   GATEWAY_URL=http://localhost/api ./scripts/poc-e2e.sh          # via nginx
#   POC_AGENT_HOST=172.17.0.1 ./scripts/poc-e2e.sh                 # Linux docker
#
# Requirements: sandbox stack up (make -f deploy/sandbox/Makefile up && seed),
# python3, curl. The probe agent runs host-side — POC_AGENT_HOST must be the
# address at which containers can reach this host (host.docker.internal on
# Mac/Windows, usually 172.17.0.1 on Linux).
# ═══════════════════════════════════════════════════════════════════════════
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GATEWAY="${GATEWAY_URL:-http://localhost:8080}"
REGISTRY="${REGISTRY_URL:-http://localhost:8000}"
PND="${PND_URL:-http://localhost:8001}"
AGENT_HOST="${POC_AGENT_HOST:-host.docker.internal}"
PROBE_PORT="${POC_PROBE_PORT:-8930}"
SSE_TIMEOUT="${POC_SSE_TIMEOUT:-180}"
PROBE_DID="did:orcha:agent:poc-probe"

AGENT_PID=""
TMPDIR_POC="$(mktemp -d)"
declare -a STAGE_NAME STAGE_RESULT

cleanup() {
  [[ -n "$AGENT_PID" ]] && kill "$AGENT_PID" 2>/dev/null
  rm -rf "$TMPDIR_POC"
}
trap cleanup EXIT

say()  { echo; echo "━━ Stage $1 — $2 ━━"; }
ok()   { echo "  ✅ $1"; }
bad()  { echo "  ❌ $1"; }

record() { STAGE_NAME+=("$1"); STAGE_RESULT+=("$2"); }

start_agent() { # $1 = "" | --flaky
  [[ -n "$AGENT_PID" ]] && { kill "$AGENT_PID" 2>/dev/null; wait "$AGENT_PID" 2>/dev/null; AGENT_PID=""; }
  PYTHONPATH="$ROOT/sdk/src" python3 "$ROOT/agents/poc-probe-agent/agent.py" ${1:-} \
    > "$TMPDIR_POC/agent.log" 2>&1 &
  AGENT_PID=$!
  for _ in $(seq 1 20); do
    curl -sf "http://localhost:$PROBE_PORT/health" >/dev/null 2>&1 && return 0
    sleep 0.5
  done
  return 1
}

run_goal() { # $1 goal text, $2 output file  → uses $TOKEN, creates a session
  local sid
  sid=$(curl -sf -X POST "$GATEWAY/api/v1/sessions" \
    -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{}' |
    python3 -c "import json,sys; print(json.load(sys.stdin)['session_id'])") || return 1
  curl -sf -N -X POST "$GATEWAY/api/v1/sessions/$sid/message" \
    -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
    -d "{\"message\":\"$1\"}" --max-time "$SSE_TIMEOUT" > "$2" 2>/dev/null || true
  [[ -s "$2" ]]
}

# ─── Stage 1 — Infra ─────────────────────────────────────────────────────────
say 1 "Infra health"
S1=PASS
for svc in "$GATEWAY/health" "$REGISTRY/api/v1/health" "$PND/api/v1/health"; do
  if curl -sf "$svc" >/dev/null 2>&1; then ok "up: $svc"; else bad "down: $svc"; S1=FAIL; fi
done
ENVF=""
for f in "$ROOT/.env.sandbox" "$ROOT/.env" "$ROOT/.env.local"; do [[ -f "$f" ]] && { ENVF="$f"; break; }; done
if [[ -n "$ENVF" ]] && grep -q "PAYMENT_MODE=mock" "$ENVF"; then
  ok "PAYMENT_MODE=mock ($ENVF)"
else
  bad "PAYMENT_MODE=mock not found in env file"; S1=FAIL
fi
record "1 Infra" "$S1"

# ─── Stage 2 — Dev end: emerge SDK registration ─────────────────────────────
say 2 "Dev end — emerge SDK registration"
S2=PASS
if start_agent ""; then
  ok "poc-probe-agent serving on :$PROBE_PORT (emerge SDK server)"
else
  bad "agent failed to start — see $TMPDIR_POC/agent.log"; S2=FAIL
fi
if [[ "$S2" == PASS ]]; then
  # Force a FRESH registration every run. A DID left registered across day-gaps
  # goes UNHEALTHY (registry HealthMonitor, 5-min cycle) and PnD's GIN pre-filter
  # silently drops it from discovery — cascading stages 3/4/6/7 into failure.
  # Soft-delete first: registration purges inactive rows (registration.py::
  # _purge_soft_deleted_agent) and re-creates with a live HEALTHY probe.
  curl -sf -X DELETE "$REGISTRY/api/v1/agents/$PROBE_DID" >/dev/null 2>&1 \
    && ok "prior registration soft-deleted (fresh health probe forced)" \
    || echo "  ℹ  no prior registration to delete (first run)"
  if PYTHONPATH="$ROOT/sdk/src" python3 -m emerge.cli publish \
      "$ROOT/agents/poc-probe-agent/agent.py" \
      --registry "$REGISTRY" --host "$AGENT_HOST" >"$TMPDIR_POC/publish.log" 2>&1; then
    ok "emerge publish → $REGISTRY (SDK client path)"
  elif curl -sf "$REGISTRY/api/v1/agents?limit=100" | grep -q "$PROBE_DID"; then
    ok "emerge publish returned non-zero but $PROBE_DID already registered (prior run)"
  else
    bad "emerge publish failed — $(tail -1 "$TMPDIR_POC/publish.log")"; S2=FAIL
  fi
fi
if [[ "$S2" == PASS ]]; then
  if curl -sf "$REGISTRY/api/v1/agents?limit=100" | grep -q "$PROBE_DID"; then
    ok "registry lists $PROBE_DID"
  else
    bad "registry does not list $PROBE_DID"; S2=FAIL
  fi
  # Health assertion — guards the exact staleness cascade described above.
  curl -sf "$REGISTRY/api/v1/agents?limit=100" > "$TMPDIR_POC/agents-health.json" 2>/dev/null \
    || echo '{}' > "$TMPDIR_POC/agents-health.json"
  if python3 - "$TMPDIR_POC/agents-health.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
data = d if isinstance(d, list) else d.get("data") or d
items = data if isinstance(data, list) else data.get("agents") or []
probe = [a for a in items if "poc-probe" in json.dumps(a)]
assert probe, "poc-probe not in registry list"
status = probe[0].get("health_status", "")
assert status.upper() == "HEALTHY", f"health_status={status!r}"
print("ok")
PY
  then
    ok "health_status=HEALTHY (fresh live probe)"
  else
    bad "poc-probe not HEALTHY — a stale DID (idle across days) gets flipped UNHEALTHY by the registry HealthMonitor and PnD discovery silently excludes it. The soft-delete step above should prevent this; if it recurs, check the DELETE endpoint and registry logs."
    S2=FAIL
  fi
  EMB=$(curl -sf -X POST "$PND/api/v1/manifests/process" \
    -H 'Content-Type: application/json' -d "{\"agent_id\":\"$PROBE_DID\"}" 2>/dev/null || echo '{}')
  if echo "$EMB" | python3 -c "import json,sys; sys.exit(0 if json.load(sys.stdin).get('success') else 1)" 2>/dev/null; then
    ok "PnD embeddings processed"
  else
    bad "PnD manifests/process failed: $EMB"; S2=FAIL
  fi
fi
record "2 SDK registration" "$S2"

# ─── Stage 3 — Discovery ─────────────────────────────────────────────────────
say 3 "Discovery — PnD hybrid search returns the probe agent"
S3=FAIL
CAND=$(curl -sf -X POST "$PND/api/v1/candidates" \
  -H 'Content-Type: application/json' \
  -d '{"query":"Run a system probe report for hello-world","user_id":"poc-harness","top_k":8}' 2>/dev/null || echo '{}')
if echo "$CAND" | grep -q "poc-probe"; then
  ok "candidates include poc-probe"
  S3=PASS
else
  bad "poc-probe not in PnD candidates: $(echo "$CAND" | head -c 300)"
fi
record "3 Discovery" "$S3"

# ─── Auth (shared by stages 4–7) ────────────────────────────────────────────
EMAIL="poc-$(date +%s)@example.com"
TOKEN=$(curl -sf -X POST "$GATEWAY/auth/register" \
  -H 'Content-Type: application/json' \
  -d "{\"email\":\"$EMAIL\",\"password\":\"poc-e2e-pass-123\",\"display_name\":\"PoC Harness\"}" 2>/dev/null |
  python3 -c "import json,sys; print(json.load(sys.stdin)['access_token'])" 2>/dev/null) || TOKEN=""
[[ -z "$TOKEN" ]] && { bad "auth register failed — aborting consumer stages"; record "4 Goal run" FAIL; record "5 Verifier" FAIL; record "6 Retry-gate" FAIL; record "7 Settlement" FAIL; record "8 Metrics" FAIL; }

if [[ -n "$TOKEN" ]]; then

# ─── Stage 4 — Consumer end: multi-protocol goal ────────────────────────────
say 4 "Consumer end — goal routes to finance MCP + paid A2A probe"
S4=FAIL
SSE1="$TMPDIR_POC/sse-main.txt"
GOAL1="Run a system probe report for hello-world, then show me my portfolio dashboard"
run_goal "$GOAL1" "$SSE1"
if python3 - "$SSE1" <<'PY'
import json, sys
events = []
for line in open(sys.argv[1], errors="replace"):
    line = line.strip()
    if not line.startswith("data:"): continue
    p = line[5:].strip()
    if not p or p == "[DONE]": continue
    try: events.append(json.loads(p))
    except json.JSONDecodeError: pass
def has(t, frag):
    return any(e.get("type") == t and frag in str(e.get("agent_id", "")) for e in events)
assert has("invocation_start", "poc-probe"), "no invocation_start for poc-probe"
assert has("invocation_result", "poc-probe"), "no invocation_result for poc-probe"
assert has("invocation_result", "finance-dashboard"), "no invocation_result for finance-dashboard"
probe_results = [e for e in events if e.get("type") == "invocation_result" and "poc-probe" in str(e.get("agent_id",""))]
assert any("PROBE-REPORT" in str(e.get("content_preview","")) for e in probe_results), "probe result content missing PROBE-REPORT"
print("ok")
PY
then
  ok "invocation_start+result for poc-probe (A2A, paid) AND finance-dashboard (MCP)"
  ok "probe result contains PROBE-REPORT"
  S4=PASS
else
  bad "multi-protocol goal did not invoke both agents (see $SSE1)"
fi
record "4 Goal run" "$S4"

# ─── Stage 5 — Verifier assertions ──────────────────────────────────────────
say 5 "Verifier — verified/verdict_reason on results + CanvasKit manifest"
S5=FAIL
if python3 - "$SSE1" <<'PY'
import json, sys
events = []
for line in open(sys.argv[1], errors="replace"):
    line = line.strip()
    if not line.startswith("data:"): continue
    p = line[5:].strip()
    if not p or p == "[DONE]": continue
    try: events.append(json.loads(p))
    except json.JSONDecodeError: pass
results = [e for e in events if e.get("type") == "invocation_result"
           and not str(e.get("agent_id","")).startswith("_")]
assert results, "no external invocation_result events"
for e in results:
    assert "verified" in e, f"invocation_result missing verified: {e.get('agent_id')}"
    assert e.get("verdict_reason"), f"missing verdict_reason: {e.get('agent_id')}"
    assert e["verified"] is True, f"step unverified: {e.get('agent_id')} — {e.get('verdict_reason')}"
required = {"metric_card", "line_chart", "data_table", "alert_feed"}
found = set()
for e in events:
    if e.get("type") == "canvas_manifest":
        for c in (e.get("manifest") or {}).get("components", []):
            found.add(c.get("type",""))
assert required <= found, f"canvas components missing: {required - found}"
print("ok")
PY
then
  ok "every external invocation_result: verified=true + verdict_reason"
  ok "canvas_manifest carries all 4 component families"
  S5=PASS
else
  bad "verifier assertions failed (see $SSE1)"
fi
record "5 Verifier" "$S5"

# ─── Stage 6 — Retry-gate (injected transient failure) ──────────────────────
say 6 "Retry-gate — flaky 503 → invocation_retry → verified result"
S6=FAIL
if start_agent "--flaky"; then
  ok "probe agent restarted in flaky mode (first message/send → 503)"
  SSE2="$TMPDIR_POC/sse-retry.txt"
  run_goal "Run a system probe report for retry-check" "$SSE2"
  if python3 - "$SSE2" <<'PY'
import json, sys
events = []
for line in open(sys.argv[1], errors="replace"):
    line = line.strip()
    if not line.startswith("data:"): continue
    p = line[5:].strip()
    if not p or p == "[DONE]": continue
    try: events.append(json.loads(p))
    except json.JSONDecodeError: pass
retries = [e for e in events if e.get("type") == "invocation_retry"]
assert retries, "no invocation_retry event — retry-gate did not fire"
probe_results = [e for e in events if e.get("type") == "invocation_result"
                 and "poc-probe" in str(e.get("agent_id",""))]
assert probe_results, "no invocation_result for poc-probe after retry"
final = probe_results[-1]
assert final.get("verified") is True, f"post-retry result unverified: {final.get('verdict_reason')}"
assert "PROBE-REPORT" in str(final.get("content_preview","")), "post-retry content missing PROBE-REPORT"
print(f"ok attempts={retries[0].get('attempt')}/{retries[0].get('max_attempts')}")
PY
  then
    ok "invocation_retry emitted, then verified PROBE-REPORT — the ✗→retry→✓ flip"
    S6=PASS
  else
    bad "retry proof failed (see $SSE2 and $TMPDIR_POC/agent.log)"
  fi
else
  bad "flaky agent failed to start"
fi
record "6 Retry-gate" "$S6"

# ─── Stage 7 — Settlement (economic loop) ───────────────────────────────────
say 7 "Settlement — Transaction row with developer_payout for poc-probe"
S7=FAIL
for _ in $(seq 1 15); do
  curl -sf "$GATEWAY/wallet/transactions" -H "Authorization: Bearer $TOKEN" \
    > "$TMPDIR_POC/tx.json" 2>/dev/null || echo '{}' > "$TMPDIR_POC/tx.json"
  if python3 - "$TMPDIR_POC/tx.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
rows = [t for t in d.get("transactions", []) if "poc-probe" in str(t.get("agent_id",""))]
assert rows, "no transaction rows for poc-probe yet"
t = rows[0]
assert float(t["developer_payout"]) > 0, "developer_payout not > 0"
assert float(t["platform_cut"]) > 0, "platform_cut not > 0"
assert abs(float(t["developer_payout"]) + float(t["platform_cut"]) - float(t["base_fee"])) < 1e-9, "split != base_fee"
print(f"ok base_fee={t['base_fee']} dev={t['developer_payout']} platform={t['platform_cut']} status={t['status']}")
PY
  then
    ok "Transaction row: developer_payout + platform_cut = base_fee (80/20 mock split)"
    S7=PASS
    break
  fi
  sleep 2
done
[[ "$S7" == FAIL ]] && bad "no settled Transaction row for poc-probe within 30s"
record "7 Settlement" "$S7"

# ─── Stage 8 — Learning-layer metrics feed ──────────────────────────────────
say 8 "Metrics — execution_count incremented on the agent row"
S8=FAIL
curl -sf "$REGISTRY/api/v1/agents?limit=100" > "$TMPDIR_POC/agents.json" 2>/dev/null \
  || echo '{}' > "$TMPDIR_POC/agents.json"
if python3 - "$TMPDIR_POC/agents.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
data = d if isinstance(d, list) else d.get("data") or d
items = data if isinstance(data, list) else data.get("agents") or []
probe = [a for a in items if "poc-probe" in json.dumps(a)]
assert probe, "poc-probe not in registry list"
a = probe[0]
blob = json.dumps(a)
count = a.get("execution_count")
if count is None and isinstance(a.get("metrics"), dict):
    count = a["metrics"].get("execution_count")
assert count is not None, f"execution_count not exposed on agent row: {blob[:200]}"
assert int(count) >= 1, f"execution_count not incremented: {count}"
print(f"ok execution_count={count}")
PY
then
  ok "execution_count >= 1 — behavioral-metrics path (D2/GNN feed) is live"
  S8=PASS
else
  bad "execution_count not observed on registry agent row"
fi
record "8 Metrics" "$S8"

fi  # TOKEN

# ─── Stage 9 — Summary ──────────────────────────────────────────────────────
echo
echo "═══════════════════════════════════════════════"
echo "  SYSTEM PoC SUMMARY"
echo "═══════════════════════════════════════════════"
FAILED=0
for i in "${!STAGE_NAME[@]}"; do
  r="${STAGE_RESULT[$i]}"
  [[ "$r" == PASS ]] && icon="✅" || { icon="❌"; FAILED=$((FAILED+1)); }
  printf "  %s  %s\n" "$icon" "${STAGE_NAME[$i]}"
done
echo "═══════════════════════════════════════════════"
if [[ "$FAILED" -eq 0 ]]; then
  echo "  POC: PROVEN — full loop verified end-to-end"
  exit 0
fi
echo "  POC: $FAILED stage(s) failed"
exit 1
