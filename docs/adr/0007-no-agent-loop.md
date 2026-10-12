# ADR 0007: Why no agent loop

Status: Accepted (2026-10)

## Context
An agent loop lets the model decide when to search again, which tool to call and when to stop. It is the fashionable shape for a RAG system. It also makes latency, cost and correctness unbounded: one question can trigger a variable number of retrievals and model calls, and each run differs.

## Decision
The pipeline is a fixed sequence of stages with a hard timeout each, and routing is a single classification between SQL and vector retrieval. There is no tool-calling loop and no unbounded retry. Every stage can be switched off from an eval config, so each one is justified by a before-and-after number rather than by plausibility.

## Consequences
Latency is bounded by the sum of stage timeouts, and every run is reproducible with a pinned seed and model digests. The system cannot answer questions that need several dependent lookups. If that matters, the bounded alternative is a corrective gate: score the retrieved set, rewrite the query and retry once, measured against the same grid.
