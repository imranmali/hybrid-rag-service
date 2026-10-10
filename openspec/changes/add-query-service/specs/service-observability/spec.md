# Spec Delta: service-observability

## Purpose

Makes every query traceable end to end: one span per pipeline stage exported to the local collector, a trace id in the response and in every log line, and structured JSON logs.

## ADDED Requirements

### Requirement: Stage spans
Each executed stage SHALL produce a span named exactly `route`, `filter.estimate`, `embed`, `retrieve.vector`, `retrieve.bm25`, `fuse`, `rerank`, `generate`, `sql.generate` or `sql.execute`, all children of the request's server span, with failures recorded as error status, and spans SHALL be exported over OTLP to the collector configured in foundation contract C2.

#### Scenario: Full pipeline spans
- **WHEN** a full-pipeline query routed to vector completes
- **THEN** the trace contains spans `route`, `filter.estimate`, `embed`, `retrieve.vector`, `retrieve.bm25`, `fuse`, `rerank` and `generate` under one parent

#### Scenario: Failed stage marked
- **WHEN** the reranker call fails
- **THEN** the `rerank` span has error status and the request still returns 200

### Requirement: Trace id propagation
The response body `trace_id`, the `X-Trace-Id` header and the `trace_id` field of every log line written while handling the request SHALL carry the same 32-hex trace id, including log lines written on worker threads.

#### Scenario: Log line matches response
- **WHEN** a query completes
- **THEN** the `query_completed` log line's `trace_id` equals the response's `trace_id`

### Requirement: Structured logs
The service SHALL log JSON lines to stdout, including exactly one `query_completed` line per query with request id, trace id, route, route source, degraded codes, filter strategy, chunk count, answer status and stage timings, and a `dependency_degraded` warning naming the dependency and reason whenever a degraded path is taken.

#### Scenario: Degraded path logged
- **WHEN** the reranker is down during a query
- **THEN** a `dependency_degraded` JSON warning with dependency `reranker` is logged with the request's trace id

### Requirement: Model digest check
At startup the service SHALL compare the live embedding-model digest with `index_metadata.embedding_model_digest`, log an error on mismatch without failing, and report the result in `GET /v1/info`.

#### Scenario: Digest mismatch reported
- **WHEN** the live bge-m3 digest differs from the one recorded in `index_metadata`
- **THEN** the service starts, logs an error, and `/v1/info` reports `embedding_digest_matches_index` false
