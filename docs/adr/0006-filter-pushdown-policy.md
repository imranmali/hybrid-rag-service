# ADR 0006: Pre-filter vs over-fetch-and-post-filter

Status: Accepted (2026-10)

## Context
HNSW returns approximate neighbours of the whole collection. A metadata predicate applied afterwards can leave too few results; applied before, it requires an exact scan of the filtered set. Which is better depends on how selective the predicate is.

## Decision
Estimate selectivity per query as matching chunks divided by all chunks. Below 0.25 the service pre-filters: an exact distance sort over only the matching chunks. At or above 0.25 it over-fetches `vector_k × 5` = 250 candidates from HNSW with `ef_search` 250, applies the filter, and keeps the top 50. Both strategies can be forced per request for benchmarking.

## Consequences
The benchmark in `evals/results_filter_pushdown.md` compares recall@20 across strategies: pre 0.867, post 0.867, auto 0.867. HNSW recall@10 under filters (post-filter, `ef_search` 40) is in `indexer/bench/index_bench.md`: 1.000 (year=2026, selectivity 0.181). On this small corpus the strategies tie on recall; pre-filtering is faster and never underfills, so the policy mainly protects larger corpora. The post-filter path can return fewer than 50 candidates, flagged `underfilled`, and 0.25 was chosen up front, not tuned.
