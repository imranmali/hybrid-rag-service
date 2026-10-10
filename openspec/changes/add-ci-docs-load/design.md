# Design: add-ci-docs-load

## Context

See proposal.md — Why. The Makefile commands (C10), replay/snapshot mode (C9), eval configs (C7), query API (C6) and numbers (C11) are fixed by `add-platform-foundation/design.md` → "Cross-change contracts" and copied verbatim below. By the time this change runs, the other five are implemented: `make test`/`make lint` cover all packages, `evals/results.md`, `evals/results_filter_pushdown.md`, `indexer/bench/index_bench.md`, `evals/thresholds/ci.yaml`, `evals/cassettes/ci/` and `evals/ci/snapshot.lock.json` exist.

## Goals / Non-Goals

**Goals:**
- A CI eval gate that is deterministic, fast (< 15 min) and honest about what it does and does not cover.
- A README a reviewer can scan in 60 seconds; ADRs that cite real numbers.

**Non-Goals:**
- Running real models in CI; nightly live-model runs (possible later on a self-hosted runner).

## Decisions

### D1. CI eval gate = deterministic replay (not live Ollama)
Options considered:
1. **Ollama service container with the real models** — llama3.1:8b on 4 vCPU without a GPU runs at a few tokens/s; routing alone (50 classifications × the routing row, plus SQL generation) and pulling ~6 GB of models per run push the job past 30–60 min, and generation/judging would take hours. Rejected.
2. **Ollama with small substitute models** (e.g. a 0.5B LLM, a small embedder) — fast, but the gate would measure a different system with different thresholds, and an index embedded with bge-m3 cannot be queried with another embedder. Rejected.
3. **Replay (chosen)** — contract C9: restore the real index from a data-only snapshot, run the service in exact-vector-search mode, and serve recorded Ollama/reranker responses from WireMock. Same service code, same SQL, same fusion/filter/routing/guard logic, same models' recorded outputs → bit-identical metrics to the local recording. It catches every regression in *our* code (retrieval SQL, fusion, filter extraction and pushdown, routing parse, SQL guard, rerank merge). It does not catch model drift (guarded locally by `infra/models.lock.json` digests) and does not measure generation quality (faithfulness/relevance are local-only; generation is off in the gate).
Cost: an intentional change to what the service sends a model (e.g. different fused candidates reaching the reranker) causes cassette misses; the gate exits 2 with instructions to run `make eval-record-ci` locally and commit the new cassettes and thresholds. This is the classic VCR trade-off, accepted and documented in the README CI section and the workflow header comment.

### D2. Workflow layout (`.github/workflows/ci.yml`)
- Triggers: `push` to `main`, `pull_request`; `concurrency: ci-${{ github.ref }}`, `cancel-in-progress: true`; `permissions: contents: read`.
- `python` — `strategy.matrix.package: [indexer, reranker, evals]`; `astral-sh/setup-uv@v6` (`python-version: "3.12"`, cache enabled); `actions/cache` for `~/.cache/huggingface` (bge-m3 tokenizer); `uv sync --frozen`; `uv run ruff check .`; `uv run ruff format --check .`; `uv run pytest -m "not model and not live"` (indexer integration tests use Docker, available on `ubuntu-latest`).
- `kotlin` — `actions/setup-java@v4` (`temurin`, `25`), `gradle/actions/setup-gradle@v4`; `cd service && ./gradlew ktlintCheck build --no-daemon`; upload test reports on failure.
- `docker` — `docker/setup-buildx-action@v3`; `docker/build-push-action@v6` for `reranker/` and `service/` with `push: false`, `cache-from/to: type=gha`.
- `eval-gate` — `needs: [python, kotlin]`, `timeout-minutes: 30`, `permissions: contents: read` (release download uses `GITHUB_TOKEN`): checkout; setup-uv; `cp .env.example .env`; read `evals/ci/snapshot.lock.json` with `jq` (fail with a "publish the snapshot: make snapshot && gh release create <tag> <asset>" message if absent); `gh release download "$TAG" -p "$ASSET" -D data/snapshots` with `GH_TOKEN`; `echo "$SHA  data/snapshots/$ASSET" | sha256sum -c`; `make eval-ci SNAPSHOT=data/snapshots/$ASSET`; on exit code 2 print the re-record hint; `if: failure()` → `docker compose … logs > compose.log` and `actions/upload-artifact@v4` of `evals/runs/ci/` + `compose.log`; `if: always()` → `make down`.
- `load-lint` — `docker run --rm -v "$PWD/load:/load" grafana/k6:0.54.0 inspect /load/query.js`.
- `docs` — `xmllint --noout docs/architecture.svg`; `.github/scripts/check_readme.sh` asserts the README section order (the seven headings/anchors of D4 in order) and emits `::warning file=…,line=…::unfilled placeholder` for each `{{RESULT:`; never fails on placeholders.

