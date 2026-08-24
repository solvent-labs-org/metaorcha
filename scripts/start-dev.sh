#!/usr/bin/env bash
# ============================================================================
# Orcha — MVP Local Dev Startup
# Starts all services in the correct order.
# Usage: ./scripts/start-dev.sh
# ============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$REPO_ROOT/.venv"
LOGS="$REPO_ROOT/.logs"
PYTHON="$VENV/bin/python"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; RESET='\033[0m'
info()    { echo -e "${BLUE}[orcha]${RESET} $*"; }
success() { echo -e "${GREEN}✓${RESET} $*"; }
warn()    { echo -e "${YELLOW}⚠${RESET} $*"; }
error()   { echo -e "${RED}✗${RESET} $*" >&2; exit 1; }

# ── 1. Prerequisites ──────────────────────────────────────────────────────────
info "Checking prerequisites..."
command -v docker  >/dev/null 2>&1 || error "docker is required"
command -v node    >/dev/null 2>&1 || error "node is required (18+)"
command -v npm     >/dev/null 2>&1 || error "npm is required"
[[ -d "$VENV" ]] || error "Python venv not found at .venv — run: python -m venv .venv && source .venv/bin/activate && pip install -e common/kafka -e common/database -e common/utils -e common/proto -e common/llm"
success "Prerequisites OK"

# ── 2. Copy .env.example → .env if missing ───────────────────────────────────
info "Checking .env files..."
copy_env() {
  local dir="$1"
  if [[ ! -f "$REPO_ROOT/$dir/.env" && -f "$REPO_ROOT/$dir/.env.example" ]]; then
    cp "$REPO_ROOT/$dir/.env.example" "$REPO_ROOT/$dir/.env"
    warn "Created $dir/.env from .env.example — edit it with your API keys"
  fi
}
copy_env services/registry
copy_env services/superagent
copy_env services/planning-discovery
copy_env services/gateway
copy_env services/agent
copy_env frontend

# ── 3. Check OPENROUTER_API_KEY ───────────────────────────────────────────────
PND_ENV="$REPO_ROOT/services/planning-discovery/.env"
if [[ -f "$PND_ENV" ]] && grep -q "^OPENROUTER_API_KEY=sk-or-\.\.\." "$PND_ENV"; then
  warn "Planning & Discovery requires a real OPENROUTER_API_KEY."
  warn "Edit services/planning-discovery/.env and set OPENROUTER_API_KEY=sk-or-..."
  warn "Get a free key at https://openrouter.ai — service will be skipped."
  SKIP_PND=true
else
  SKIP_PND=false
fi

SUPER_ENV="$REPO_ROOT/services/superagent/.env"
if [[ -f "$SUPER_ENV" ]] && grep -q "^OPENROUTER_API_KEY=sk-or-\.\.\." "$SUPER_ENV" && grep -qE "^#?GROQ_API_KEY" "$SUPER_ENV"; then
  warn "SuperAgent has no LLM key — chat will error on inference calls."
  warn "Edit services/superagent/.env and set OPENROUTER_API_KEY or GROQ_API_KEY."
fi

# ── 4. Generate gRPC proto stubs ──────────────────────────────────────────────
info "Generating gRPC stubs..."
PROTO_DIR="$REPO_ROOT/common/proto/src"
if [[ ! -f "$PROTO_DIR/registry_pb2_grpc.py" ]]; then
  "$PYTHON" -m grpc_tools.protoc \
    -I "$PROTO_DIR" \
    --python_out="$PROTO_DIR" \
    --grpc_python_out="$PROTO_DIR" \
    "$PROTO_DIR/registry.proto" \
    "$PROTO_DIR/auth.proto"
  # Fix flat imports
  sed -i 's/^import registry_pb2 as/from common.proto.src import registry_pb2 as/' "$PROTO_DIR/registry_pb2_grpc.py"
  sed -i 's/^import auth_pb2 as/from common.proto.src import auth_pb2 as/' "$PROTO_DIR/auth_pb2_grpc.py"
  success "gRPC stubs generated"
