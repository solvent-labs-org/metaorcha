#!/usr/bin/env bash
# ============================================================================
# Orcha — Full Stack Local Dev Runner
#
# Usage:
#   ./scripts/run-all.sh              # Full clean start
#   ./scripts/run-all.sh --skip-infra # Skip Docker/DB setup (services only)
#   ./scripts/run-all.sh --skip-seed  # Skip agent registration + embeddings
# ============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

LOGS="$ROOT/.logs"
mkdir -p "$LOGS"

SKIP_INFRA=false
SKIP_SEED=false
for arg in "$@"; do
  case "$arg" in
    --skip-infra) SKIP_INFRA=true ;;
    --skip-seed)  SKIP_SEED=true ;;
  esac
done

BOLD='\033[1m'; RESET='\033[0m'; RED='\033[0;31m'; GREEN='\033[0;32m'
YELLOW='\033[1;33m'; BLUE='\033[0;34m'; CYAN='\033[0;36m'; DIM='\033[2m'

C_REG='\033[0;33m'; C_PND='\033[0;36m'; C_SA='\033[0;35m'
C_GW='\033[0;34m'; C_FE='\033[0;32m'

info()    { echo -e "${BLUE}[orcha]${RESET} $*"; }
success() { echo -e "${GREEN}     ✓${RESET} $*"; }
warn()    { echo -e "${YELLOW}     ⚠${RESET} $*"; }
fail()    { echo -e "${RED}     ✗${RESET} $*" >&2; exit 1; }
step()    { echo -e "\n${BOLD}${BLUE}── $* ──${RESET}"; }

PIDS=()
SVC_NAMES=()

cleanup() {
  echo ""
  info "Shutting down all services..."
  for i in "${!PIDS[@]}"; do
    pid="${PIDS[$i]}"; name="${SVC_NAMES[$i]:-?}"
    kill -0 "$pid" 2>/dev/null && kill "$pid" 2>/dev/null && echo -e "  ${DIM}stopped ${name}${RESET}"
  done
  sleep 1
  for pid in "${PIDS[@]}"; do kill -9 "$pid" 2>/dev/null || true; done
  wait 2>/dev/null || true
  info "All services stopped."
  exit 0
}
trap cleanup SIGINT SIGTERM

kill_port() {
  local pids; pids=$(lsof -ti:"$1" 2>/dev/null || true)
  [[ -n "$pids" ]] && echo "$pids" | xargs kill 2>/dev/null && sleep 0.3 || true
}

step "Clearing ports"
for port in 8000 8001 8002 8080 3000 3004 3006 3007 3009 3010 3011 4567; do kill_port "$port"; done
success "Ports cleared"

start_service() {
  local name="$1" colour="$2" logfile="$LOGS/${1}.log"; shift 2; : > "$logfile"
  "$@" > >(while IFS= read -r line; do
    echo -e "${colour}${BOLD}[$(printf '%-10s' "$name")]${RESET} ${line}"
    echo "$line" >> "$logfile"
  done) 2>&1 &
  PIDS+=("$!"); SVC_NAMES+=("$name")
  echo -e "  ${DIM}started ${name} (pid $!) → .logs/${name}.log${RESET}"
}

wait_for() {
  local name="$1" url="$2" max="${3:-60}" i=0
  echo -ne "  ${DIM}waiting for ${name}...${RESET}"
  while [[ $i -lt $max ]]; do
    curl -sf --max-time 2 "$url" >/dev/null 2>&1 && {
      echo -e "\r  ${GREEN}✓${RESET} ${name} ready at ${CYAN}${url}${RESET}          "; return 0; }
    sleep 1; i=$((i + 1))
  done
  echo -e "\r  ${RED}✗${RESET} ${name} not ready after ${max}s"; return 1
}