### D3. k6 load script (`load/query.js`)
- `K6_TARGET` (default `http://localhost:8080`); questions from `/dataset/questions.v1.jsonl` (mounted by `make load`; fallback path `../evals/dataset/questions.v1.jsonl`) via `SharedArray`; deterministic question order (index = iteration counter mod n, offset 42).
- Pipelines are copied literally from `evals/configs/04-contextual.yaml`, `05-rerank.yaml`, `06-routing.yaml` (generation disabled for the first two), as JS constants with a comment naming the source file.
- Scenarios (executor `ramping-arrival-rate` / `constant-arrival-rate`, started sequentially via `startTime`):
  - `retrieval`: ramp 1 → 20 rps over 30 s, hold 20 rps for 60 s; `preAllocatedVUs: 40`; threshold `http_req_duration{scenario:retrieval}: p(95)<500`.
  - `rerank`: 0.5 rps (`rate: 1, timeUnit: '2s'`) for 120 s; `p(95)<12000`.
  - `full`: 0.05 rps (`rate: 1, timeUnit: '20s'`) for 200 s; `p(95)<60000`.
  - Global: `http_req_failed: rate<0.01`. `SCENARIO=<name>` keeps only that scenario.
- Custom metrics: `rag_degraded` (Rate), `rag_stage_<key>` Trends from `timings_ms` (dots replaced by underscores).
- `handleSummary` writes `/load/results/summary-<scenario|all>-<ISO timestamp>.json` and prints `textSummary`.
- 20 rps matches the Hikari sizing rationale in add-query-service D3.

### D4. README skeleton (BUILD_SPEC order, exact)
1. One sentence (no heading above it except the title): "Hybrid RAG service over ~200 arXiv full-text papers (cs.IR, cs.CL, cs.DB): metadata-filtered BM25 + vector retrieval on Postgres/pgvector, cross-encoder reranking and SQL/vector routing — Kotlin serving layer, Python eval harness."
2. `## Results` — table `| # | Config | nDCG@10 | Recall@20 | p95 latency | Cost/query |` with six rows (`01-vector` … `06-routing`), every cell `{{RESULT: <metric> row <n> | source: evals/results.md}}`; one line under it linking `evals/results.md` and the dataset card.
3. `## Architecture` — `![Architecture](docs/architecture.svg)`.
4. `## Findings` — three bullets, shape "X changed nDCG@10 from {{…}} to {{…}} for {{…}} ms p95", one explicitly negative/neutral (placeholder text instructs the filler to choose the honest negative result from `evals/results.md` or `evals/results_filter_pushdown.md`).
5. `## Run it` — `cp .env.example .env` · `make models` · `make up` (= `docker compose up`, notes first-start reranker download) · `make index` · `make query Q="…"`; prerequisites line (Docker, Ollama, uv, JDK 21+).
6. `## Latency budget` — `| Stage | Timeout (budget) | p50 | p95 |` for route, embed, retrieve.vector, retrieve.bm25, fuse, rerank, generate, total; timeouts filled now from C11, p50/p95 placeholders sourced from the `06-routing` run in `evals/results.md`.
7. `## Decisions` — seven one-line ADR links.
Below the fold: `## Trace` (`docs/img/trace-waterfall.png`, placeholder note), `## Evaluation` (dataset link, honest verification note, judge caveat), `## CI eval gate` (D1 in four sentences), `## Non-goals` (BUILD_SPEC list, one line each), `## Deferred on purpose` (multi-tenancy, incremental re-embedding, streaming, query caching, multi-turn rewriting — one sentence each on what we would do), `## Repo layout`. HNSW build time line placeholder sourced from `indexer/bench/index_bench.md` (BUILD_SPEC: "note the build time in the README").

