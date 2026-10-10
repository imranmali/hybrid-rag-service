# Tasks: add-indexer

Scope: this change owns `indexer/**` only (plus generated, git-ignored files under `data/cache/` and `data/bench/`). Read-only inputs: `infra/migrations/*.sql`, `infra/models.lock.json`, `.env`. Prerequisite: add-platform-foundation is applied (`make up` works for `postgres` + `flyway`, `.env` exists, both Ollama models pulled). Do not run `git commit`.

## 1. Project setup

- [x] 1.1 Create `indexer/pyproject.toml` (deps per design D1, script `indexer = "indexer.cli:app"`, `[tool.ruff] extend = "../ruff.toml"`, pytest markers `integration` and `network`) and the `indexer/src/indexer/` package skeleton; run `cd indexer && uv sync`; verify `uv run indexer --help` lists `select`, `fetch`, `ingest`, `status`, `bench-index`.
- [x] 1.2 Implement `config.py` (pydantic-settings reading `../.env` then env; fields `INDEXER_DATABASE_URL`, `OLLAMA_HOST_URL`, `OLLAMA_EMBED_MODEL`, `OLLAMA_LLM_MODEL`, `OLLAMA_KEEP_ALIVE`, `LLM_SEED`, `LLM_PROVIDER`, `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL`, `ARXIV_CONTACT`) with a test that `LLM_PROVIDER=anthropic` without a key raises a config error; verify `uv run pytest tests/test_config.py`.
- [x] 1.3 Implement `names.py` (`normalize_author_name`, contract C4) with a parametrized test over all five contract test vectors; verify `uv run pytest tests/test_names.py`.

## 2. Acquisition (corpus-acquisition spec)

- [x] 2.1 Implement `arxiv/ratelimit.py` (3 s min interval, injectable clock) and `arxiv/cache.py` (atomic writes, negative entries); tests with a fake clock assert spacing ≥ 3 s and that cache hits skip the limiter; verify `uv run pytest tests/test_ratelimit.py tests/test_cache.py`.
- [x] 2.2 Implement `arxiv/api.py` (Atom parsing with lxml: id/version, title, abstract, authors, categories, primary category, published/updated) and `arxiv/html_fetch.py` (retries 5/15/45 s honoring `Retry-After`, User-Agent per D7); tests use `respx` + `tests/fixtures/atom_sample.xml` (synthetic) and assert a 429→200 sequence succeeds with one retry; verify `uv run pytest tests/test_arxiv_api.py tests/test_html_fetch.py`.
- [x] 2.3 Implement `arxiv/select.py` + `indexer select --target 220 --seed 42` per design D2; unit test with a synthetic pool asserting determinism (two runs → identical output), prolific-author inclusion (≤ 5 per author, ≤ 8 authors), round-robin ordering and HTML-availability replacement; verify `uv run pytest tests/test_select.py`.

## 3. Parsing and chunking (document-indexing spec)

- [x] 3.1 Implement `parse/html_parser.py` per design D6 against a hand-written LaTeXML-like fixture `tests/fixtures/latexml_sample.html` (abstract, nested sections 3 > 3.2, math with alttext, a table, a figure caption, footnote, bibliography, acknowledgements); tests assert bibliography/footnote/acknowledgement text is absent, math becomes `$…$`, table rows are pipe-joined, and section paths are `3 Method > 3.2 Index construction`; verify `uv run pytest tests/test_html_parser.py`.
- [x] 3.2 Implement `chunk/tokenizer.py` (bge-m3 tokenizer, no special tokens) and `chunk/sentences.py` (abbreviation-aware splitter); table-driven tests including `et al.`, `Fig. 3`, `e.g.` and inline math; verify `uv run pytest tests/test_sentences.py`.
- [x] 3.3 Implement `chunk/chunker.py` per design D5; tests assert every chunk ≤ 448 tokens, overlap ≤ 64 tokens and sentence-aligned, no cross-section chunk, tail merge, long-sentence windowing, ordinals 0..n-1, and identical output on a second run (including `content_sha256`); verify `uv run pytest tests/test_chunker.py`.
- [ ] 3.4 Run `uv run indexer select --target 220 --seed 42` for real (network, ~15–20 min with politeness); verify `indexer/corpus/manifest.jsonl` has 220 lines, each with the five required keys, and `wc -l` per primary category is non-zero for cs.IR, cs.CL and cs.DB. Then run `uv run indexer fetch` twice; verify the second run reports 0 network requests.

