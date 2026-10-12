# Spec Delta: dependency-resilience

## Purpose

Keeps the query path responsive when the embedding model, reranker or LLM is slow or down, through hard per-stage timeouts, bounded retries, circuit breakers and explicit degraded responses.

## ADDED Requirements

### Requirement: Hard per-stage timeouts
Every call to the embedding model, reranker and LLM SHALL be bounded by the stage timeouts in foundation contract C11 (embed 2000 ms, rerank 10000 ms, route 5000 ms, SQL generation 15000 ms, generate 45000 ms, connect 500 ms), and database statements by 1500 ms for retrieval legs, 500 ms for the filter estimate and 2000 ms for generated SQL.

#### Scenario: Slow reranker cut off
- **WHEN** the reranker takes longer than the rerank timeout
- **THEN** the request completes without waiting for it, with fused ordering and `rerank_unavailable` in `degraded`

### Requirement: Bounded retries
Calls SHALL be retried at most once (two attempts in total) and only after a connection failure or an HTTP 5xx response, never after a read timeout, an HTTP 4xx response or an open circuit breaker.

#### Scenario: Transient 503 recovered
- **WHEN** the reranker answers 503 once and then 200
- **THEN** the request is served with rerank scores, no degraded flag, and exactly two requests reached the reranker

#### Scenario: Timeout not retried
- **WHEN** the LLM generation call times out
- **THEN** exactly one generation request was sent

### Requirement: Circuit breakers
Each of the embedding model, reranker and LLM SHALL be protected by its own circuit breaker (count-based window of 20 calls, minimum 10 calls, opening at a 50 % failure rate, open for 30 seconds, 3 trial calls when half-open, timeouts counted as failures); while a breaker is open, calls SHALL fail immediately without contacting the dependency.

#### Scenario: Open breaker short-circuits
- **WHEN** the reranker has failed enough calls to open its breaker and another rerank-enabled query arrives
- **THEN** no request reaches the reranker and the response carries `rerank_unavailable`

#### Scenario: Breakers do not fail health
- **WHEN** any breaker is open
- **THEN** `GET /actuator/health` still reports UP if the database is reachable

### Requirement: Degraded responses
The service SHALL degrade rather than fail: reranker unavailable → fused ranking with `rerank_unavailable`; LLM unavailable for generation → retrieved chunks returned with `answer.status` `unavailable`, `answer.http_status` 503 and `llm_unavailable`; LLM unavailable for routing → vector route with `router_unavailable`; SQL path failure → vector route with `sql_fallback`; embedding unavailable with the lexical leg enabled → lexical results only with `embedding_unavailable`. Every degraded response SHALL have HTTP status 200 and an `X-RAG-Degraded` header listing the codes.

#### Scenario: LLM down returns passages
- **WHEN** the LLM is unreachable and a full-pipeline query arrives
- **THEN** the HTTP status is 200, `chunks` is non-empty, `answer.http_status` is 503, and `degraded` contains `llm_unavailable` and `router_unavailable`

#### Scenario: Embedding down with lexical leg
- **WHEN** the embedding model is unreachable and both legs are enabled
- **THEN** the response has lexical results only, null vector scores, and `embedding_unavailable` in `degraded`

#### Scenario: Embedding down without lexical leg
- **WHEN** the embedding model is unreachable and only the vector leg is enabled
- **THEN** the response status is 503

### Requirement: Connection pools sized deliberately
The service SHALL use a read-only retrieval pool of 10 connections and a separate read-only generated-SQL pool of 3 connections as the `rag_router` role, each with a 1000 ms connection timeout, and SHALL NOT hold a database connection while calling the embedding model, reranker or LLM.

#### Scenario: No connection held during generation
- **WHEN** 12 concurrent full-pipeline queries are waiting on a slow LLM
- **THEN** the retrieval pool has no active connections attributable to those queries during generation
