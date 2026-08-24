#!/usr/bin/env bash
# Register fleet agents from agents/*/emerge.yaml against a running Registry.
#
# Usage:
#   ./scripts/seed-live-agents.sh              # register all fleet agents
#   ./scripts/seed-live-agents.sh --embeddings # also trigger PnD embedding jobs
#
# Requires:
#   - Registry listening at REGISTRY_URL (default http://localhost:8000)
#   - Agent HTTP servers running for live capability harvest (make run-all)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

REGISTRY_URL="${REGISTRY_URL:-http://localhost:8000}"
PND_URL="${PND_URL:-http://localhost:8001}"
WITH_EMBEDDINGS=false

for arg in "$@"; do
  case "$arg" in
    --embeddings) WITH_EMBEDDINGS=true ;;
  esac
done

registered=0
failed=0

for agent_dir in agents/*/; do
  yaml="$agent_dir/emerge.yaml"
  [[ -f "$yaml" ]] || continue
  agent_name=$(basename "$agent_dir")

  result=$(curl -s -w "\n%{http_code}" \
    -F "emerge_yaml=@$yaml" \
    "${REGISTRY_URL}/api/v1/agents/register" 2>/dev/null || echo -e '{}\n000')
  http_code=$(echo "$result" | tail -1)
  body=$(echo "$result" | sed '$d')

  agent_id=$(echo "$body" | python3 -c \
    "import json,sys; print(json.load(sys.stdin).get('data', {}).get('agent_id', ''))" 2>/dev/null || echo "")

  if [[ -n "$agent_id" && "$http_code" =~ ^2 ]]; then
    echo "  registered ${agent_name} -> ${agent_id}"
    registered=$((registered + 1))
    if [[ "$WITH_EMBEDDINGS" == "true" ]]; then
      emb=$(curl -sf -X POST "${PND_URL}/api/v1/manifests/process" \
        -H "Content-Type: application/json" \
        -d "{\"agent_id\":\"$agent_id\"}" 2>/dev/null || echo '{}')
      ok=$(echo "$emb" | python3 -c \
        "import json,sys; print(json.load(sys.stdin).get('success', False))" 2>/dev/null || echo "False")
      [[ "$ok" == "True" ]] && echo "    embeddings ok for ${agent_name}" \
        || echo "    embeddings failed for ${agent_name} (non-fatal)"
    fi
  else
    echo "  failed to register ${agent_name} (HTTP ${http_code})" >&2
    failed=$((failed + 1))
  fi
done

echo "Done: ${registered} registered, ${failed} failed"
[[ "$failed" -eq 0 ]]
