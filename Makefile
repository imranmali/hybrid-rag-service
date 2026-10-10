# Hybrid RAG Service: one-command workflows. Contract: design.md of add-platform-foundation, C10.
# `make` or `make help` lists every target.

SHELL := /bin/bash
.SHELLFLAGS := -o pipefail -c
.DEFAULT_GOAL := help

# Local settings come from .env (git-ignored; copy .env.example). Defaults below keep the
# targets usable when a variable is missing.
-include .env
POSTGRES_DB ?= rag
POSTGRES_USER ?= rag_owner
SERVICE_HOST_PORT ?= 8080
RERANKER_HOST_PORT ?= 8081
RERANKER_DEVICE ?= auto
RAG_SERVICE_URL ?= http://localhost:$(SERVICE_HOST_PORT)
OLLAMA_EMBED_MODEL ?= bge-m3
OLLAMA_LLM_MODEL ?= llama3.1:8b
LIMIT ?= 200
GIT_SHA ?= $(shell git rev-parse --short HEAD 2>/dev/null || echo unknown)
export GIT_SHA

COMPOSE = docker compose --env-file .env -f infra/docker-compose.yml
COMPOSE_REPLAY = $(COMPOSE) -f infra/docker-compose.replay.yml
# WIREMOCK_MODE picks which mappings directory the replay overlay mounts (see C9).
REPLAY_RECORD = WIREMOCK_MODE=record WIREMOCK_MAPPINGS_DIR=./wiremock/proxy-mappings
REPLAY_REPLAY = WIREMOCK_MODE=replay WIREMOCK_MAPPINGS_DIR=../evals/cassettes/ci/mappings
PG_EXEC = $(COMPOSE) exec -T postgres

