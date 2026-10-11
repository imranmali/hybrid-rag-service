# ADR 0001: Postgres + pgvector over Elasticsearch or a dedicated vector database

Status: Accepted (2026-10)

## Context
The service needs three things per query: nearest-neighbour search over chunk embeddings, lexical ranking, and relational filters on paper metadata (category, year, author). Aggregate questions such as "how many cs.IR papers did X publish in 2025" also need real SQL. The corpus is about 200 papers, so scale is not the constraint. Operations are.

## Decision
Use one Postgres instance with pgvector for vectors, a `tsvector` column with a GIN index for BM25-style ranking, and ordinary tables for metadata. A hybrid query is one SQL statement, and metadata filters are joins against `documents`, not a second system kept in sync.

## Consequences
One datastore means one backup, one connection pool and one failure mode. The router can run generated, guarded SQL against the same database. We give up Elasticsearch's analyzers and native BM25; `ts_rank_cd` is a weaker lexical scorer, and the hybrid row measures its effect: it changed nDCG@10 by -0.083 against vector-only. Past tens of millions of vectors this choice would need revisiting.
