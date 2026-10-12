# Spec Delta: query-api

## Purpose

Defines the HTTP interface of the query service — request schema with per-request pipeline switches, response schema, validation and status semantics — so clients and the eval harness can drive every pipeline component.

## ADDED Requirements

### Requirement: Query endpoint contract
The service SHALL expose `POST /v1/query` whose request and response bodies match foundation contract C6 exactly, including snake_case field names, the `pipeline` object, the response fields `request_id`, `trace_id`, `route`, `route_source`, `pipeline`, `filters`, `chunks`, `sql`, `answer`, `degraded`, `timings_ms` and `usage`, and the `X-Trace-Id` header.

#### Scenario: Minimal request uses full-pipeline defaults
- **WHEN** a client posts `{"question": "What is ColBERT?"}`
- **THEN** the response's `pipeline` echoes vector and bm25 enabled, RRF k 60, contextual true, filters enabled with strategy auto, rerank enabled with 20 candidates, routing enabled and generation enabled with context_k 6

#### Scenario: Resolved pipeline echoed
- **WHEN** a client posts a request with `pipeline.rerank.enabled` false and all other pipeline fields omitted
- **THEN** the response's `pipeline.rerank.enabled` is false and every other pipeline field shows its default

### Requirement: Strict validation
The service SHALL reject with HTTP 400 and an `application/problem+json` body listing offending fields any request that violates the validation rules of contract C6, including unknown fields, a blank question, both retrieval legs disabled, out-of-range k values, an unknown fusion method or strategy, a reversed date range, and rerank candidates exceeding `return_k`.

#### Scenario: Unknown field rejected
- **WHEN** a request contains `pipeline.rerank.enable` (misspelled)
- **THEN** the response status is 400 and the problem body names the unknown field

#### Scenario: Both legs disabled
- **WHEN** a request sets `retrieval.vector` and `retrieval.bm25` both false
- **THEN** the response status is 400 with an error on `pipeline.retrieval`

### Requirement: Final ranked chunk list
The response SHALL list at most `return_k` chunks in final rank order, each identified by `chunk_id`, `external_id` and `ordinal`, with per-stage scores (`vector`, `vector_rank`, `bm25`, `bm25_rank`, `fused`, `rerank`) set to null for stages that did not run or did not include the chunk.

#### Scenario: Vector-only scores
- **WHEN** a request enables only the vector leg
- **THEN** every chunk has non-null `vector` and `vector_rank` and null `bm25`, `bm25_rank`, `fused` and `rerank`

### Requirement: HTTP status semantics
The service SHALL return 200 for every request it can serve, including degraded ones; SHALL return 503 with a problem body when no retrieval is possible (database unavailable, or the embedding model unavailable while the lexical leg is disabled); SHALL return 400 for invalid requests and 500 for unexpected errors; and every error body SHALL include the trace id.

#### Scenario: Database down
- **WHEN** the database is unreachable and a query arrives
- **THEN** the response status is 503 with a problem body containing `trace_id`

### Requirement: Stage timings and usage
The response SHALL report wall-clock milliseconds for every attempted stage under the fixed keys of contract C6 plus `total`, and token/call counts in `usage`.

#### Scenario: Skipped stages absent
- **WHEN** a request disables rerank, routing and generation
- **THEN** `timings_ms` has no `rerank`, `route` or `generate` keys and `usage.llm_calls` is 0

### Requirement: Info endpoint
The service SHALL expose `GET /v1/info` returning service version and git sha, embedding and LLM model names with their live digests, reranker reachability and model, the current `index_metadata` contents read at request time, whether the embedding digest matches the index, whether exact vector search is on, and the default pipeline.

#### Scenario: Index metadata read live
- **WHEN** `index_metadata.chunk_count` changes while the service is running
- **THEN** the next `GET /v1/info` reports the new value without a restart
