# Spec Delta: rerank-api

## Purpose

Scores (query, passage) pairs with a self-hosted cross-encoder over HTTP so the query service can reorder retrieved chunks, with explicit limits, readiness and latency reporting.

## ADDED Requirements

### Requirement: Rerank endpoint
The service SHALL expose `POST /rerank` accepting `{query, passages: [{id, text}], top_k?}` and returning `{model, results: [{id, index, score}], latency_ms}` exactly as foundation contract C5, with results sorted by score descending (ties by lower index) and truncated to `top_k` (default: all passages; larger values clamped).

#### Scenario: Results ordered and truncated
- **WHEN** a request has 5 passages and `top_k` 3
- **THEN** the response has exactly 3 results, sorted by descending score, each carrying the passage's `id` and its 0-based request `index`

#### Scenario: top_k larger than passage count
- **WHEN** a request has 2 passages and `top_k` 20
- **THEN** the response has 2 results

### Requirement: Score semantics
Each score SHALL be the sigmoid of the cross-encoder logit for the (query, passage) pair, in the range [0, 1], with each pair truncated to the configured maximum length (default 512 tokens).

#### Scenario: Relevant passage scores higher
- **WHEN** the real model scores the query "what is the capital of France" against "Paris is the capital of France." and "Bananas are rich in potassium."
- **THEN** the first passage's score is higher than the second's and both lie in [0, 1]

### Requirement: Input validation and limits
The service SHALL reject with HTTP 422 a request whose query is empty or longer than 2000 characters, whose passage list is empty or longer than the configured maximum (default 100), whose passage ids are empty or duplicated, or whose passage text is empty.

#### Scenario: Too many passages
- **WHEN** a request contains 101 passages with the default configuration
- **THEN** the response status is 422 and no inference runs

#### Scenario: Duplicate ids
- **WHEN** two passages share the id "7"
- **THEN** the response status is 422

### Requirement: Readiness
`GET /healthz` SHALL return 503 with `ready: false` until the model is loaded and 200 with `{status, model, revision, device, max_length, ready: true}` afterwards; `POST /rerank` SHALL return 503 while the model is not ready.

#### Scenario: Health after load
- **WHEN** the model has finished loading
- **THEN** `GET /healthz` returns 200 with `ready` true, `device` "cpu" and the configured model name

### Requirement: Latency reporting
Every rerank response SHALL carry the server-measured latency (queue wait plus inference) in `latency_ms` and in the `X-Rerank-Latency-Ms` header, and the service SHALL log one JSON line per request with passage count, top_k, queue milliseconds, inference milliseconds and model.

#### Scenario: Latency logged
- **WHEN** a rerank request completes
- **THEN** stdout contains a JSON log line with `event` "rerank", `n_passages`, `queue_ms`, `inference_ms` and `model`, and the response header value equals `latency_ms`

### Requirement: Configurable model and fake backend
The model, revision, maximum length, batch size, maximum passages, torch thread count and maximum concurrent inferences SHALL be configurable through the environment variables in foundation contract C2, and `RERANKER_MODEL=fake` SHALL select a deterministic token-overlap scorer that needs no model download.

#### Scenario: Fake model deterministic
- **WHEN** the same request is sent twice to a service started with `RERANKER_MODEL=fake`
- **THEN** both responses have identical results and scores

#### Scenario: Fallback model
- **WHEN** the service starts with `RERANKER_MODEL=BAAI/bge-reranker-base`
- **THEN** `/healthz` and every rerank response report that model name

### Requirement: Bounded inference concurrency
The service SHALL run at most `RERANKER_MAX_CONCURRENCY` (default 1) inferences at a time, queueing further requests rather than running them in parallel.

#### Scenario: Concurrent requests serialized
- **WHEN** two rerank requests arrive simultaneously with the default configuration
- **THEN** their inference intervals do not overlap and the second response's `queue_ms` is greater than zero