# ═══════════ PHASE 1: Infrastructure ═══════════
if [[ "$SKIP_INFRA" == "false" ]]; then
  step "Phase 1: Docker Infrastructure"

  # --- Core infra (postgres, redis, kafka) — must succeed ---
  info "Starting postgres, redis, kafka..."
  docker compose -f deploy/docker-compose.local.yml up -d postgres redis orcha-kafka >/dev/null 2>&1
  success "Core Docker containers started"

  # --- Ollama — best-effort; native install on host takes precedence ---
  OLLAMA_SOURCE=""
  if curl -sf --max-time 2 http://localhost:11434/api/tags >/dev/null 2>&1; then
    OLLAMA_SOURCE="native"
    success "Ollama already running on host (native install)"
  else
    info "Starting Ollama Docker container..."
    if docker compose -f deploy/docker-compose.local.yml up -d ollama >/dev/null 2>&1; then
      success "Ollama Docker container started"
      OLLAMA_SOURCE="docker"
    else
      warn "Ollama Docker container failed to start (port 11434 conflict?) — embeddings will use fallback"
    fi
  fi

  echo -ne "  ${DIM}waiting for postgres...${RESET}"
  for i in $(seq 1 30); do
    docker exec orcha-postgres pg_isready -U postgres >/dev/null 2>&1 && {
      echo -e "\r  ${GREEN}✓${RESET} Postgres ready          "; break; }
    [[ $i -eq 30 ]] && fail "Postgres did not start in 30s"
    sleep 1
  done

  docker exec orcha-postgres psql -U postgres -d orcha \
    -c "CREATE EXTENSION IF NOT EXISTS vector;" >/dev/null 2>&1
  success "pgvector extension enabled"

  step "Phase 1b: Database Setup"
  info "Running Prisma migrations..."
  DATABASE_URL="postgresql://postgres:postgres@localhost:5432/orcha?schema=public" \
    uv run prisma migrate deploy --schema common/database/schema.prisma \
    >"$LOGS/migrate.log" 2>&1
  success "Prisma migrations applied"

  info "Creating vector indices..."
  DATABASE_URL="postgresql://postgres:postgres@localhost:5432/orcha" \
    uv run python services/planning-discovery/scripts/db/initialize_database.py \
    >"$LOGS/pnd-db-init.log" 2>&1
  success "Vector indices ready"

  info "Creating Kafka topics..."
  docker exec orcha-kafka bash -c '
    for topic in registry.agent.registered gateway.user.query \
                 planning.manifest.created planning.validation.failed \
                 execution.step_complete; do
      kafka-topics.sh --bootstrap-server localhost:9092 --create --if-not-exists \
        --topic $topic --partitions 1 --replication-factor 1 2>/dev/null; done' >/dev/null 2>&1
  success "Kafka topics created"

  # --- Pull embedding model into whichever Ollama is available ---
  info "Pulling Ollama embedding model (nomic-embed-text)..."
  if [[ "$OLLAMA_SOURCE" == "native" ]]; then
    echo -e "  ${DIM}using native ollama${RESET}"
    ollama pull nomic-embed-text >"$LOGS/ollama-pull.log" 2>&1 \
      && success "nomic-embed-text model ready (native)" \
      || warn "Failed to pull nomic-embed-text via native ollama (non-fatal)"
  elif [[ "$OLLAMA_SOURCE" == "docker" ]]; then
    echo -ne "  ${DIM}waiting for ollama container...${RESET}"
    for i in $(seq 1 30); do
      docker exec orcha-ollama curl -sf http://localhost:11434/api/tags >/dev/null 2>&1 && {
        echo -e "\r  ${GREEN}✓${RESET} Ollama container ready          "; break; }
      [[ $i -eq 30 ]] && { warn "Ollama container not ready after 30s — embeddings will fail"; break; }
      sleep 1
    done
    docker exec orcha-ollama ollama pull nomic-embed-text >"$LOGS/ollama-pull.log" 2>&1 \
      && success "nomic-embed-text model ready (docker)" \
      || warn "Failed to pull nomic-embed-text (non-fatal)"
  else
    warn "No Ollama available — embeddings will use fallback search"
  fi
else
  step "Phase 1: Skipped (--skip-infra)"
fi

# ═══════════ PHASE 2: Code Generation ═══════════
step "Phase 2: Code Generation"
info "Generating Prisma client..."
uv run prisma generate --schema common/database/schema.prisma >"$LOGS/prisma-gen.log" 2>&1
uv run prisma py fetch >>"$LOGS/prisma-gen.log" 2>&1
success "Prisma client generated"

info "Generating gRPC stubs..."
make grpc-generate >>"$LOGS/grpc-gen.log" 2>&1
success "gRPC stubs generated"

# ═══════════ PHASE 3: Start Services ═══════════
step "Phase 3: Starting Services"

start_service registry "$C_REG" env PYTHONPATH="$ROOT" \
  uv run uvicorn services.registry.src.main:app --host 0.0.0.0 --port 8000 --log-level info
wait_for registry http://localhost:8000/ 30

start_service pnd "$C_PND" env PYTHONPATH="$ROOT" \
  uv run uvicorn planning_discovery.main:app --app-dir services/planning-discovery/src \
  --host 0.0.0.0 --port 8001 --log-level info
wait_for pnd http://localhost:8001/ 45

start_service superagent "$C_SA" bash -c "set -a && source '$ROOT/services/superagent/.env' && set +a && exec env PYTHONPATH='$ROOT' uv run uvicorn superagent.main:app --app-dir services/superagent/src --host 0.0.0.0 --port 8002 --log-level info 2>&1"
wait_for superagent http://localhost:8002/ 30

# LOCAL_MODE=true → /auth/local issues a persistent single-user session (frictionless
# local login). VITE_LOCAL_MODE flows to the Vite dev server so the UI auto-logs-in.
start_service gateway "$C_GW" env PYTHONPATH="$ROOT" LOCAL_MODE=true \
  uv run uvicorn gateway.main:app --app-dir services/gateway/src \
  --host 0.0.0.0 --port 8080 --log-level info
wait_for gateway http://localhost:8080/ 20

if [[ -d "$ROOT/frontend" ]]; then
  start_service frontend "$C_FE" bash -c "cd '$ROOT/frontend' && VITE_LOCAL_MODE=true npm run dev 2>&1"
  wait_for frontend http://localhost:3000/ 20
fi

# ═══════════ PHASE 3b: Agent Servers ═══════════
step "Phase 3b: Agent Servers"

