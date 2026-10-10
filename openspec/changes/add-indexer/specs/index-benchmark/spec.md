# Spec Delta: index-benchmark

## Purpose

Produces reproducible measurements of ANN index build time and recall, with and without selective metadata filters, on the real indexed corpus so the HNSW and filter-pushdown ADRs cite real numbers.

## ADDED Requirements

### Requirement: HNSW versus IVFFlat benchmark
`indexer bench-index` SHALL copy the chunk embeddings into a temporary table, build an HNSW index (m 16, ef_construction 64) and an IVFFlat index (lists = round(sqrt(row count))), record each build time, and report recall@10 against exact search plus p50/p95 query latency for HNSW `ef_search` ∈ {40, 100, 250} and IVFFlat `probes` ∈ {1, 5, 10}, over 200 seeded sample queries that exclude the query chunk itself.

#### Scenario: Results written
- **WHEN** `uv run indexer bench-index --queries 200 --seed 42` completes
- **THEN** `indexer/bench/index_bench.json` and `indexer/bench/index_bench.md` contain build seconds, recall@10 and latency percentiles for every setting, plus the row count and timestamp

#### Scenario: No changes to real indexes
- **WHEN** the benchmark runs
- **THEN** it uses only temporary tables and the real `chunks` indexes are untouched

### Requirement: Filtered recall measurement
The benchmark SHALL measure recall@10 of an HNSW query with a plain `WHERE` filter (no iterative scan) against exact filtered search, for filters on each primary category and on each publication year, and report each filter's selectivity alongside.

#### Scenario: Selective filter reported
- **WHEN** the benchmark runs on a corpus where one category holds under 25 % of chunks
- **THEN** the output lists that filter's selectivity and its naive-HNSW recall@10 next to the exact recall of 1.0
