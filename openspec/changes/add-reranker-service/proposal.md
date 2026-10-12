# Proposal: add-reranker-service

## Why

Cross-encoder reranking is BUILD_SPEC's "largest single quality jump", and self-hosting it (ADR 0004) is what gives the project a real latency number to budget against. The query service needs a small, stable HTTP service that scores (query, passage) pairs with `BAAI/bge-reranker-v2-m3` on CPU, reports its own latency, and can be swapped for a fake scorer in tests and CI.

## What Changes

- New uv project `reranker/` (Python 3.12, FastAPI, sentence-transformers CrossEncoder on CPU torch).
- `POST /rerank` and `GET /healthz` exactly as foundation contract C5.
- Configurable model and revision (`RERANKER_MODEL`, `RERANKER_MODEL_REVISION`; `BAAI/bge-reranker-base` as a faster fallback; `fake` for tests), max length, batch size, max passages, torch threads, and single-flight inference concurrency.
- Structured JSON request logs with queue and inference latency; `X-Rerank-Latency-Ms` header.
- `reranker/Dockerfile` (CPU-only torch wheels, non-root, model cached in the `hf-cache` volume) used by the compose `reranker` service.
- Tests with the deterministic fake model, plus an opt-in test against a tiny real cross-encoder.

## Capabilities

### New Capabilities

- `rerank-api`: The HTTP contract, scoring semantics, limits, health/readiness, latency reporting and configuration of the cross-encoder reranking service.

### Modified Capabilities

(none)

## Impact

- Owns `reranker/**` only. Built by `infra/docker-compose.yml` (`build: ../reranker`, container port 8080, host 8081).
- Consumed by add-query-service (`RAG_RERANKER_URL=http://reranker:8080`) and recorded/replayed by the CI eval gate.
- First container start downloads the model (~2.2 GB) into the `hf-cache` volume.

## Implementation order

Depends on `add-platform-foundation` (compose service definition, `.env` variables). Runs in parallel with `add-indexer` and `add-query-service` (the service tests use WireMock, not this container). Must be done before `add-eval-harness` runs grid rows 5–6.

## Non-goals

- No managed rerank API (Cohere etc.) — ADR 0004.
- No GPU/MPS support inside the container (Docker Desktop on macOS has no GPU passthrough; CPU is the locked decision).
- No fine-tuning of the cross-encoder (BUILD_SPEC non-goal: no fine-tuning).
- No batching across requests or result caching (BUILD_SPEC defers query caching).
- No authentication (internal compose network only).
