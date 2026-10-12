# ADR 0006: Pre-filter vs over-fetch-and-post-filter

Status: Accepted (2026-10)

## Context
HNSW returns approximate neighbours of the whole collection. A metadata predicate applied afterwards can leave too few results; applied before, it requires an exact scan of the filtered set. Which is better depends on how selective the predicate is.

## Decision
Estimate selectivity per query as matching chunks divided by all chunks. Below 0.25 the service pre-filters: an exact distance sort over only the matching chunks. At or above 0.25 it over-fetches `vector_k × 5` = 250 candidates from HNSW with `ef_search` 250, applies the filter, and keeps the top 50. Both strategies can be forced per request for benchmarking.

## Consequences
The benchmark in `evals/results_filter_pushdown.md` compares the strategies: {{RESULT: recall@20 for pre, post and auto | source: evals/results_filter_pushdown.md}}. HNSW recall under selective filters is in `indexer/bench/index_bench.md`: {{RESULT: filtered recall@10 at the most selective filter | source: indexer/bench/index_bench.md}}. The post-filter path can still return fewer than 50 candidates, so the response carries an `underfilled` flag, and the threshold of 0.25 is tuned on this corpus only.
