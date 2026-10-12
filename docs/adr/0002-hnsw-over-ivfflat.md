# ADR 0002: HNSW over IVFFlat

Status: Accepted (2026-10)

## Context
pgvector offers two approximate indexes. IVFFlat clusters the vectors into lists at build time and probes a few lists per query. HNSW builds a layered proximity graph. The benchmark in `indexer/bench/index_bench.md` builds both on the real chunk table and compares them with exact search on 200 seeded query chunks.

## Decision
Use HNSW with `m = 16` and `ef_construction = 64`, and set `hnsw.ef_search` to 100 for unfiltered queries so the 50 requested candidates are never capped at pgvector's default of 40.

## Consequences
HNSW needs no training step, and we chose it expecting higher recall at low latency. Measured on this corpus, recall@10 is 0.998 for HNSW against 0.951 for IVFFlat. The price is build time, 10.924 s against 0.832 s, and a larger index. IVFFlat also needs a rebuild as data grows because its lists go stale. Whether selective filters hurt HNSW recall is measured in ADR 0006.
