# ADR 0003: Kotlin serving layer, Python offline

Status: Accepted (2026-10)

## Context
Two kinds of work live in this system. Indexing parses papers, chunks them, calls embedding and summary models, and has no latency budget. Serving answers a request within a budget while three dependencies (Ollama, the reranker, Postgres) can be slow or down.

## Decision
Python owns everything with a model dependency and no latency budget: the indexer, the eval harness and the reranker model host. Kotlin owns everything with a latency budget and no model dependency: routing, retrieval SQL, fusion, timeouts, circuit breakers and tracing. The seam is the database. The indexer writes as `rag_indexer`; the service connects only as read-only roles `rag_reader` and `rag_router`, so it cannot corrupt the index.

## Consequences
Each side uses the strongest tools for its job: pooling, Resilience4j and OpenTelemetry on the JVM, the model ecosystem in Python. The cost is two toolchains, a duplicated author-name normalisation rule that a shared contract pins, and an eval harness that must call the service over HTTP.
