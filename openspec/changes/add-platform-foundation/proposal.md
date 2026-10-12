# Proposal: add-platform-foundation

## Why

Nothing can be built, tested or measured until there is one local stack (Postgres + pgvector, migrations, reranker slot, OTel tracing) and one canonical schema everyone writes against. Five later changes are implemented by separate agents, partly in parallel, so the database DDL, ports, environment variables, model endpoints and HTTP/YAML contracts must be fixed once, here, before any of them starts.

## What Changes

- Add `infra/docker-compose.yml` with `postgres` (pgvector/pgvector:pg17), one-shot `flyway`, `reranker`, `service`, `otel-collector`, `tempo` and `grafana`, plus an `infra/docker-compose.replay.yml` overlay (WireMock record/replay) used by the CI eval gate.
- Add Flyway migrations `infra/migrations/V1__schema.sql` (arXiv-adapted data model: `documents`, `chunks` with two embedding columns and two tsvector columns for the contextual-chunking ablation, `authors`, `paper_authors`, `paper_categories`, `index_metadata`) and `V2__roles.sql` (`rag_indexer` read-write, `rag_reader` read-only, `rag_router` read-only on structured tables with a 2 s statement timeout).
- Add OTel collector config, Tempo config, Grafana datasource provisioning, and WireMock proxy mappings for recording.
- Add `.env.example`, a root `Makefile` (`up`, `down`, `doctor`, `models`, `models-lock`, `index`, `query`, `eval`, `eval-ci`, `eval-record-ci`, `snapshot`, `snapshot-restore`, `test`, `lint`, `load`), a root `ruff.toml`, `infra/models.lock.json` (pinned Ollama digests + reranker revision) and helper scripts in `infra/scripts/`.
- Add the repo skeleton directories (`service/`, `indexer/`, `evals/`, `reranker/`, `load/`, `docs/adr/`, `data/`) and extend `.gitignore`.
- Publish, in `design.md` → "Cross-change contracts", the canonical definitions every other change copies verbatim: DDL, env vars/ports, Ollama endpoints and model names, author-name normalization, reranker API, query service API, eval config YAML schema, dataset record schema, CI replay/snapshot contract, Makefile command contract, and the chosen numbers.

## Capabilities

### New Capabilities

- `local-stack`: The docker-compose stack, its services, ports, health checks, tracing pipeline and the replay overlay.
- `corpus-database`: The Postgres schema, extensions, indexes and least-privilege roles that the indexer writes and the service reads.
- `dev-workflow`: The root Makefile targets, `.env.example`, shared lint config, model digest lock and helper scripts that give one-command workflows.

### Modified Capabilities

(none — greenfield)

## Impact

- New files owned by this change: `infra/**`, `Makefile`, `.env.example`, `ruff.toml`, `.gitignore`, skeleton `.gitkeep` files in the package directories.
- Every later change depends on the contracts in `design.md`; changing them after this lands requires updating every dependent design.
- Requires Docker Desktop and host Ollama with `bge-m3` and `llama3.1:8b` pulled.

## Implementation order

1. **This change first** — nothing else can integrate without the schema and the compose stack.
2. Then in parallel: `add-indexer`, `add-reranker-service`, `add-query-service` (each only reads `infra/` contracts).
3. Then `add-eval-harness` (needs an indexed DB and a running service).
4. Finally `add-ci-docs-load` (needs eval results, the DB snapshot and the CI cassettes).

## Non-goals

- No knowledge graph, multimodal, agent loop, fine-tuning or UI (BUILD_SPEC non-goals). The stack has exactly one datastore.
- No Elasticsearch/OpenSearch or dedicated vector DB (BUILD_SPEC "operational restraint").
- No Prometheus or other metrics backend; traces only (Tempo + Grafana). Metrics are deferred.
- No multi-tenancy, no production deployment manifests (Kubernetes, Terraform); local compose only.
- Ollama is not containerized; it runs on the host for Metal acceleration (locked decision).