PY_PACKAGES := indexer reranker evals
SNAPSHOT_DIR := data/snapshots
SNAPSHOT ?= $(lastword $(sort $(wildcard $(SNAPSHOT_DIR)/*.dump)))
SHA256 = $(shell command -v sha256sum >/dev/null 2>&1 && echo sha256sum || echo "shasum -a 256")

.PHONY: help doctor models models-lock up down clean index query eval eval-record-ci eval-ci \
	snapshot snapshot-restore test lint load up-infra reranker up-reranker-container \
	_require-env _require-reranker

help: ## List the available targets
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z_-]+:.*## / {printf "  %-18s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

_require-env:
	@test -f .env || { echo "error: .env is missing; run: cp .env.example .env" >&2; exit 1; }

doctor: ## Check docker, .env, Ollama and its models, uv and the JDK
	@bash infra/scripts/doctor.sh

models: ## Pull the Ollama models (bge-m3, llama3.1:8b) on the host
	ollama pull $(OLLAMA_EMBED_MODEL) && ollama pull $(OLLAMA_LLM_MODEL)

models-lock: ## Regenerate infra/models.lock.json (Ollama digests + reranker revision)
	python3 infra/scripts/models_lock.py > infra/models.lock.json.tmp && mv infra/models.lock.json.tmp infra/models.lock.json \
		|| { rm -f infra/models.lock.json.tmp; exit 1; }

up: _require-env ## Build and start the stack (reranker runs separately: make reranker) and wait until healthy
	$(COMPOSE) up -d --build --wait

reranker: _require-env ## Run the reranker natively on the host (MPS on Apple Silicon) on RERANKER_HOST_PORT; foreground
	@test -f reranker/pyproject.toml || { echo "error: reranker/ is not present yet" >&2; exit 1; }
	cd reranker && set -a && . ../.env && set +a && RERANKER_DEVICE=$(RERANKER_DEVICE) uv run uvicorn reranker.app:app --host 0.0.0.0 --port $(RERANKER_HOST_PORT) --workers 1 --no-access-log

up-reranker-container: _require-env ## Opt-in: run the reranker as a CPU container instead of `make reranker`
	$(COMPOSE) --profile reranker-container up -d --build --wait reranker

_require-reranker:
	@curl -fsS -m 3 http://localhost:$(RERANKER_HOST_PORT)/healthz >/dev/null || { echo "error: no reranker on localhost:$(RERANKER_HOST_PORT); start it with 'make reranker' (or 'make up-reranker-container')" >&2; exit 1; }

up-infra: _require-env ## Start only postgres, migrations and tracing (no service/reranker images needed)
	$(COMPOSE) up -d --wait postgres otel-collector tempo grafana
	$(COMPOSE) run --rm flyway

down: ## Stop the stack (keeps volumes)
	$(COMPOSE) down --remove-orphans

clean: ## Stop the stack and delete its volumes (database, model cache, traces)
	$(COMPOSE) down -v --remove-orphans

index: ## Ingest papers into the database (LIMIT=200)
	@test -f indexer/pyproject.toml || { echo "error: indexer/ is not present yet" >&2; exit 1; }
	cd indexer && uv run indexer ingest --limit $(LIMIT)

query: export Q := $(Q)
query: ## Ask the running service a question: make query Q="..."
	@test -n "$$Q" || { echo 'usage: make query Q="your question"' >&2; exit 1; }
	curl -sS -X POST $(RAG_SERVICE_URL)/v1/query -H 'content-type: application/json' -d "$$(jq -n --arg q "$$Q" '{question:$$q}')" | jq

eval: ## Run the full ablation grid against the running stack
	cd evals && uv run evals grid

eval-record-ci: _require-env _require-reranker ## Record the CI cassettes (WireMock proxying live Ollama and the reranker)
	$(COMPOSE) up -d --build --wait postgres flyway
	$(REPLAY_RECORD) $(COMPOSE_REPLAY) up -d --build --wait --force-recreate --no-deps wiremock service
	@rc=0; \
	( cd evals && uv run evals grid --subset ci --no-generation --no-judge && uv run evals cassettes snapshot ) || rc=$$?; \
	$(COMPOSE_REPLAY) rm -sf wiremock service; \
	exit $$rc

eval-ci: _require-env ## Run the CI regression gate offline (snapshot restore + WireMock replay)
	@test -n "$(SNAPSHOT)" || { echo "error: no snapshot found in $(SNAPSHOT_DIR)/; pass SNAPSHOT=<path>" >&2; exit 3; }
	@test -d evals/cassettes/ci/mappings || { echo "error: evals/cassettes/ci/mappings is missing; run: make eval-record-ci" >&2; exit 3; }
	$(COMPOSE) up -d --wait postgres flyway
	$(MAKE) snapshot-restore SNAPSHOT=$(SNAPSHOT)
	$(REPLAY_REPLAY) $(COMPOSE_REPLAY) up -d --build --wait --force-recreate --no-deps wiremock service
	@rc=0; \
	( cd evals && uv run evals grid --subset ci --no-generation --no-judge --replay --gate thresholds/ci.yaml ) || rc=$$?; \
	$(COMPOSE_REPLAY) rm -sf wiremock service; \
	exit $$rc

snapshot: _require-env ## Dump the indexed corpus (data only) to data/snapshots/rag-index-<fingerprint>.dump
	@fp="$$($(PG_EXEC) psql -U $(POSTGRES_USER) -d $(POSTGRES_DB) -tA -c "select value from index_metadata where key='pipeline_fingerprint'" | tr -d '[:space:]')"; \
	if [ -z "$$fp" ]; then \
		echo "error: index_metadata has no 'pipeline_fingerprint'; run 'make index' first" >&2; exit 1; \
	fi; \
	mkdir -p $(SNAPSHOT_DIR); \
	out="$(SNAPSHOT_DIR)/rag-index-$${fp:0:12}.dump"; \
	$(PG_EXEC) pg_dump -U $(POSTGRES_USER) -d $(POSTGRES_DB) --data-only --format=custom \
		--exclude-table=flyway_schema_history > "$$out.tmp" && mv "$$out.tmp" "$$out" \
		|| { rm -f "$$out.tmp"; echo "error: pg_dump failed" >&2; exit 1; }; \
	echo "wrote $$out"; \
	$(SHA256) "$$out"

snapshot-restore: _require-env ## Restore a snapshot into the migrated DB (replaces the corpus): make snapshot-restore SNAPSHOT=<path>
	@test -n "$(SNAPSHOT)" -a -f "$(SNAPSHOT)" || { echo "error: snapshot file not found; pass SNAPSHOT=<path>" >&2; exit 1; }
	$(PG_EXEC) psql -U $(POSTGRES_USER) -d $(POSTGRES_DB) -v ON_ERROR_STOP=1 -c 'truncate documents, authors, paper_authors, paper_categories, chunks, index_metadata restart identity cascade'
	$(PG_EXEC) pg_restore -U $(POSTGRES_USER) -d $(POSTGRES_DB) --data-only --disable-triggers --single-transaction < "$(SNAPSHOT)"
	$(PG_EXEC) psql -U $(POSTGRES_USER) -d $(POSTGRES_DB) -v ON_ERROR_STOP=1 -c 'ANALYZE'

test: ## Run the tests of every package that exists
	@fail=0; \
	for p in $(PY_PACKAGES); do \
		if [ -f $$p/pyproject.toml ]; then \
			echo "==> $$p: uv run pytest"; (cd $$p && uv run pytest) || fail=1; \
		else echo "skip: $$p (not present)"; fi; \
	done; \
	if [ -f service/gradlew ]; then \
		echo "==> service: ./gradlew test"; (cd service && ./gradlew test) || fail=1; \
	else echo "skip: service (not present)"; fi; \
	exit $$fail

lint: ## Lint every package that exists (ruff for Python, ktlint for the service)
	@fail=0; \
	for p in $(PY_PACKAGES); do \
		if [ -f $$p/pyproject.toml ]; then \
			echo "==> $$p: ruff"; (cd $$p && uv run ruff check . && uv run ruff format --check .) || fail=1; \
		else echo "skip: $$p (not present)"; fi; \
	done; \
	if [ -f service/gradlew ]; then \
		echo "==> service: ./gradlew ktlintCheck"; (cd service && ./gradlew ktlintCheck) || fail=1; \
	else echo "skip: service (not present)"; fi; \
	exit $$fail

load: ## Run the k6 load test against the running service
	docker run --rm -v $(CURDIR)/load:/load -v $(CURDIR)/evals/dataset:/dataset:ro \
		-e SCENARIO -e K6_TARGET=http://host.docker.internal:$(SERVICE_HOST_PORT) grafana/k6:0.54.0 run /load/query.js
