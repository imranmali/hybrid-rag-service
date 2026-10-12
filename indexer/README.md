# Indexer

The host-side indexer builds the reproducible arXiv corpus used by the Hybrid RAG
Service. It selects 220 version-pinned cs.IR, cs.CL, and cs.DB papers from 2023-2026,
parses arXiv LaTeXML HTML, writes section-aware chunks, and stores both plain and
contextual bge-m3 embeddings in PostgreSQL.

## Commands

From `indexer/`:

```bash
uv sync
uv run indexer select --target 220 --seed 42
uv run indexer fetch
uv run indexer ingest --limit 5
uv run indexer ingest --limit 150
uv run indexer ingest --offset 130 --limit 20   # manifest slice; not combinable with --prune
uv run indexer ingest --limit 150 --prune
uv run indexer status
uv run indexer bench-index --queries 200 --seed 42
```

`make index` at the repository root runs `uv run indexer ingest --limit $(LIMIT)` (default 200; this
project's live corpus was indexed with `make index LIMIT=150`, see Measured live runs).

## Environment

The package loads the repository-root `.env`; process environment variables override it.

| Variable | Purpose |
| --- | --- |
| `INDEXER_DATABASE_URL` | PostgreSQL DSN for the `rag_indexer` role |
| `OLLAMA_HOST_URL` | Host Ollama base URL |
| `OLLAMA_EMBED_MODEL` | Embedding model, normally `bge-m3` |
| `OLLAMA_LLM_MODEL` | Context-summary model, normally `llama3.1:8b` |
| `OLLAMA_KEEP_ALIVE` | Ollama residency value, normally `30m` |
| `LLM_SEED` | Deterministic context-summary seed, normally `42` |
| `LLM_PROVIDER` | `ollama` (default) or opt-in `anthropic` |
| `ANTHROPIC_API_KEY` / `ANTHROPIC_MODEL` | Required only for the Anthropic provider |
| `ARXIV_CONTACT` | Optional mailto address included in the arXiv User-Agent |

## Pipeline contracts

- Cache: `data/cache/arxiv/api/<sha1>.xml`, version-pinned HTML at
  `data/cache/arxiv/html/<external_id>v<version>.html`, and `.missing` negative entries.
  A warm `indexer fetch` makes zero network requests.
- Chunking: sentence-aligned, section-bounded 384-token chunks; whole-sentence overlap is
  at most 64 tokens; a tail under 64 can merge for an absolute 448-token maximum.
- Headers: `Paper: <title> (<primary_category>, <year>). Section: <section_path>. <summary>`.
  The summary is one LLM call per document and is capped at 50 words.
- Fingerprint inputs: chunk size, overlap, minimum chunk length, tokenizer,
  prompt and parser versions, embedding model and digest, plus LLM model and digest.
- Model lock drift fails ingestion with exit code 3; `--allow-model-drift` permits it
  explicitly. Exit code 1 is a failure rate over 5%; code 2 is usage/configuration error.

## Measured live runs

Host: Apple M4, Ollama on the host GPU (`bge-m3`, `llama3.1:8b`), shared with other agents, so
throughput varied between about 1 and 2 documents per minute (load average 5 to 18).

| Step | Command | Result |
| --- | --- | --- |
| Select | `indexer select --target 220 --seed 42` | 23 m 45 s; 220 papers (cs.IR 79, cs.DB 73, cs.CL 68); 48 API pages, 388 HTML probes (364 pages cached, 24 negative `.missing` entries) |
| Fetch, twice | `indexer fetch` | 220 papers, 0 missing, 0 network requests each time (0.3 to 0.6 s); select had already warmed the cache |
| First ingest | `indexer ingest --limit 5` | 5 inserted, 0 failed, 263 chunks, max 443 tokens; 9 m 38 s (includes cold model load) |
| Main ingest | `make index` | **Cut at 92 documents, not 200** (see below); 5,486 chunks, 0 failed; about 1.5 to 2 docs/min, 1 to 2 chunks/s |
| Idempotent re-run | `make index LIMIT=83` | `inserted 0, updated 0, unchanged 83, failed 0` in 26.8 s |
| Idempotent re-run | `indexer ingest --offset 130 --limit 9` | `unchanged 9, failed 0` in 2.6 s |
| ANN benchmark | `indexer bench-index --queries 200 --seed 42` | about 45 to 60 s; see `bench/index_bench.md` |

### Corpus actually indexed (deviation: 92 of the planned 200 papers)

The live ingest was stopped for time. Task 5.2 asks for 200 papers and at least 150 documents;
this corpus has 92 documents, so that bar is **not met**. The database holds manifest papers
the first 83 papers (positions 0 to 82) plus 9 papers at positions 130 to 138, which a second worker wrote
before it was shown to be slower than a single worker (1.34 vs 1.97 chunks/s on one GPU).
`make index` (default `LIMIT=200`) tops the corpus up idempotently: papers already indexed are
skipped as `unchanged`. A kill mid-paper loses at most that paper, because each document is one
transaction.

Final state: 92 documents (cs.IR 35, cs.DB 30, cs.CL 27), 5,486 chunks, 479 authors, no chunk with
a null embedding or contextual embedding, max chunk 446 tokens (limit 448), all 13 `index_metadata`
keys present (`document_count` 92, `chunk_count` 5486), `ANALYZE` run on all five corpus tables.
`pipeline_fingerprint` is
`33b6d6becba537c6b8bbe2b22f1d27812f1fdcf64dae662693d0cabcef6583eb`.

### Benchmark notes

- The benchmark ANALYZEs its temporary table and forbids sequential scans on the approximate
  side, so every approximate query uses the ANN index (checked with `EXPLAIN`).
- Filtered recall is measured with `hnsw.ef_search = 40` and no iterative scan, using queries
  whose own category or year equals the filter. Those neighbours tend to share the filter value, so
  recall is 1.000 on this small corpus; it is not evidence about hard filters on a larger corpus.
- `\di chunks*` was identical before and after every run (7 indexes).

## Sample headers

Five random `chunks.context_header` values:

```text
Paper: Accurate and Scalable Matrix Mechanisms via Divide and Conquer (cs.DB, 2026). Section: 6. The QuerySmasher Algorithm > 6.3. Optimizing a Workload Partition > Optimal Matrix Mechanism Construction. This paper presents QuerySmasher, a scalable matrix mechanism for providing differentially private query answers to high-dimensional datasets, which operates by dividing queries into sub-queries and re-assembling them using existing low-dimensional mechanisms in the database and machine learning domains.
Paper: ECR: Manifold-Guided Semantic Cues for Compact Language Models (cs.CL, 2026). Section: 2 Related Works. This research proposes Embedding Consistency Regulation (ECR), a framework that preserves semantic structure in compact language models by maintaining consistent geometry around teacher embeddings, addressing issues with model collapse under capacity constraints in multilingual tasks.
Paper: Improving the Distributional Alignment of LLMs using Supervision (cs.CL, 2025). Section: 7 Discussion. This research investigates how to improve the alignment of Large Language Models (LLMs) with diverse population groups on subjective questions by adding simple supervision, evaluating its effectiveness across three datasets in public health, opinion, and values and beliefs domains.
Paper: Cost-Effective Numerical QA over Semi-Structured Table: Structuring, Resolution, Planning (cs.DB, 2026). Section: V Plan–Guided Reasoner > V-B From Plan to Execution. This research proposes SemiBonsai, a cost-effective framework for answering numerical questions over semi-structured tables by structuring, resolving uncertainty, planning reasoning, and routing Large Language Models (LLMs) under fixed monetary constraints in the database management domain.
Paper: Accurate and Scalable Matrix Mechanisms via Divide and Conquer (cs.DB, 2026). Section: 6. The QuerySmasher Algorithm > 6.2. Query Decomposition Details. This paper presents QuerySmasher, a scalable matrix mechanism for providing differentially private query answers to high-dimensional datasets, which operates by dividing queries into sub-queries and re-assembling them using existing low-dimensional mechanisms in the database and machine learning domains.
```

Each has the title, primary category, year, a section path and one summary sentence; the longest
stored summary across all 92 documents is 45 words (limit 50).

## Known limitations

- Author identities are normalized names, so homonyms can be merged.
- Only arXiv HTML/LaTeXML papers are accepted; PDFs and papers without usable HTML are excluded.
- Selection and fetching are deliberately sequential to meet arXiv's three-second politeness interval.
- Ingestion throughput is bound by one Ollama GPU stream; running two ingest workers was measured slower than one.
- Papers without arXiv HTML are replaced at selection time (24 negative cache entries in the live run).