## 4. Enrichment, embeddings, writes

- [x] 4.1 Implement `enrich/llm.py` (Ollama `/api/chat` per contract C3; optional Anthropic Messages client used only when `LLM_PROVIDER=anthropic`) and `enrich/context.py` (prompt `ctx-v1`, post-processing, header formatting per design D3); respx tests assert the exact request body (`temperature` 0, `seed` 42, `num_predict` 96, no `format`), the 50-word truncation and the header format; verify `uv run pytest tests/test_context.py`.
- [x] 4.2 Implement `embed/ollama.py` (`/api/embed`, batches ≤ 32, `truncate: true`, dimension check = 1024); respx tests for batching of 70 inputs into 32/32/6 and the dimension-mismatch error; verify `uv run pytest tests/test_embed.py`.
- [x] 4.3 Implement `models_lock.py` (compare live `/api/tags` digests with `../infra/models.lock.json`; untagged names match `:latest`) and `fingerprint.py` (design D8); tests: drift → exit code 3 unless `--allow-model-drift`; fingerprint changes when any field changes and is stable otherwise; verify `uv run pytest tests/test_models_lock.py tests/test_fingerprint.py`.
- [x] 4.4 Implement `store/writer.py` (design D9: per-document transaction, skip rule, `--force`, `--prune`, orphan-author cleanup, `index_metadata` upsert of all 13 keys, `ANALYZE`) and wire `indexer ingest` / `indexer status` in `cli.py` with the JSON summary line and exit codes of design D10.
- [ ] 4.5 Write `tests/test_ingest_integration.py` (marker `integration`): Testcontainers `pgvector/pgvector:pg17`, apply `../infra/migrations/V1__schema.sql` and `V2__roles.sql` as superuser with the three `${…_password}` placeholders substituted, connect as `rag_indexer`, ingest two fixture papers with a fake LLM and a fake deterministic embedder; assert row counts in all five corpus tables, author dedup across papers, all 13 `index_metadata` keys, then re-run → summary `unchanged=2` with zero LLM/embedding calls and unchanged `ingested_at`, then mutate one fixture's text → `updated=1` with replaced chunk ids, then inject a failure mid-document → previous version still present. Verify `uv run pytest -m integration`.

## 5. Real ingestion

- [ ] 5.1 With the stack up (`postgres`, `flyway`), run `cd indexer && uv run indexer ingest --limit 5`; verify the last line is the JSON summary with `inserted: 5`, and `psql "$INDEXER_DATABASE_URL" -c "select count(*), min(token_count), max(token_count) from chunks"` shows max ≤ 448.
- [ ] 5.2 Run `make index` (200 papers); verify `failed` ≤ 10, `select count(*) from documents` ≥ 150, `select count(*) from chunks where embedding_ctx is null` = 0, and `select key from index_metadata` lists all 13 keys. Record wall-clock time in `indexer/README.md`.
- [ ] 5.3 Run `make index` again; verify the summary shows `inserted: 0, updated: 0` and every processed document `unchanged`, and that it finishes in under 60 s.
- [ ] 5.4 Spot-check contextual header quality: `psql … -c "select context_header from chunks order by random() limit 5"`; verify each header contains the title, category, year, a section path and one sentence ≤ 50 words. Paste the five headers into `indexer/README.md` under "Sample headers".

## 6. Index benchmark (index-benchmark spec)

- [x] 6.1 Implement `bench.py` + `indexer bench-index --queries 200 --seed 42` per design D11; a unit test with a tiny in-memory recall computation (exact vs approximate id lists) covers the recall@10 math; verify `uv run pytest tests/test_bench.py`.
- [ ] 6.2 Run `uv run indexer bench-index --queries 200 --seed 42` against the real DB; verify `indexer/bench/index_bench.json` and `index_bench.md` exist with HNSW and IVFFlat build seconds, recall@10 for every ef_search/probes value, and filtered recall rows with selectivity per category and year; verify `\di chunks*` is unchanged before/after.

## 7. Documentation and verification

- [ ] 7.1 Write `indexer/README.md`: commands, environment variables, cache layout, chunking numbers (384/64/64), header format, fingerprint fields, exit codes, ingest timings, sample headers, known limitations (author homonyms, HTML-only papers). Verify every command in it runs as written.
- [ ] 7.2 Final verification from `indexer/`: `uv run ruff check . && uv run ruff format --check . && uv run pytest` (all markers, Docker running) all pass; `make lint` and `make test` from the repo root include the indexer and pass.
