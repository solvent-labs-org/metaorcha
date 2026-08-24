.PHONY: help install clean test test-all lint format docker-up docker-down docker-dev-up docker-dev-down migrate migrate-dev migrate-reset db-indices prisma-generate grpc-generate dev dev-watch seed check setup ci test-manifest-server test-manifest-server-stop pnd-dev pnd-dev-watch pnd-test pnd-test-unit pnd-test-cov pnd-db-init kafka-up kafka-down kafka-topics sa-dev sa-dev-watch sa-test sa-test-unit sa-test-cov redis-up redis-down chat gw-dev gw-dev-watch gw-test run-all run-all-quick stop-all

# Colors for output
BLUE := \033[0;34m
GREEN := \033[0;32m
RED := \033[0;31m
YELLOW := \033[0;33m
RESET := \033[0m

# Service configuration (default: registry)
s ?= registry
SERVICE_PATH = services/$(s)

# Port parameter — override with p=8001 for planning-discovery
p ?= 8000

# Compose file selection:
#   make docker-up          → deploy/docker-compose.local.yml (infra + registry + pnd; services run on host)
#   make docker-dev-up      → deploy/docker-compose.dev.yml   (full stack including superagent + gateway)
DC_LOCAL := docker compose -f deploy/docker-compose.local.yml
DC_DEV   := docker compose -f deploy/docker-compose.dev.yml

help: ## Show this help message
	@printf '$(BLUE)Orcha Development Commands$(RESET)\n\n'
	@printf '$(YELLOW)Usage:$(RESET)\n'
	@printf '  make <target> [s=<service>] [p=<port>]\n\n'
	@printf '$(YELLOW)Available services:$(RESET) registry, planning-discovery\n'
	@printf '$(YELLOW)Default service:$(RESET) registry\n\n'
	@printf '$(YELLOW)Examples:$(RESET)\n'
	@printf '  make test                    # Test registry (default)\n'
	@printf '  make test s=registry         # Test registry (explicit)\n'
	@printf '  make test-all                # Test all services (registry, pnd, superagent)\n'
	@printf '  make pnd-test                # Test planning-discovery\n'
	@printf '  make dev s=registry          # Registry dev server (port 8000)\n'
	@printf '  make dev s=planning-discovery p=8001  # PnD dev server\n'
	@printf '  make pnd-dev                 # PnD dev server shortcut\n'
	@printf '  make kafka-up                # Start Kafka broker\n'
	@printf '  make pnd-db-init             # Create vector indices\n\n'
	@printf '$(YELLOW)Targets:$(RESET)\n'
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | grep -v "^services" | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  $(GREEN)%-22s$(RESET) %s\n", $$1, $$2}'

install: ## Install all dependencies
	@printf '$(BLUE)Installing dependencies...$(RESET)\n'
	@uv sync --all-packages || (printf '$(RED)✗ Failed to install dependencies$(RESET)\n' && exit 1)
	@printf '$(GREEN)✓ Dependencies installed$(RESET)\n'

clean: ## Clean build artifacts and cache
	@printf '$(BLUE)Cleaning build artifacts...$(RESET)\n'
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
	find . -type f -name "*.pyo" -delete
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".ruff_cache" -exec rm -rf {} + 2>/dev/null || true
	rm -rf .coverage coverage.xml htmlcov/
	@printf '$(GREEN)✓ Cleaned$(RESET)\n'

prisma-generate: ## Generate Prisma client and fetch query engine binary
	@printf '$(BLUE)Generating Prisma client...$(RESET)\n'
	@uv run prisma generate --schema common/database/schema.prisma || (printf '$(RED)✗ Failed to generate Prisma client. Check that common/database/schema.prisma exists and is valid.$(RESET)\n' && exit 1)
	@printf '$(BLUE)Fetching Prisma query engine binary...$(RESET)\n'
	@uv run prisma py fetch || (printf '$(RED)✗ Failed to fetch Prisma engine binary$(RESET)\n' && exit 1)
	@printf '$(GREEN)✓ Prisma client generated$(RESET)\n'

grpc-generate: ## Generate gRPC stubs
	@printf '$(BLUE)Generating gRPC stubs...$(RESET)\n'
	@cd common/proto && uv run python -m grpc_tools.protoc \
		-I./src \
		--python_out=./src \
		--grpc_python_out=./src \
		./src/registry.proto || (printf '$(RED)✗ Failed to generate gRPC stubs. Check that common/proto/src/*.proto files exist and are valid.$(RESET)\n' && exit 1)
	@if [ "$$(uname)" = "Darwin" ]; then \
		sed -i '' 's/^import registry_pb2/from . import registry_pb2/' common/proto/src/registry_pb2_grpc.py; \
	else \
		sed -i 's/^import registry_pb2/from . import registry_pb2/' common/proto/src/registry_pb2_grpc.py; \
	fi
	@printf '$(GREEN)✓ gRPC stubs generated$(RESET)\n'

migrate: ## Run database migrations + vector indices (requires DATABASE_URL)
	@printf '$(BLUE)Running database migrations...$(RESET)\n'
	@uv run prisma migrate deploy --schema common/database/schema.prisma || (printf '$(RED)✗ Failed to run migrations. Check DATABASE_URL environment variable and database connectivity.$(RESET)\n' && exit 1)
	@printf '$(GREEN)✓ Migrations applied$(RESET)\n'
	@$(MAKE) --no-print-directory db-indices

migrate-dev: ## Create and apply new migration
	@printf '$(BLUE)Creating new migration...$(RESET)\n'
	@read -p "Migration name: " name; \
	uv run prisma migrate dev --name $$name --schema common/database/schema.prisma
	@printf '$(GREEN)✓ Migration created and applied$(RESET)\n'

migrate-reset: ## Reset database then re-apply migrations + indices (WARNING: destructive)
	@printf '$(BLUE)Resetting database...$(RESET)\n'
	uv run prisma migrate reset --force --schema common/database/schema.prisma
	@printf '$(GREEN)✓ Database reset and migrations applied$(RESET)\n'
	@$(MAKE) --no-print-directory db-indices

db-indices: ## Apply PnD vector indices + search triggers (idempotent — safe to re-run)
	@printf '$(BLUE)Applying vector indices + full-text search triggers...$(RESET)\n'
	@[ -n "$$DATABASE_URL" ] || (printf '$(RED)✗ DATABASE_URL is not set$(RESET)\n' && exit 1)
	@psql "$$DATABASE_URL" -v ON_ERROR_STOP=1 \
		-f services/planning-discovery/scripts/db/001_create_vector_indices.sql \
		> /dev/null \
		&& printf '$(GREEN)✓ Vector indices applied$(RESET)\n' \
		|| (printf '$(RED)✗ Failed to apply vector indices$(RESET)\n' && exit 1)

lint: ## Run linting checks
	@printf '$(BLUE)Running linting...$(RESET)\n'
	uv run ruff check --fix services/ common/
	@printf '$(GREEN)✓ Linting passed$(RESET)\n'

lint-fix: ## Fix linting issues automatically
	@printf '$(BLUE)Fixing linting issues...$(RESET)\n'
	uv run ruff check --fix services/ common/
	@printf '$(GREEN)✓ Linting issues fixed$(RESET)\n'

format: ## Format code with ruff
	@printf '$(BLUE)Formatting code...$(RESET)\n'
	uv run ruff format services/ common/
	@printf '$(GREEN)✓ Code formatted$(RESET)\n'

format-check: ## Check code formatting
	@printf '$(BLUE)Checking code formatting...$(RESET)\n'
	uv run ruff format --check services/ common/
	@printf '$(GREEN)✓ Formatting check passed$(RESET)\n'

test: ## Run all tests (SERVICE=registry by default)
	@printf '$(BLUE)Running tests for $(s)...$(RESET)\n'
	@uv run pytest $(SERVICE_PATH)/tests/ -v || (printf '$(RED)✗ Tests failed. Check the output above for details.$(RESET)\n' && exit 1)
	@printf '$(GREEN)✓ Tests passed$(RESET)\n'

test-all: ## Run tests for all services (registry, planning-discovery, superagent, gateway)
	@printf '$(BLUE)Running registry tests...$(RESET)\n'
	@uv run pytest services/registry/tests/ -v || (printf '$(RED)✗ Registry tests failed$(RESET)\n' && exit 1)
	@printf '$(GREEN)✓ Registry tests passed$(RESET)\n'
	@printf '$(BLUE)Running Planning & Discovery tests...$(RESET)\n'
	@cd services/planning-discovery && uv run pytest tests/ -v || (printf '$(RED)✗ PnD tests failed$(RESET)\n' && exit 1)
	@printf '$(GREEN)✓ Planning & Discovery tests passed$(RESET)\n'
	@printf '$(BLUE)Running SuperAgent tests...$(RESET)\n'
	@cd services/superagent && uv run pytest tests/ -v || (printf '$(RED)✗ SuperAgent tests failed$(RESET)\n' && exit 1)
	@printf '$(GREEN)✓ SuperAgent tests passed$(RESET)\n'
	@printf '$(BLUE)Running Gateway tests...$(RESET)\n'
	@cd services/gateway && uv run pytest tests/ -v || (printf '$(RED)✗ Gateway tests failed$(RESET)\n' && exit 1)
	@printf '$(GREEN)✓ Gateway tests passed$(RESET)\n'
	@printf '$(GREEN)✓ All service tests passed$(RESET)\n'

test-cov: ## Run tests with coverage (SERVICE=registry by default)
	@printf '$(BLUE)Running tests with coverage for $(s)...$(RESET)\n'
	uv run pytest $(SERVICE_PATH)/tests/ \
		--cov=$(SERVICE_PATH)/src \
		--cov-report=html \
		--cov-report=term \
		-v
	@printf '$(GREEN)✓ Coverage report generated in htmlcov/$(RESET)\n'

test-unit: ## Run unit tests only (SERVICE=registry by default)
	@printf '$(BLUE)Running unit tests for $(s)...$(RESET)\n'
	uv run pytest $(SERVICE_PATH)/tests/ -m unit -v
	@printf '$(GREEN)✓ Unit tests passed$(RESET)\n'

test-integration: ## Run integration tests only (SERVICE=registry by default)
	@printf '$(BLUE)Running integration tests for $(s)...$(RESET)\n'
	uv run pytest $(SERVICE_PATH)/tests/ -m integration -v
	@printf '$(GREEN)✓ Integration tests passed$(RESET)\n'

docker-up: ## Start local infra (postgres, redis, kafka, registry, pnd) — services run on host
	@printf '$(BLUE)Starting local infra services...$(RESET)\n'
	$(DC_LOCAL) up -d
	@printf '$(GREEN)✓ Local infra started$(RESET)\n'
	@printf 'Registry API:              http://localhost:8000\n'
	@printf 'Planning & Discovery API:  http://localhost:8001\n'
	@printf 'gRPC Server:               localhost:50051\n'
	@printf 'Kafka:                     localhost:9092\n'
	@printf 'Postgres:                  localhost:5432\n'
	@printf 'Redis:                     localhost:6379\n'
	@printf '(run $(YELLOW)make sa-dev$(RESET) and $(YELLOW)make gw-dev$(RESET) to start remaining services locally)\n'

docker-down: ## Stop local infra services
	@printf '$(BLUE)Stopping local infra services...$(RESET)\n'
	$(DC_LOCAL) down
	@printf '$(GREEN)✓ Local infra stopped$(RESET)\n'

docker-dev-up: ## Start full stack in Docker (includes superagent + gateway)
	@printf '$(BLUE)Starting full Docker stack...$(RESET)\n'
	$(DC_DEV) up -d
	@printf '$(GREEN)✓ Full stack started$(RESET)\n'
	@printf 'Gateway API:               http://localhost:8080\n'
	@printf 'Registry API:              http://localhost:8000\n'
	@printf 'Planning & Discovery API:  http://localhost:8001\n'
	@printf 'SuperAgent API:            http://localhost:8002\n'
	@printf 'gRPC Server:               localhost:50051\n'
	@printf 'Kafka:                     localhost:9092\n'

docker-dev-down: ## Stop full Docker stack
	@printf '$(BLUE)Stopping full Docker stack...$(RESET)\n'
	$(DC_DEV) down
	@printf '$(GREEN)✓ Full stack stopped$(RESET)\n'

docker-logs: ## View local infra logs (use COMPOSE=dev for full stack)
	$(DC_LOCAL) logs -f

docker-build: ## Build local infra docker images
	@printf '$(BLUE)Building local infra images...$(RESET)\n'
	$(DC_LOCAL) build
	@printf '$(GREEN)✓ Images built$(RESET)\n'

docker-clean: ## Stop and remove all containers, networks, and volumes (local infra)
	@printf '$(BLUE)Cleaning docker resources...$(RESET)\n'
	$(DC_LOCAL) down -v --remove-orphans
	@printf '$(GREEN)✓ Docker resources cleaned$(RESET)\n'

dev: prisma-generate grpc-generate ## Run development server (s=registry, p=8000 by default)
	@printf '$(BLUE)Starting development server for $(s) on port $(p)...$(RESET)\n'
	@printf '$(YELLOW)Debug logging enabled - check console for DEBUG/WARN/ERROR messages$(RESET)\n'
	PYTHONPATH=$(PWD) uv run uvicorn services.$(s).src.main:app --host 0.0.0.0 --port $(p) --log-level debug

dev-watch: prisma-generate grpc-generate ## Run dev server with auto-reload (s=registry, p=8000 by default)
	@printf '$(BLUE)Starting development server for $(s) with auto-reload on port $(p)...$(RESET)\n'
	@printf '$(YELLOW)Debug logging enabled - check console for DEBUG/WARN/ERROR messages$(RESET)\n'
	PYTHONPATH=$(PWD) uv run uvicorn services.$(s).src.main:app --reload --host 0.0.0.0 --port $(p) --log-level debug

kafka-up: ## Start Kafka broker (KRaft mode, no Zookeeper)
	@printf '$(BLUE)Starting Kafka...$(RESET)\n'
	$(DC_LOCAL) up -d orcha-kafka
	@printf '$(GREEN)✓ Kafka started on localhost:9092$(RESET)\n'

kafka-down: ## Stop Kafka broker
	@printf '$(BLUE)Stopping Kafka...$(RESET)\n'
	$(DC_LOCAL) stop orcha-kafka
	@printf '$(GREEN)✓ Kafka stopped$(RESET)\n'

kafka-topics: ## Create all required Kafka topics
	@printf '$(BLUE)Creating Kafka topics...$(RESET)\n'
	@docker exec orcha-kafka bash -c '\
		for topic in registry.agent.registered gateway.user.query planning.manifest.created planning.validation.failed execution.step_complete; do \
			kafka-topics.sh --bootstrap-server localhost:9092 --create --if-not-exists \
				--topic $$topic --partitions 1 --replication-factor 1; \
		done'
	@printf '$(GREEN)✓ Kafka topics created$(RESET)\n'

pnd-db-init: ## Initialise PnD vector indices (run after migrate)
	@printf '$(BLUE)Initialising Planning & Discovery vector indices...$(RESET)\n'
	@uv run python services/planning-discovery/scripts/db/initialize_database.py || \
		(printf '$(RED)✗ Failed to initialise DB. Ensure DATABASE_URL is set and migrate has been run.$(RESET)\n' && exit 1)
	@printf '$(GREEN)✓ Vector indices ready$(RESET)\n'

pnd-dev: prisma-generate ## Start Planning & Discovery dev server (port 8001)
	@printf '$(BLUE)Starting Planning & Discovery service on port 8001...$(RESET)\n'
	PYTHONPATH=$(CURDIR) uv run uvicorn planning_discovery.main:app --app-dir services/planning-discovery/src --host 0.0.0.0 --port 8001 --log-level debug

pnd-dev-watch: prisma-generate ## Start Planning & Discovery dev server with auto-reload
	@printf '$(BLUE)Starting Planning & Discovery service with auto-reload on port 8001...$(RESET)\n'
	PYTHONPATH=$(CURDIR) uv run uvicorn planning_discovery.main:app --app-dir services/planning-discovery/src --reload --host 0.0.0.0 --port 8001 --log-level debug

pnd-test: ## Run Planning & Discovery tests
	@printf '$(BLUE)Running Planning & Discovery tests...$(RESET)\n'
	@cd services/planning-discovery && uv run pytest tests/ -v || (printf '$(RED)✗ Tests failed$(RESET)\n' && exit 1)
	@printf '$(GREEN)✓ Planning & Discovery tests passed$(RESET)\n'

pnd-test-unit: ## Run Planning & Discovery unit tests only
	@printf '$(BLUE)Running Planning & Discovery unit tests...$(RESET)\n'
	@cd services/planning-discovery && uv run pytest tests/ -m unit -v
	@printf '$(GREEN)✓ Unit tests passed$(RESET)\n'

pnd-test-cov: ## Run Planning & Discovery tests with coverage
	@printf '$(BLUE)Running Planning & Discovery tests with coverage...$(RESET)\n'
	@cd services/planning-discovery && uv run pytest tests/ \
		--cov=src \
		--cov-report=html \
		--cov-report=term \
		-v
	@printf '$(GREEN)✓ Coverage report generated in htmlcov/$(RESET)\n'

seed: ## Seed registry with test fixture agents (registry must be running)
	@printf '$(BLUE)Seeding registry with fixture agents...$(RESET)\n'
	@printf '$(YELLOW)Requires: registry running (make dev s=registry) + KAFKA_ENABLED=true in registry .env for PnD indexing$(RESET)\n'
	uv run python services/registry/scripts/seed_agents.py

seed-live: ## Register fleet agents from agents/*/emerge.yaml (registry + agents must be running)
	@printf '$(BLUE)Registering fleet agents from agents/*/emerge.yaml...$(RESET)\n'
	@printf '$(YELLOW)Requires: registry running + HTTP agents up (make run-all)$(RESET)\n'
	@./scripts/seed-live-agents.sh --embeddings

check: lint format-check test-all ## Run all checks (lint, format, test)
	@printf '$(GREEN)✓ All checks passed$(RESET)\n'

setup: install prisma-generate grpc-generate migrate ## Initial project setup
	@printf '$(GREEN)✓ Setup complete$(RESET)\n\n'
	@printf 'Next steps:\n'
	@printf '  1. Copy .env.example to .env and configure\n'
	@printf '  2. Run: make dev\n'
	@printf '  3. Visit: http://localhost:8000/docs\n'

ci: lint format-check test-all ## Run CI checks locally
	@printf '$(GREEN)✓ CI checks passed$(RESET)\n'

test-manifest-server: ## Start test manifest server for API testing
	@printf '$(BLUE)Starting test manifest server...$(RESET)\n'
	@printf '$(YELLOW)Server: http://localhost:9000$(RESET)\n'
	@printf '$(YELLOW)Endpoints:$(RESET)\n'
	@printf '  - /manifest?type=mcp  (returns MCP manifest)\n'
	@printf '  - /manifest?type=a2a  (returns A2A manifest)\n'
	@printf '  - /health             (health check)\n\n'
	uv run python services/registry/tests/manifest_server.py

test-manifest-server-stop: ## Stop test manifest server
	@pkill -f "manifest_server.py" || true
	@printf '$(GREEN)✓ Test manifest server stopped$(RESET)\n'

# ── SuperAgent (port 8002) ────────────────────────────────────────────────────

sa-dev: prisma-generate ## Start SuperAgent dev server on port 8002
	@printf '$(BLUE)Starting SuperAgent dev server...$(RESET)\n'
	@printf '$(YELLOW)Requires: Redis (make redis-up) and PnD service (make pnd-dev)$(RESET)\n'
	PYTHONPATH=$(CURDIR) uv run uvicorn superagent.main:app --app-dir services/superagent/src --host 0.0.0.0 --port 8002 --log-level debug

sa-dev-watch: prisma-generate ## Start SuperAgent with auto-reload
	@printf '$(BLUE)Starting SuperAgent dev server with auto-reload...$(RESET)\n'
	PYTHONPATH=$(CURDIR) uv run uvicorn superagent.main:app --app-dir services/superagent/src --reload --host 0.0.0.0 --port 8002 --log-level debug

sa-test: ## Run SuperAgent tests
	@printf '$(BLUE)Running SuperAgent tests...$(RESET)\n'
	@cd services/superagent && uv run --no-sync pytest tests/ -v || (printf '$(RED)✗ SuperAgent tests failed$(RESET)\n' && exit 1)
	@printf '$(GREEN)✓ SuperAgent tests passed$(RESET)\n'

sa-test-unit: ## Run SuperAgent unit tests only
	@printf '$(BLUE)Running SuperAgent unit tests...$(RESET)\n'
	cd services/superagent && uv run --no-sync pytest tests/unit/ -v

sa-test-cov: ## Run SuperAgent tests with coverage
	@printf '$(BLUE)Running SuperAgent tests with coverage...$(RESET)\n'
	cd services/superagent && uv run --no-sync pytest tests/ \
		--cov=src/superagent \
		--cov-report=html \
		--cov-report=term \
		-v

# ── Redis (SuperAgent checkpointing) ─────────────────────────────────────────

redis-up: ## Start Redis for SuperAgent session state
	@printf '$(BLUE)Starting Redis...$(RESET)\n'
	$(DC_LOCAL) up -d redis
	@printf '$(GREEN)✓ Redis started at localhost:6379$(RESET)\n'

redis-down: ## Stop Redis
	@printf '$(BLUE)Stopping Redis...$(RESET)\n'
	$(DC_LOCAL) stop redis
	@printf '$(GREEN)✓ Redis stopped$(RESET)\n'

# ── Test Agents (SuperAgent integration testing) ──────────────────────────────


chat: ## Open SuperAgent CLI chat (requires sa-dev to be running)
	@printf '$(BLUE)Starting SuperAgent CLI chat (port 8002)...$(RESET)\n'
	uv run python services/superagent/cli/chat.py --port 8002

# ── Gateway (port 8080) ───────────────────────────────────────────────────────

gw-dev: prisma-generate ## Start Gateway dev server on port 8080
	@printf '$(BLUE)Starting Gateway dev server...$(RESET)\n'
	@printf '$(YELLOW)Requires: Postgres, Redis, SuperAgent, and Registry to be running$(RESET)\n'
	PYTHONPATH=$(CURDIR) uv run uvicorn gateway.main:app --app-dir services/gateway/src --host 0.0.0.0 --port 8080 --log-level debug

gw-dev-watch: prisma-generate ## Start Gateway with auto-reload
	@printf '$(BLUE)Starting Gateway dev server with auto-reload...$(RESET)\n'
	PYTHONPATH=$(CURDIR) uv run uvicorn gateway.main:app --app-dir services/gateway/src --reload --host 0.0.0.0 --port 8080 --log-level debug

gw-test: ## Run Gateway tests
	@printf '$(BLUE)Running Gateway tests...$(RESET)\n'
	@cd services/gateway && uv run --no-sync pytest tests/ -v || (printf '$(RED)✗ Gateway tests failed$(RESET)\n' && exit 1)
	@printf '$(GREEN)✓ Gateway tests passed$(RESET)\n'

# ── Full-Stack Orchestrator ───────────────────────────────────────────────────

run-all: ## Start entire stack (infra + codegen + services + seed) in one command
	@printf '$(BLUE)Starting full Orcha stack...$(RESET)\n'
	@bash scripts/run-all.sh

run-all-quick: ## Start stack skipping Docker infra and seeding (services only)
	@printf '$(BLUE)Starting Orcha services (skip infra & seed)...$(RESET)\n'
	@bash scripts/run-all.sh --skip-infra --skip-seed

stop-all: ## Stop all running services and infra
	@printf '$(BLUE)Stopping all Orcha services...$(RESET)\n'
	@pkill -f "uvicorn.*registry" 2>/dev/null || true
	@pkill -f "uvicorn.*planning_discovery" 2>/dev/null || true
	@pkill -f "uvicorn.*superagent" 2>/dev/null || true
	@pkill -f "uvicorn.*gateway" 2>/dev/null || true
	@kill $$(lsof -ti:3000) 2>/dev/null || true
	$(DC_LOCAL) down
	@printf '$(GREEN)✓ All services stopped$(RESET)\n'

run-infra-local:
	docker run --rm \
	  -p 8000:8000 \
	  -e AWS_DEFAULT_REGION=us-east-1 \
	  -e AWS_ACCESS_KEY_ID=$(shell aws configure get aws_access_key_id) \
	  -e AWS_SECRET_ACCESS_KEY=$(shell aws configure get aws_secret_access_key) \
	  orcha-core:local