else
  success "gRPC stubs already exist"
fi

# ── 5. Start infrastructure (postgres + redis) ────────────────────────────────
info "Starting Docker infrastructure (postgres + redis)..."
docker-compose -f "$REPO_ROOT/docker-compose.yml" up -d postgres redis
sleep 3
# Enable pgvector + pg_trgm extensions
docker exec orcha-postgres psql -U postgres -d orcha -c \
  "CREATE EXTENSION IF NOT EXISTS vector; CREATE EXTENSION IF NOT EXISTS pg_trgm;" \
  >/dev/null 2>&1 || true
success "Postgres + Redis running"

# ── 6. Apply database schema ──────────────────────────────────────────────────
info "Applying Prisma schema..."
(
  source "$VENV/bin/activate"
  export DATABASE_URL="postgresql://postgres:postgres@localhost:5432/orcha"
  export PYTHONPATH="$REPO_ROOT"
  cd "$REPO_ROOT"
  prisma db push --schema=common/database/schema.prisma --accept-data-loss >/dev/null 2>&1
)
success "Database schema applied"

# ── 7. Start backend services ─────────────────────────────────────────────────
mkdir -p "$LOGS"

start_service() {
  local name="$1"; local dir="$2"; local module="$3"; local port="$4"
  info "Starting $name on :$port ..."
  (
    source "$VENV/bin/activate"
    export PYTHONPATH="$REPO_ROOT"
    # Load service .env if present
    [[ -f "$REPO_ROOT/$dir/.env" ]] && set -a && source "$REPO_ROOT/$dir/.env" && set +a
    cd "$REPO_ROOT/$dir"
    "$PYTHON" -m uvicorn "$module" --host 0.0.0.0 --port "$port" \
      > "$LOGS/$name.log" 2>&1
  ) &
}

# Registry (no LLM needed)
start_service registry  services/registry  src.main:app  8001
sleep 3

# P&D (needs OPENROUTER_API_KEY)
if [[ "$SKIP_PND" == "false" ]]; then
  start_service planning-discovery  services/planning-discovery  src.main:app  8002
  sleep 2
fi

# SuperAgent (LLM optional at startup)
start_service superagent  services/superagent  superagent.main:app  8003
sleep 2

# Agent service
start_service agent  services/agent  src.main:app  8004
sleep 2

# Gateway
start_service gateway  services/gateway  src.main:app  8000

# ── 8. Frontend ───────────────────────────────────────────────────────────────
info "Starting frontend..."
(
  cd "$REPO_ROOT/frontend"
  [[ ! -d node_modules ]] && npm install --silent
  npm run dev > "$LOGS/frontend.log" 2>&1
) &

# ── 9. Health checks ──────────────────────────────────────────────────────────
sleep 6
info "Checking service health..."

check_http() {
  local name="$1" url="$2"
  if python3 -c "import urllib.request; urllib.request.urlopen('$url', timeout=3)" >/dev/null 2>&1; then
    success "$name  →  $url"
  else
    warn "$name not responding at $url (check .logs/$name.log)"
  fi
}

check_http registry      http://localhost:8001/
check_http superagent    http://localhost:8003/health
check_http gateway       http://localhost:8000/health
check_http frontend      http://localhost:8080/

echo ""
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
echo -e "${GREEN}  Orcha MVP is running${RESET}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
echo ""
echo -e "  Frontend      →  ${BLUE}http://localhost:8080${RESET}"
echo -e "  Registry API  →  ${BLUE}http://localhost:8001/api/v1/agents${RESET}"
echo -e "  SuperAgent    →  ${BLUE}http://localhost:8003/health${RESET}"
echo -e "  Logs          →  ${YELLOW}.logs/<service>.log${RESET}"
echo ""
echo -e "  ${YELLOW}Press Ctrl+C to stop all services${RESET}"
echo ""

# Keep script alive and forward signals
trap 'echo ""; info "Stopping services..."; kill 0; exit 0' INT TERM
wait
