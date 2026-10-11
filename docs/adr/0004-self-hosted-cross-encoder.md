# ADR 0004: Self-hosted cross-encoder over a managed rerank API

Status: Accepted (2026-10)

## Context
A cross-encoder reads the query and each candidate together and is usually the largest single quality gain available after hybrid retrieval. A managed API such as Cohere Rerank is simpler, but it needs a paid key, and this project must run with no key.

## Decision
Serve `BAAI/bge-reranker-v2-m3` from a small FastAPI app, by default as a host process on the Apple GPU (MPS) because a CPU container took about 40 s per 20 passages against a 10 s timeout; a CPU container remains the Linux and CI option. The service sends the top 20 fused candidates, never more, to bound latency. `bge-reranker-base` is the configurable fallback when the larger model is too slow. If the reranker is unreachable the circuit breaker opens and fused order is served with a `rerank_unavailable` flag.

## Consequences
Adding the reranker took nDCG@10 from 0.611 (row 4) to 0.846 (row 5), with a rerank-stage p95 of 7,937 ms. Self-hosting yields a real latency number to budget against, but inference is slow, concurrency is capped at one request, and we own the model download and health check.
