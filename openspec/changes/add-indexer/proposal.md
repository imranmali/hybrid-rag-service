# Proposal: add-indexer

## Why

Retrieval quality is bounded by what goes into the index. The offline Python indexer turns a reproducible set of arXiv papers into section-aware chunks with contextual headers, two embedding variants and the structured author/category tables the SQL router queries. Without it there is nothing to retrieve, nothing to route to, and no gold chunks for the eval harness.

## What Changes

- New uv project `indexer/` (Python 3.12) with a Typer CLI `indexer` exposing `select`, `fetch`, `ingest`, `status` and `bench-index`.
- Reproducible corpus: `indexer select` builds a committed manifest `indexer/corpus/manifest.jsonl` of 220 arXiv papers (cs.IR, cs.CL, cs.DB, 2023–2026) that have arXiv HTML, including papers by prolific authors so aggregate questions are meaningful.
- Polite fetching: export.arxiv.org Atom API and arxiv.org/html with ≥ 3 s between requests, retries with backoff, and an on-disk cache under `data/cache/arxiv/`.
- Parsing of arXiv LaTeXML HTML into a section tree (references, footnotes and navigation removed; math kept as LaTeX alt text; tables flattened).
- Chunking: 384-token section-bounded, sentence-aligned chunks with 64-token overlap (bge-m3 tokenizer), stable ordinals.
- Contextual header: one LLM call per document (`llama3.1:8b`, temperature 0, seed 42) writes a ≤ 50-word situating sentence; the per-chunk `context_header` combines title, category, year, section path and that sentence.
- Embeddings via host Ollama `bge-m3`: `embedding` (content only) and `embedding_ctx` (header + content), matching the foundation schema.
- Idempotent ingestion keyed on `external_id` + `content_hash` + `pipeline_fingerprint`; per-document transactional replace; `index_metadata` written after every run.
- `indexer bench-index`: HNSW vs IVFFlat build time and recall, and HNSW recall under selective filters, written to `indexer/bench/` for ADRs 0002 and 0006.

## Capabilities

### New Capabilities

- `corpus-acquisition`: Selecting a reproducible arXiv paper set and fetching its metadata and HTML politely with an on-disk cache.
- `document-indexing`: Parsing, chunking, contextual headers, dual embeddings and idempotent writes into the corpus database.
- `index-benchmark`: Measuring ANN index build time and recall (unfiltered and under filters) on the real indexed corpus.

### Modified Capabilities

(none)

## Impact

- Owns `indexer/**` only. Reads `infra/migrations/*.sql` (integration tests) and `infra/models.lock.json`; writes the DB as `rag_indexer`; writes cache files under `data/cache/` (git-ignored) and benchmark output under `indexer/bench/`.
- Depends on host Ollama models `bge-m3` and `llama3.1:8b` and network access to arxiv.org.
- Produces the data `add-query-service`, `add-eval-harness` and `add-ci-docs-load` rely on.

## Implementation order

Depends on `add-platform-foundation` (schema, roles, `.env`, `models.lock.json`). Runs in parallel with `add-reranker-service` and `add-query-service`. Must finish (an indexed DB with ≥ 150 papers) before `add-eval-harness` generates its dataset.

## Non-goals

- No incremental re-embedding on model upgrade (BUILD_SPEC "deferred on purpose"): a changed model digest changes the fingerprint and triggers full re-ingest.
- No PDF parsing, OCR, figures or multimodal content (BUILD_SPEC non-goal: multimodal).
- No custom embedding training or fine-tuning (BUILD_SPEC non-goal).
- No citation graph or knowledge graph (BUILD_SPEC non-goal).
- No per-chunk LLM context generation (cost: ~6k LLM calls vs ~200); see design D3.
- No non-English papers (single corpus, English only).