C_GWS='\033[0;94m'; C_FD='\033[0;92m'

# finance-dashboard-agent — the hero-demo MCP agent (CanvasKit dashboard).
# Not optional: docs/dev_docs/M2-DEMO.md's canonical goal routes here first.
start_service finance-db "$C_FD" bash -c "cd '$ROOT/agents/finance-dashboard-agent' && { [ -f .env ] && set -a && source .env && set +a || true; } ; exec env PORT=3010 uv run python server.py 2>&1"
# WORKSPACE_MCP_PORT default 8100: the agent spawns workspace-mcp on this port,
# and its upstream default (8000) collides with the registry. A value in the
# agent's .env still wins.
start_service gws-orch "$C_GWS" bash -c "cd '$ROOT/agents/google-workspace-orchestrator' && { [ -f .env ] && set -a && source .env && set +a || true; } ; export WORKSPACE_MCP_PORT=\"\${WORKSPACE_MCP_PORT:-8100}\" ; exec uv run uvicorn src.server:app --host 0.0.0.0 --port 3011 --log-level info 2>&1"

wait_for finance-db   http://localhost:3010/health 30 || warn "finance-dashboard-agent not healthy (non-fatal)"
wait_for gws-orch     http://localhost:3011/health 30 || warn "google-workspace-orchestrator not healthy (non-fatal)"


# ═══════════ PHASE 4: Seed Agents ═══════════
if [[ "$SKIP_SEED" == "false" ]]; then
  step "Phase 4: Agent Registration & Embeddings"

  # Hard-delete any existing agents so re-registration works cleanly
  info "Clearing previous agent data..."
  docker exec orcha-postgres psql -U postgres -d orcha -c \
    "DELETE FROM agent_embeddings; DELETE FROM capabilities; DELETE FROM agent_versions; DELETE FROM agents;" \
    >/dev/null 2>&1 || true
  success "Agent tables cleared"

  ./scripts/seed-live-agents.sh --embeddings || warn "Some agents failed to register (non-fatal)"

  counts=$(docker exec orcha-postgres psql -U postgres -d orcha -tAc \
    "SELECT count(*) FROM agents;" 2>/dev/null || echo "?")
  embs=$(docker exec orcha-postgres psql -U postgres -d orcha -tAc \
    "SELECT count(*) FROM agent_embeddings WHERE embedding IS NOT NULL;" 2>/dev/null || echo "?")
  success "DB: ${counts} agents, ${embs} with embeddings"
else
  step "Phase 4: Skipped (--skip-seed)"
fi

# ═══════════ READY ═══════════
echo ""
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
echo -e "${GREEN}${BOLD}  Orcha is running${RESET}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
echo ""
echo -e "  ${BOLD}Frontend${RESET}         →  ${CYAN}http://localhost:3000${RESET}"
echo -e "  ${BOLD}Gateway API${RESET}      →  ${CYAN}http://localhost:8080${RESET}"
echo -e "  ${BOLD}Registry API${RESET}     →  ${CYAN}http://localhost:8000/docs${RESET}"
echo -e "  ${BOLD}PnD API${RESET}          →  ${CYAN}http://localhost:8001/docs${RESET}"
echo -e "  ${BOLD}SuperAgent API${RESET}   →  ${CYAN}http://localhost:8002/docs${RESET}"
echo -e ""
echo -e "  ${BOLD}Finance Dashboard${RESET} →  ${CYAN}http://localhost:3010${RESET}"
echo -e "  ${BOLD}Web Scraper${RESET}      →  ${CYAN}http://localhost:3004${RESET}"
echo -e "  ${BOLD}Search Agent${RESET}     →  ${CYAN}http://localhost:3007${RESET}"
echo -e "  ${BOLD}Notion Research${RESET}  →  ${CYAN}http://localhost:3006${RESET}"
echo -e "  ${BOLD}Lead Gen${RESET}         →  ${CYAN}http://localhost:4567${RESET}"
echo -e "  ${BOLD}Google Workspace${RESET} →  ${CYAN}http://localhost:3011${RESET}"
echo -e "  ${BOLD}Ecomm Automation${RESET} →  ${CYAN}http://localhost:3009${RESET}"
echo -e ""
echo -e "  ${BOLD}Logs${RESET}             →  ${DIM}.logs/<service>.log${RESET}"
echo ""
echo -e "  ${YELLOW}Ctrl+C to stop everything cleanly${RESET}"
echo ""

while true; do
  for i in "${!PIDS[@]}"; do
    pid="${PIDS[$i]}"; name="${SVC_NAMES[$i]}"
    if ! kill -0 "$pid" 2>/dev/null; then
      wait "$pid" 2>/dev/null; rc=$?
      [[ $rc -ne 0 ]] && echo -e "${RED}${BOLD}[crash]${RESET} ${name} exited (code $rc) — .logs/${name}.log"
      unset 'PIDS[$i]'; unset 'SVC_NAMES[$i]'
    fi
  done
  [[ ${#PIDS[@]} -eq 0 ]] && { echo -e "${RED}All services exited.${RESET}"; exit 1; }
  sleep 2
done