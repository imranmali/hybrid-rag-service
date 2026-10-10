# ADR 0002: HNSW over IVFFlat

Status: Accepted (2026-10)

## Context
pgvector offers two approximate indexes. IVFFlat clusters the vectors into lists at build time and probes a few lists per query. HNSW builds a layered proximity graph. The benchmark in `indexer/bench/index_bench.md` builds both on the real chunk table and compares them with exact search on 200 seeded query chunks.

## Decision
Use HNSW with `m = 16` and `ef_construction = 64`, and set `hnsw.ef_search` to 100 for unfiltered queries so the 50 requested candidates are never capped at pgvector's default of 40.

## Consequences
HNSW needs no training step, and we chose it expecting higher recall at low latency. Measured on this corpus, recall@10 is {{RESULT: HNSW recall@10 at ef_search 100 | source: indexer/bench/index_bench.md}} for HNSW against {{RESULT: IVFFlat recall@10 at probes 10 | source: indexer/bench/index_bench.md}} for IVFFlat. The price is build time, {{RESULT: HNSW build seconds | source: indexer/bench/index_bench.md}} against {{RESULT: IVFFlat build seconds | source: indexer/bench/index_bench.md}}, and a larger index. IVFFlat also needs a rebuild as data grows because its lists go stale. Selective filters hurt HNSW recall, which ADR 0006 addresses.
