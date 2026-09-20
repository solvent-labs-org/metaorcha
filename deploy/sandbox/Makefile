COMPOSE = docker compose -f deploy/sandbox/docker-compose.sandbox.yml --env-file deploy/sandbox/.env.sandbox
ROOT    = $(shell git rev-parse --show-toplevel)

.PHONY: up down logs status seed check-env pull-models init-db migrate attest-check

ENVF = $(ROOT)/deploy/sandbox/.env.sandbox
# Read one KEY=value from .env.sandbox (last assignment wins; blank if absent).
envval = $(shell grep -E '^$(1)=' $(ENVF) 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '[:space:]')

check-env:
	@test -f $(ENVF) || (echo "ERROR: deploy/sandbox/.env.sandbox not found. Copy deploy/sandbox/.env.sandbox.example and fill in secrets." && exit 1)
	@# Attested profile: mirror the SuperAgent boot guards so a misconfiguration
	@# fails here, with the fix, instead of in a container log.
	@if [ "$(call envval,SETTLEMENT_REQUIRE_ATTESTATION)" = "true" ] && [ "$(call envval,RUN_ATTESTATION_ENABLED)" != "true" ]; then \
	  echo "ERROR: SETTLEMENT_REQUIRE_ATTESTATION=true needs RUN_ATTESTATION_ENABLED=true (otherwise every charged call defers forever). Set both or neither."; exit 1; fi
	@if [ "$(call envval,RUN_ATTESTATION_ENABLED)" = "true" ] && [ -z "$(call envval,ATTESTATION_PRIVATE_KEY_B64)" ] && [ "$(call envval,ATTESTATION_ALLOW_EPHEMERAL_KEY)" != "1" ]; then \
	  echo "ERROR: RUN_ATTESTATION_ENABLED=true but ATTESTATION_PRIVATE_KEY_B64 is blank. Generate one:  openssl rand -base64 32  and paste it into deploy/sandbox/.env.sandbox (never commit it). For a throwaway boot set ATTESTATION_ALLOW_EPHEMERAL_KEY=1 instead."; exit 1; fi
	@if [ "$(call envval,SETTLEMENT_REQUIRE_ATTESTATION)" = "true" ] && [ -z "$(call envval,RUN_ATTESTATION_CHARTER_HASH)" ]; then \
	  echo "WARNING: SETTLEMENT_REQUIRE_ATTESTATION=true with no RUN_ATTESTATION_CHARTER_HASH — every gated settle will refuse (charter_hash). See .env.sandbox.example."; fi

# Apply the Prisma migrations (attestations, attested_settlements, ...) from
# inside the superagent image, which carries common/database. Idempotent.
migrate:
	@echo "Applying Prisma migrations..."
	@$(COMPOSE) run --rm --no-deps -T --entrypoint python superagent -m prisma migrate deploy --schema=/app/common/database/schema.prisma
	@echo "  migrations applied."

# Prove the attested profile is wired: tables exist, and SuperAgent booted with
# the observer (or say exactly which piece is missing).
attest-check:
	@docker exec -i sandbox-postgres psql -U postgres orcha -tAc "select count(*) from information_schema.tables where table_name in ('attestations','attested_settlements')" | grep -qx 2 \
	  && echo "OK   attestations + attested_settlements tables present" \
	  || (echo "FAIL attestation tables missing — run: make -f deploy/sandbox/Makefile migrate" && exit 1)
	@docker logs sandbox-superagent 2>&1 | grep -qiE "RunAttestationObserver|run attestation.*(enabled|installed)" \
	  && echo "OK   SuperAgent booted with run attestation" \
	  || (echo "FAIL SuperAgent log shows no run-attestation observer — check RUN_ATTESTATION_ENABLED and ATTESTATION_PRIVATE_KEY_B64 in .env.sandbox, then: docker logs sandbox-superagent" && exit 1)

pull-models:
	@echo "Pulling nomic-embed-text model into Ollama (first pull may take a few minutes)..."
	@docker exec sandbox-ollama ollama pull nomic-embed-text || \
	  echo "WARNING: ollama pull failed — planning-discovery embeddings may not work"

up: check-env
	$(COMPOSE) up -d --build
	@$(MAKE) -f deploy/sandbox/Makefile migrate
	@echo "Waiting for Ollama to be ready..."
	@sleep 10
	@$(MAKE) -f deploy/sandbox/Makefile pull-models
	@echo ""
	@echo "Sandbox starting. Run 'make logs' to follow, 'make status' to check health."
	@echo "URL: http://localhost"

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f --tail=100

status:
	$(COMPOSE) ps

init-db:
	@echo "Applying PnD vector/FTS schema (idempotent)..."
	@docker exec -i sandbox-postgres psql -U postgres orcha \
	  < $(ROOT)/services/planning-discovery/scripts/db/001_create_vector_indices.sql
	@echo "  schema ready."

seed: init-db
	@echo "Seeding agents into registry..."
	@sleep 5
	cd $(ROOT) && REGISTRY_URL=http://localhost:8000 ./scripts/seed-sandbox-agents.sh
