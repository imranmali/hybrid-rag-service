# Spec Delta: hybrid-retrieval

## Purpose

Retrieves candidate chunks with dense vectors and Postgres full text, fuses them, applies metadata filters with a per-query pushdown strategy, selects the contextual or plain variant, and merges reranker output — all controlled by request switches.

## ADDED Requirements

### Requirement: Dense retrieval
When the vector leg is enabled the service SHALL embed the question with the configured embedding model and return the `vector_k` chunks with the highest cosine similarity, reporting similarity and 1-based rank.

#### Scenario: Nearest chunk first
- **WHEN** the question embedding equals a stored chunk's embedding and no filters apply
- **THEN** that chunk has `vector_rank` 1 and `vector` similarity 1.0 (within 1e-4)

#### Scenario: More than 40 results returned
- **WHEN** `vector_k` is 50 and the corpus has more than 50 chunks
- **THEN** exactly 50 dense candidates are returned

### Requirement: Lexical retrieval
When the BM25 leg is enabled the service SHALL match the question's stemmed English terms with OR semantics against the chunk full-text vector and return the top `bm25_k` chunks by `ts_rank_cd` with normalization 32, ties broken by lower chunk id.

#### Scenario: Partial term overlap still matches
- **WHEN** the question contains three terms and a chunk contains only one of them
- **THEN** that chunk can appear in the lexical results

#### Scenario: Stop-word-only question
- **WHEN** the question contains only stop words
- **THEN** the lexical leg returns no candidates and the request still succeeds

### Requirement: Reciprocal rank fusion
When both legs are enabled the service SHALL fuse them with `score = Σ 1/(k + rank)` over the legs in which a chunk appears, sort by descending score with ties broken by lower chunk id, and truncate to `return_k`.

#### Scenario: Chunk in both legs outranks single-leg chunks
- **WHEN** chunk A is rank 2 in both legs and chunk B is rank 1 in only the dense leg, with k 60
- **THEN** A's fused score (2/62) exceeds B's (1/61) and A ranks above B

#### Scenario: Configurable k
- **WHEN** the request sets `fusion.k` to 1
- **THEN** fused scores are computed with k 1

### Requirement: Contextual variant switch
The service SHALL use the content-only embedding and full-text columns when `contextual` is false and the header-plus-content columns when it is true, and SHALL send the header-plus-content text to the reranker only when `contextual` is true.

#### Scenario: Header-only term
- **WHEN** a question term appears only in a chunk's context header and the request uses only the lexical leg
- **THEN** the chunk is returned with `contextual` true and not returned with `contextual` false

### Requirement: Metadata filters
When filters are enabled the service SHALL restrict both legs to documents matching every applied filter — any of the given categories (primary or cross-listed), any of the given normalized author names, and the inclusive publication date range — where applied filters are the explicit ones plus, if extraction is enabled, those extracted from the question for fields not given explicitly.

#### Scenario: Year and author extracted
- **WHEN** the question is "What did Jimmy Lin publish on rerankers in 2024?" and "jimmy lin" exists in the authors table
- **THEN** `filters.extracted` contains author `jimmy lin`, `published_from` 2024-01-01 and `published_to` 2024-12-31, and every returned chunk belongs to a 2024 paper with that author

#### Scenario: Unknown author ignored
- **WHEN** the question mentions "by Ada Nobody" and no such author exists
- **THEN** no author filter is applied

#### Scenario: Explicit overrides extracted
- **WHEN** the question says "in 2024" and the explicit filter sets `published_from` 2025-01-01
- **THEN** the applied `published_from` is 2025-01-01

#### Scenario: Filters disabled
- **WHEN** `filters.enabled` is false and the question says "in 2024"
- **THEN** `filters` in the response is null and results are not date-restricted

### Requirement: Selectivity-based filter pushdown
For the dense leg with filters applied, the service SHALL compute the fraction of chunks matching the filters and, under strategy `auto`, use exact search over the filtered set when that fraction is below `selectivity_threshold` and otherwise fetch `vector_k × overfetch_factor` approximate nearest neighbours and filter them; strategies `pre` and `post` SHALL force the respective path; the response SHALL report the strategy, the selectivity, the matching chunk count and whether the post-filter path returned fewer than `vector_k` candidates.

#### Scenario: Selective filter uses pre-filter
- **WHEN** filters match 3 % of chunks under strategy auto with threshold 0.25
- **THEN** `filters.strategy` is `pre` and the dense results equal an exact search over the matching chunks

#### Scenario: Broad filter uses post-filter
- **WHEN** filters match 60 % of chunks under strategy auto
- **THEN** `filters.strategy` is `post`

#### Scenario: Underfilled post-filter reported
- **WHEN** strategy `post` is forced with a filter matching 1 % of chunks
- **THEN** fewer than `vector_k` dense candidates are returned and `filters.underfilled` is true

### Requirement: Rerank merge
When rerank is enabled the service SHALL send the top `candidates` chunks of the fused list to the reranker and return them ordered by rerank score, followed by the remaining fused chunks in fused order.

#### Scenario: Tail keeps fused order
- **WHEN** rerank is enabled with 20 candidates and `return_k` 50
- **THEN** ranks 1–20 are ordered by descending `rerank` score and ranks 21–50 have null `rerank` and keep their fused order

### Requirement: Exact search mode
When exact vector search is configured the service SHALL compute dense results by exact distance ordering with ties broken by lower chunk id, without approximate indexes, so results are reproducible across database restores.

#### Scenario: Deterministic across restores
- **WHEN** the same snapshot is restored twice and the same request is sent with exact search on
- **THEN** both responses list identical chunk ids in identical order
