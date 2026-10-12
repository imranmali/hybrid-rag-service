# Proposal: add-query-service

## Why

The query path is where BUILD_SPEC's backend-seniority claims live: a JVM service that retrieves with metadata-filtered BM25 + vector fusion from Postgres, reranks with a self-hosted cross-encoder, routes aggregate questions to guarded SQL, and generates answers from a flaky local LLM — with pooling, hard timeouts, circuit breakers, degraded paths and per-stage traces. It must also expose every component as a per-request switch so the eval harness can run the six-row ablation grid against one running service.

## What Changes

- New Gradle (Kotlin DSL) project `service/`: Kotlin 2.x, Spring Boot 3.5.x, builds with the local JDK 25, emits JVM 21 bytecode; ktlint.
- `POST /v1/query`, `GET /v1/info`, `GET /actuator/health` exactly as foundation contract C6, with per-request `pipeline` switches (vector, BM25 + RRF fusion with configurable k, metadata filters explicit + extracted, filter strategy, contextual variant, rerank, routing, generation).
- Hybrid retrieval in SQL over pgvector + Postgres full text; filter pushdown with both paths (exact pre-filter, HNSW over-fetch + post-filter) chosen per query by measured selectivity (threshold 0.25, over-fetch ×5) and reported in the response.
- Deterministic rule-based filter extraction (years, categories, author names verified against the `authors` table).
- LLM router (SQL vs vector) with guarded SQL generation: parsed single-SELECT allowlist, table allowlist, `LIMIT` wrapper, separate read-only `rag_router` pool with a 2 s statement timeout; fallback to vector on any failure.
- Answer generation with citations and an exact abstention sentence; optional Anthropic provider.
- Resilience4j circuit breakers + bounded retries + hard per-stage timeouts on embedding, reranker and LLM; degraded responses per BUILD_SPEC (rerank down → fused results + flag; LLM down → passages + `503` on the generated field).
- Two HikariCP pools sized with a documented rationale; virtual threads; parallel lexical and dense legs.
- OTel Java agent + manual spans `route`, `embed`, `retrieve.vector`, `retrieve.bm25`, `fuse`, `rerank`, `generate` (plus `filter.estimate`, `sql.generate`, `sql.execute`); JSON logs carrying the trace id.
- Exact-vector-search switch for the CI replay gate (contract C9).
- Tests: unit, Testcontainers (pgvector image + real Flyway migrations), WireMock failure modes for Ollama and the reranker.
- `service/Dockerfile` used by the compose `service` container.

## Capabilities

### New Capabilities

- `query-api`: The HTTP request/response contract, validation, defaults and HTTP status semantics of the query service.
- `hybrid-retrieval`: Dense and lexical retrieval, RRF fusion, contextual variant selection, metadata filters with selectivity-based pushdown, and rerank merging.
- `query-routing`: LLM classification of SQL vs vector questions and guarded execution of generated SQL.
- `answer-generation`: Grounded answer generation with citations, abstention, and SQL-result answers.
- `dependency-resilience`: Timeouts, retries, circuit breakers and degraded behaviour for the embedding model, reranker and LLM.
- `service-observability`: Trace spans per stage, trace-id propagation, structured logs and the info endpoint.

### Modified Capabilities

(none)

## Impact

- Owns `service/**` only. Reads `infra/migrations/*.sql` in integration tests. Connects to Postgres as `rag_reader` and `rag_router` only; never writes.
- Depends at runtime on host Ollama (`bge-m3`, `llama3.1:8b`), the reranker container and an indexed DB; tests depend only on Docker.
- `add-eval-harness` and `add-ci-docs-load` (k6, README) call this API.

## Implementation order

Depends on `add-platform-foundation` (schema, roles, compose service definition, contracts). Runs in parallel with `add-indexer` and `add-reranker-service`: all tests use Testcontainers fixtures and WireMock, so no real index or reranker is needed until the final end-to-end verification group, which requires both to be done. Must be done before `add-eval-harness` runs.

## Non-goals

- No agent loop, multi-step retrieval or corrective retrieval gate (BUILD_SPEC non-goal #10; #7 is an optional later extension).
- No streaming responses, query caching or conversational multi-turn rewriting (BUILD_SPEC "deferred on purpose").
- No UI beyond `make query` (BUILD_SPEC: "no UI beyond a thin query console").
- No writes to the database, no ingestion endpoints (Kotlin never writes; BUILD_SPEC architecture).
- No multi-tenancy or authentication (single tenant).
- No LLM-based filter extraction (deterministic rules keep the filter ablation measurable and cheap).