### D5. ADRs
Format: `# ADR 000N: <title>`, `Status: Accepted (2026-10)`, `## Context`, `## Decision`, `## Consequences`; 120–200 words of body. Numbers only via placeholders with sources:
- 0001 Postgres + pgvector over Elasticsearch/vector DB — one datastore for vectors, full text and relational filters; filters join in SQL.
- 0002 HNSW over IVFFlat — `indexer/bench/index_bench.md` build seconds and recall@10.
- 0003 Kotlin serving, Python offline — the seam sentence from BUILD_SPEC; read-only roles.
- 0004 Self-hosted cross-encoder — `evals/results.md` rows 4→5 nDCG@10 and p95 rerank; CPU trade-off, 20 candidates, fallback model.
- 0005 Contextual chunking and chunk size — 384/64, doc-level summary + section path, two-column ablation; rows 3→4.
- 0006 Pre-filter vs over-fetch-and-post-filter — threshold 0.25, factor 5; `evals/results_filter_pushdown.md` and the filtered-recall table in `indexer/bench/index_bench.md`.
- 0007 Why no agent loop — unbounded retrieval without evals; corrective gate (#7) as the bounded alternative.

### D6. Architecture SVG
Hand-written SVG (no generator dependency), `viewBox="0 0 1200 700"`, white background rect, system font stack, ≤ 50 KB. Left: offline lane (arXiv API/HTML → indexer [parse · chunk 384/64 · header · embed ×2] → Postgres). Centre: online lane — client → Kotlin service with boxes `route` → (`sql.execute` branch to structured tables) / `retrieve.vector` ∥ `retrieve.bm25` → `fuse` (RRF k=60) → `rerank` (top 20) → `generate`; dashed arrows for degraded paths. Right: dependencies — host Ollama (bge-m3, llama3.1:8b), reranker container (bge-reranker-v2-m3), Postgres (pgvector + tsvector + metadata tables); bottom: OTel collector → Tempo → Grafana. Text as real `<text>` elements (searchable, accessible); `<title>` and `<desc>` set.

### Contracts used (copied verbatim from add-platform-foundation)

<!-- CONTRACT:replay -->
Purpose: run the 50-question eval gate on GitHub runners (no Ollama, no GPU) deterministically.

- **Snapshot.** `make snapshot` writes `data/snapshots/rag-index-<first 12 hex of pipeline_fingerprint>.dump` = `pg_dump --data-only --format=custom --exclude-table=flyway_schema_history` of the `rag` DB (run inside the `postgres` container as `rag_owner`) and prints its sha256. `make snapshot-restore SNAPSHOT=<path>` (as `rag_owner`, into a DB that Flyway has already migrated) first runs `truncate documents, authors, paper_authors, paper_categories, chunks, index_metadata restart identity cascade`, then `pg_restore --data-only --disable-triggers --single-transaction`, then `ANALYZE` — so it works on a fresh CI database and on a populated local one alike. `data/snapshots/` is git-ignored; CI downloads the file from a GitHub Release described by `evals/ci/snapshot.lock.json`:
  ```json
  {"release_tag": "index-snapshot-<12hex>", "asset_name": "rag-index-<12hex>.dump", "sha256": "<64-hex>", "pipeline_fingerprint": "<64-hex>", "document_count": 212, "chunk_count": 5480}
  ```
- **Exact search.** With `RAG_RETRIEVAL_EXACT_VECTOR_SEARCH=true` the service disables index scans for vector queries (`SET LOCAL enable_indexscan = off`, `SET LOCAL enable_bitmapscan = off` in the vector-query transaction), so dense retrieval is an exact, deterministic sort (ties by `chunk_id`). HNSW graphs are rebuilt non-deterministically on restore, so ANN would not be replayable.
- **Overlay.** `infra/docker-compose.replay.yml` adds `wiremock` and overrides `service` env: `RAG_OLLAMA_URL=http://wiremock:8080`, `RAG_RERANKER_URL=http://wiremock:8080`, `RAG_RETRIEVAL_EXACT_VECTOR_SEARCH=true`, `OTEL_TRACES_EXPORTER=none`.
  - `WIREMOCK_MODE=record` (default for `make eval-record-ci`): wiremock loads proxy stubs from `infra/wiremock/proxy-mappings/` (`/api/.*` → `http://host.docker.internal:11434`, `/rerank` and `/healthz` → `http://reranker:8080`) and journals traffic; the reranker container runs.
  - `WIREMOCK_MODE=replay` (`make eval-ci`): wiremock loads only `evals/cassettes/ci/mappings/` (mounted read-only); unmatched requests return 404, which the service treats as a dependency failure (→ a `degraded` code), which the gate counts as a failure.
- **Recording.** `make eval-record-ci` = up with overlay in record mode → `uv run evals grid --subset ci --no-generation --no-judge` → `uv run evals cassettes snapshot` (calls `POST /__admin/recordings/snapshot` with `requestBodyPattern` `equalToJson`, `ignoreArrayOrder: false`, `ignoreExtraElements: false`, dedup identical requests, `persist: false`, writes stub files to `evals/cassettes/ci/mappings/` and `evals/cassettes/ci/meta.json` with recording time, git sha, model digests from `/api/tags`, reranker model/revision, snapshot sha256).
- **Gate.** `make eval-ci` = restore snapshot → up with overlay in replay mode → `uv run evals grid --subset ci --no-generation --no-judge --replay --gate thresholds/ci.yaml`. `--replay` makes the runner take model digests from `evals/cassettes/ci/meta.json` instead of live Ollama. Exit code 0 = pass, 1 = threshold breach, 2 = any degraded response (cassette miss), 3 = setup error.
<!-- /CONTRACT:replay -->

<!-- CONTRACT:make -->
Root `Makefile` (owned by add-platform-foundation). `COMPOSE = docker compose --env-file .env -f infra/docker-compose.yml`. Targets and the package commands they call (each package change MUST provide the command exactly as written):

| Target | Runs | Command provided by |
|---|---|---|
| `make doctor` | `infra/scripts/doctor.sh` — checks docker daemon, `.env` present, Ollama reachable at `OLLAMA_HOST_URL`, both models pulled, `uv`, JDK ≥ 21 | foundation |
| `make models` | `ollama pull bge-m3 && ollama pull llama3.1:8b` | foundation |
| `make models-lock` | `python3 infra/scripts/models_lock.py > infra/models.lock.json` | foundation |
| `make up` | `$(COMPOSE) up -d --build --wait` | foundation |
| `make down` | `$(COMPOSE) down` (`make clean` adds `-v`) | foundation |
| `make index` | `cd indexer && uv run indexer ingest --limit $(LIMIT)` (`LIMIT ?= 200`) | add-indexer |
| `make query Q="…"` | `curl -sS -X POST $(RAG_SERVICE_URL)/v1/query -H 'content-type: application/json' -d "$$(jq -n --arg q "$(Q)" '{question:$$q}')" \| jq` | add-query-service |
| `make eval` | `cd evals && uv run evals grid` | add-eval-harness |
| `make eval-record-ci` | see C9 | add-eval-harness |
| `make eval-ci` | see C9 | add-eval-harness |
| `make snapshot` / `make snapshot-restore SNAPSHOT=…` | see C9 | foundation |
| `make test` | for each of `indexer reranker evals` with a `pyproject.toml`: `uv run pytest`; if `service/gradlew` exists: `cd service && ./gradlew test` | each package |
| `make lint` | for each Python package: `uv run ruff check . && uv run ruff format --check .`; `cd service && ./gradlew ktlintCheck` | each package |
| `make load` | `docker run --rm -v $(CURDIR)/load:/load -v $(CURDIR)/evals/dataset:/dataset:ro -e SCENARIO -e K6_TARGET=http://host.docker.internal:$(SERVICE_HOST_PORT) grafana/k6:0.54.0 run /load/query.js` (`-e SCENARIO` forwards the caller's `SCENARIO`, so `SCENARIO=retrieval make load` runs one scenario) | add-ci-docs-load |

`make test` and `make lint` skip packages that do not exist yet, so the Makefile works at every stage of the build.
<!-- /CONTRACT:make -->

<!-- CONTRACT:eval-config -->
One file per config in `evals/configs/<name>.yaml`. The `pipeline` block is REQUIRED and COMPLETE (every key of the `/v1/query` `pipeline` object) and is sent verbatim as the request's `pipeline`; the runner validates completeness before any request.

```yaml
name: 05-rerank                      # required; unique; equals the filename stem
description: "+ cross-encoder rerank (top 20 of fused 50)"
grid_row: 5                          # 1..6 for ablation rows; omitted for auxiliary configs
question_types: [factoid, multi_chunk, filter, aggregate, unanswerable]   # optional; default all types
pipeline:
  retrieval: {vector: true, bm25: true, vector_k: 50, bm25_k: 50, fusion: {method: rrf, k: 60}, return_k: 50}
  contextual: true
  filters: {enabled: true, extract_from_question: true, strategy: auto, selectivity_threshold: 0.25, overfetch_factor: 5, explicit: null}
  rerank: {enabled: true, candidates: 20}
  routing: {enabled: false}
  generation: {enabled: true, context_k: 6}
judge:
  enabled: true                      # faithfulness + answer relevance
  provider: ollama                   # ollama | anthropic
  model: llama3.1:8b
run:
  seed: 42
  concurrency: 1
  warmup_queries: 3                  # untimed queries before measurement
  timeout_s: 120                     # client-side per-request timeout
```

The six grid configs differ ONLY in these switches (all other values exactly as above, `generation.enabled: true` in every row):

| grid_row | name | retrieval.vector | retrieval.bm25 | filters.enabled | contextual | rerank.enabled | routing.enabled |
|---|---|---|---|---|---|---|---|
| 1 | `01-vector` | true | false | false | false | false | false |
| 2 | `02-hybrid` | true | true | false | false | false | false |
| 3 | `03-filters` | true | true | true | false | false | false |
| 4 | `04-contextual` | true | true | true | true | false | false |
| 5 | `05-rerank` | true | true | true | true | true | false |
| 6 | `06-routing` | true | true | true | true | true | true |

Auxiliary configs (not grid rows; `question_types: [filter]`): `filter-pre`, `filter-post`, `filter-auto` = row 4's pipeline with `filters.strategy` set to `pre`, `post`, `auto` respectively and `generation.enabled: false`, `judge.enabled: false` — used for the pre-filter vs post-filter benchmark (ADR 0006).
<!-- /CONTRACT:eval-config -->

<!-- CONTRACT:numbers -->
| Parameter | Value | Where used |
|---|---|---|
| Chunk size | 384 tokens (bge-m3 tokenizer), section-bounded, sentence-aligned | indexer |
| Chunk overlap | 64 tokens (whole sentences, ≤ 64 tokens) | indexer |
| Min chunk | 64 tokens (shorter section tails merge into the previous chunk) | indexer |
| Context summary | 1 LLM call per document, ≤ 50 words, prompt version `ctx-v1` | indexer |
| Embedding batch | 32 inputs per `/api/embed` call | indexer |
| Dense / lexical candidates | `vector_k` = 50, `bm25_k` = 50 | service |
| RRF k | 60 | service |
| `return_k` | 50 (supports recall@50) | service |
| Rerank candidates | 20 (top 20 of the fused list; CPU latency budget) | service, reranker |
| Generation context | 6 chunks | service |
| Selectivity threshold | 0.25 of chunks; below → pre-filter | service |
| Over-fetch factor | 5 (→ 250 ANN candidates, `hnsw.ef_search` = 250) | service |
| `hnsw.ef_search` without filters | 100 (must be ≥ `vector_k`; pgvector default 40 would cap results at 40) | service |
| HNSW build | m = 16, ef_construction = 64 | schema |
| Stage timeouts | route 5 000 ms · embed 2 000 ms · filter.estimate 500 ms · retrieve.vector 1 500 ms · retrieve.bm25 1 500 ms · rerank 10 000 ms · generate 45 000 ms · sql.generate 15 000 ms · sql.execute 2 000 ms · HTTP connect 500 ms for every dependency | service |
| Retries | embedding: 2 attempts, 100 ms backoff · reranker: 2 attempts, 200 ms · LLM: 2 attempts, 250 ms. All three retry only on connect failure or HTTP 5xx, never on read timeout, HTTP 4xx or an open breaker | service |
| Circuit breakers (`embedding`, `reranker`, `llm`) | count-based window 20, minimum calls 10, failure-rate threshold 50 %, open 30 s, 3 half-open calls; timeouts count as failures; slow-call detection off | service |
| HikariCP | reader pool 10 (min idle 2), router pool 3; connectionTimeout 1 000 ms; maxLifetime 30 min | service |
| SQL guard | single SELECT/WITH, table allowlist, wrapper `LIMIT 101` (return ≤ 100, `truncated` flag), statement timeout 2 s, max 4 000 chars | service |
| LLM options | temperature 0, seed 42, num_ctx 8192 | all LLM callers |
| Reranker | max_length 512, batch 16, max passages 100, max concurrency 1 | reranker |
| arXiv politeness | ≥ 3 s between requests; retries on 429/5xx at 5 s, 15 s, 45 s | indexer |
| Corpus | manifest of 220 papers (cs.IR, cs.CL, cs.DB, 2023–2026); `make index` ingests the first 200 | indexer |
| Dataset | 150 = 60 factoid / 37 multi_chunk / 30 filter / 15 aggregate / 8 unanswerable; CI subset 50 = 20/12/10/5/3 | evals |
| CI regression tolerance | baseline − 0.03 for nDCG@10 and recall@20; − 0.05 route accuracy; − 0.10 SQL answer accuracy; 0 degraded responses | evals, CI |
<!-- /CONTRACT:numbers -->

## Risks / Trade-offs

- [Snapshot release asset not yet published when CI first runs] → job fails with publishing instructions (fail-closed: a skipped gate would look like a pass). Orchestrator step: `make snapshot && gh release create index-snapshot-<12hex> data/snapshots/rag-index-<12hex>.dump --notes "DB snapshot for CI eval gate"`.
- [Replay does not test model behaviour] → stated in README and ADR-free CI section; local `make eval` remains the source of quality numbers.
- [Service image build inside the gate adds minutes] → buildx gha cache; total target < 15 min.
- [k6 thresholds tuned to one machine] → load test is local-only; thresholds documented as this machine's budget.
- [README placeholders forgotten] → `docs` job warns per placeholder; orchestrator checklist in tasks.

## Migration Plan

Merge, publish the snapshot release, confirm the `eval-gate` job is green, then mark it a required check.

## Open Questions

None.
