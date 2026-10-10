# ADR 0004: Self-hosted cross-encoder over a managed rerank API

Status: Accepted (2026-10)

## Context
A cross-encoder reads the query and each candidate together and is usually the largest single quality gain available after hybrid retrieval. A managed API such as Cohere Rerank is simpler, but it needs a paid key, and this project must run with no key.

## Decision
Serve `BAAI/bge-reranker-v2-m3` from a small FastAPI container on CPU. The service sends the top 20 fused candidates, never more, to bound latency. `bge-reranker-base` is the configurable fallback when the larger model is too slow. If the reranker is unreachable the circuit breaker opens and fused order is served with a `rerank_unavailable` flag.

## Consequences
Adding the reranker took nDCG@10 from {{RESULT: nDCG@10 row 4 | source: evals/results.md}} (row 4) to {{RESULT: nDCG@10 row 5 | source: evals/results.md}} (row 5), with a rerank-stage p95 of {{RESULT: p95 rerank stage ms | source: evals/results.md}} ms. Self-hosting yields a real latency number to budget against, but CPU inference is slow, concurrency is capped at one request, and we own the container, the model download and its health check.
