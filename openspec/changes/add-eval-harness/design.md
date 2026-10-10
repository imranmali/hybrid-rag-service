# Design: add-eval-harness

## Context

See proposal.md — Why. The service API (C6), eval config schema and grid (C7), dataset record schema (C8), CI replay/snapshot mode (C9), Makefile commands (C10) and numbers (C11) are fixed by `add-platform-foundation/design.md` → "Cross-change contracts" and copied verbatim below. The harness runs on the host (`cd evals && uv run evals …`), calls the service at `RAG_SERVICE_URL`, reads the DB as `rag_reader` via `EVALS_DATABASE_URL`, and calls host Ollama for generation, judging and digest checks.

## Goals / Non-Goals

**Goals:**
- One command per result: `make eval` (full grid), `make eval-ci` (gate), `uv run evals filter-bench`.
- Every number traceable to a run file with seed, digests, dataset hash and git sha.
- Honest dataset provenance; drift detected, never silently tolerated.

**Non-Goals:**
- Parallel query execution (latency measurements need an uncontended service; concurrency is 1).
- Statistical significance testing beyond reporting n per metric (noted as a limitation in results.md).

## Decisions

### D1. Layout and dependencies
`evals/pyproject.toml` (name `evals`, Python 3.12, script `evals = "evals.cli:app"`, ruff `extend = "../ruff.toml"`, pytest marker `live`), `evals/uv.lock`, source `evals/src/evals/`: `cli.py`, `settings.py` (pydantic-settings, loads `../.env`), `config_schema.py` (pydantic models of C7 with `extra="forbid"` at every level), `dataset/{schema.py, sample.py, generate.py, aggregate.py, precheck.py, review.py, check_filters.py, freeze.py}`, `client.py` (httpx service client), `ollama.py` (chat + tags; optional Anthropic), `metrics/{retrieval.py, judge.py, routing.py, latency.py, cost.py}`, `runner.py`, `report.py` (results.md), `drift.py`, `thresholds.py`, `gate.py`, `cassettes.py`, `ci_lock.py`, `filter_bench.py`, `judge_agreement.py`.
Committed data: `evals/configs/*.yaml`, `evals/dataset/{questions.v1.jsonl, MANIFEST.json, DATASET_CARD.md, human_labels.template.jsonl}`, `evals/dataset/work/{samples.jsonl, candidates.jsonl, precheck.jsonl, verification.jsonl}` (provenance), `evals/pricing.yaml`, `evals/results.md`, `evals/results_filter_pushdown.md`, `evals/runs/**`, `evals/thresholds/ci.yaml`, `evals/cassettes/ci/**`, `evals/ci/snapshot.lock.json`.
Deps: `httpx`, `typer`, `pydantic>=2`, `pydantic-settings`, `python-dotenv`, `pyyaml`, `psycopg[binary]>=3.2`, `numpy`. Dev: `pytest`, `respx`, `ruff`.

### D2. CLI surface
| Command | Purpose |
|---|---|
| `evals dataset sample --seed 42` | seeded chunk samples per type (×2 oversampling) → `work/samples.jsonl` |
| `evals dataset generate [--type T] [--n N]` | LLM candidates + SQL-template aggregates → `work/candidates.jsonl` |
| `evals dataset precheck` | LLM answerability check → `work/precheck.jsonl` |
| `evals dataset check-filters` | calls the service (filters on, generation/rerank/routing off) and compares `filters.extracted` with `expected_filters` |
| `evals dataset review [--next N \| --id ID]` | prints candidate, reference answer and gold chunk text (unanswerable: service top-5 chunks) for the verifier |
| `evals dataset verdict --id ID --status accept\|reject\|edit [--question …] [--answer …] --notes …` | appends a verdict (with `by`) to `work/verification.jsonl` |
| `evals dataset freeze --version v1 --seed 42` | quota selection, ids, `ci_subset`, manifest, card |
| `evals dataset validate` | schema + counts + manifest hash check |
| `evals run --config <yaml> [--subset ci] [--limit N] [--no-generation] [--no-judge] [--replay] [--allow-model-drift] [--allow-drift]` | one config |
| `evals grid [same flags] [--gate thresholds/ci.yaml]` | the six grid configs in order |
| `evals report` | rebuild `results.md` from the latest full run per config |
| `evals filter-bench` | `filter-pre/post/auto` → `results_filter_pushdown.md` |
| `evals thresholds --from runs/ci/<run_id>` | write `thresholds/ci.yaml` |
| `evals cassettes snapshot` | wiremock snapshot → `cassettes/ci/` (C9) |
| `evals ci-lock --snapshot <path> [--release-tag T]` | write `ci/snapshot.lock.json` |
| `evals judge-agreement` | agreement vs `dataset/human_labels.jsonl` if present, else prints "pending" |

Exit codes everywhere: 0 ok, 1 threshold breach, 2 degraded/failed requests under `--gate`, 3 setup/validation/drift error.

### D3. Dataset generation
- Sampling (rag_reader, `random.Random(42)`): factoid — chunks with `token_count ≥ 150`, at most one per paper first pass; multi_chunk — pairs/triples from one paper, different top-level sections (first segment of `section_path`); filter — chunks from papers whose metadata gives a usable phrase, rotating year (40 %), author with ≥ 2 papers (40 %), category + year (20 %); unanswerable — topic seeds from category names only (no chunk).
- LLM prompts (`format: json`, `num_predict` 512, temperature 0, seed 42, model `OLLAMA_LLM_MODEL` or Anthropic if `LLM_PROVIDER=anthropic`, recorded in `source.generator`): `qgen-factoid-v1`, `qgen-multi-v1` (must need every chunk), `qgen-filter-v1` (must contain the phrase verbatim, e.g. `by Jimmy Lin`, `in 2024`, `cs.IR papers from 2025`), `qgen-unanswerable-v1` (plausible, specific, about a system/result not in the corpus). Output `{"question", "answer"}`; questions must be self-contained (no "this paper").
- Aggregate templates (expected answers computed by SQL, never by the LLM): T1 papers by author (count); T2 papers in category in year (count, membership via `paper_categories`); T3 papers by author in year (count); T4 author with most papers in category (only when no tie); T5 titles by author (JSON list; authors with 2–4 papers); T6 papers cross-listed in two categories (count). 30 candidates.
- `expected_filters` for filter questions = the phrase's metadata (author normalized per C4; year → Jan 1 – Dec 31).
- Precheck (`precheck-v1`): "Do these passages fully answer the question with this answer?" → `{"verdict": "yes"|"no"}`; unanswerable: service top-5 hybrid chunks must yield "no".

### D4. Agent verification (locked decision: agent-assisted, human pending)
The implementing agent works through `evals dataset review --next 20` batches and records one `verdict` per prechecked candidate, checking: question self-contained and unambiguous; reference answer correct and fully supported by the gold chunk text; multi-chunk truly needs ≥ 2 chunks; filter phrase present and `check-filters` matching; unanswerable truly unanswerable from the top-5 chunks; aggregate question wording matches its SQL. `by` = `agent:<model id>`. Edits keep the original text in `notes`. Freeze refuses if any type lacks enough accepted candidates and tells the operator which `generate --type` to rerun.

### D5. Freeze
Seeded selection per type to the quota (60/37/30/15/8), ids `q0001…q0150` in type order then random, `ci_subset` 20/12/10/5/3 seeded, `content_sha256` copied from the DB at freeze time. `MANIFEST.json`: `{version, created_at, sha256, counts, ci_counts, seed, generator, generator_digest, verifier, index_pipeline_fingerprint}`. `DATASET_CARD.md`: purpose, mix, generation method, precheck, agent verification, "human review pending", known biases (generator = judge model family; author homonyms), how to regenerate. `human_labels.template.jsonl`: 30 seeded answerable ids for the BUILD_SPEC judge-agreement subset.

### D6. Runner
1. Load + validate config (pydantic, `extra="forbid"`, completeness of `pipeline`); load dataset; filter by `question_types` and `--subset`.
2. Preflight: `GET /v1/info`; model digests — live `/api/tags` vs `infra/models.lock.json` (or `cassettes/ci/meta.json` with `--replay`); reranker revision from `/v1/info` vs lock (warning only); gold drift — one query `select d.external_id, c.ordinal, c.content_sha256 from chunks c join documents d …` over all gold pairs.
3. Warmup: `run.warmup_queries` fixed questions (`What is BM25?`, `What is dense retrieval?`, `What is a cross-encoder?`) with the config's pipeline, untimed.
4. Per question (sequential): `POST /v1/query {question, pipeline}` (`--no-generation` sets `pipeline.generation.enabled=false`), client timer `time.perf_counter`, `run.timeout_s`; store compact response (chunk `(external_id, ordinal, chunk_id, scores)`, route, filters, sql, answer, degraded, timings, usage).
5. Judge (if `judge.enabled` and not `--no-judge`, and `answer.status == "ok"`): `faith-v1` (question, the exact `context_k` passages or SQL rows, answer → supported/partially_supported/unsupported) and `rel-v1` (question, answer → relevant/partially_relevant/irrelevant), `format: json`, `num_predict` 128, temperature 0, seed 42. Abstained answers: faithfulness 1, relevance 0 on answerable questions.
6. Metrics (D7), write run file, upsert results row (full dataset, non-replay only).

### D7. Metric definitions
- Gold set G, ranked list L (≤ 50). `recall@k = |G ∩ L[:k]| / |G|`; `nDCG@10 = DCG/IDCG`, `DCG = Σ_{i≤10, L_i∈G} 1/log2(i+1)`, `IDCG` over `min(|G|,10)` hits; `RR = 1/rank of first hit` (0 if none). Means over questions with gold (n reported); a mis-routed SQL response counts as an empty list.
- Faithfulness mean over judged answers (all types); answer relevance mean over judged answerable questions; abstention accuracy = abstained / unanswerable; false abstention = abstained / answerable with `answer.status == "ok"`.
- Route accuracy over all questions; confusion `{expected: {sql: {sql, vector}, vector: {sql, vector}}}`; SQL answer accuracy over aggregate questions: correct iff `route == "sql"` and (scalar: some cell equals the expected value after numeric/whitespace/case normalization; list: the set of first-column values equals the expected set, case-insensitive).
- Latency: `numpy.percentile` (linear) p50/p95 of client e2e, server `total` and each `timings_ms` key (over responses containing it).
- Cost per query: `embed_prompt_tokens × embedding_per_1m/1e6 + llm_prompt_tokens × llm_prompt_per_1m/1e6 + llm_completion_tokens × llm_completion_per_1m/1e6 + rerank_pairs × rerank_per_1k/1e3`; judge tokens excluded (evaluation overhead). `pricing.yaml` defaults all to 0 with a comment explaining the estimate.
- Degraded rate (non-empty `degraded`), counts per code; error rate (non-200).

### D8. Output formats
- Run id `YYYYMMDDTHHMMSSZ-<gitsha7>` (grid: one id for all six configs); files `evals/runs/<run_id>/<config>.json` (CI: `evals/runs/ci/<run_id>/…`) plus `grid.json` summary.
- `results.md` (generated, header says "do not edit by hand"): dataset line (path, n, sha256, "human review pending"); main table `| # | Config | nDCG@10 | R@5 | R@20 | R@50 | MRR | Faithfulness | Answer rel. | Route acc. | SQL acc. | Abstain acc. | p50 e2e ms | p95 e2e ms | p95 rerank ms | Cost/query (est.) | Tokens/query | Degraded | Run |` sorted by `grid_row`; per-stage latency table; `06-routing` confusion matrix; provenance table (run id, seed, digests, git sha, dirty).
- `results_filter_pushdown.md`: `| Strategy | n | R@20 | nDCG@10 | retrieve.vector p50 | p95 | underfilled % | strategies chosen |`.

### D9. CI gate pieces (C9)
- `thresholds/ci.yaml`: `{baseline_run, tolerance: {ndcg_at_10: 0.03, recall_at_20: 0.03, route_accuracy: 0.05, sql_accuracy: 0.10}, max_degraded: 0, configs: {01-vector: {ndcg_at_10, recall_at_20}, …, 06-routing: {ndcg_at_10, recall_at_20, route_accuracy, sql_accuracy}}}`, values = round(baseline − tolerance, 3), floor 0.
- `cassettes snapshot`: `POST http://localhost:8089/__admin/recordings/snapshot` with `{"persist": false, "repeatsAsScenarios": false, "requestBodyPattern": {"matcher": "equalToJson", "ignoreArrayOrder": false, "ignoreExtraElements": false}}`; empty `cassettes/ci/mappings/` first, write each returned mapping as `<id>.json` (bodies inline), write `meta.json`. Proxy mappings themselves are excluded.
- Recording and replay both run with the service in exact-vector-search mode against the same restored snapshot, so dense results — and therefore reranker request bodies — are identical.

### Contracts used (copied verbatim from add-platform-foundation)

<!-- CONTRACT:query-api -->
Base URL: `http://service:8080` (containers) / `http://localhost:8080` (host, `RAG_SERVICE_URL`).

#### `POST /v1/query`

Request (`Content-Type: application/json`). Only `question` is required; every omitted `pipeline` field takes the default shown, and the defaults are the full pipeline (grid row 6). Unknown fields anywhere in the body → `400` (strict parsing, so a typo in an eval config cannot silently disable a component). `explicit: null` is allowed.

```json
{
  "question": "Which rerankers did Jimmy Lin's 2024 cs.IR papers evaluate?",
  "pipeline": {
    "retrieval": {
      "vector": true,
      "bm25": true,
      "vector_k": 50,
      "bm25_k": 50,
      "fusion": {"method": "rrf", "k": 60},
      "return_k": 50
    },
    "contextual": true,
    "filters": {
      "enabled": true,
      "extract_from_question": true,
      "strategy": "auto",
      "selectivity_threshold": 0.25,
      "overfetch_factor": 5,
      "explicit": {
        "categories": ["cs.IR"],
        "authors": ["Jimmy Lin"],
        "published_from": "2024-01-01",
        "published_to": "2024-12-31"
      }
    },
    "rerank": {"enabled": true, "candidates": 20},
    "routing": {"enabled": true},
    "generation": {"enabled": true, "context_k": 6}
  }
}
```

Defaults: as shown, except `filters.explicit` defaults to `null`.

Validation (→ `400`): `question` 1..2000 chars after trim; at least one of `retrieval.vector` / `retrieval.bm25` true; `vector_k`, `bm25_k`, `return_k` ∈ 1..200; `fusion.method` = `"rrf"`; `fusion.k` ∈ 1..1000; `filters.strategy` ∈ {`auto`,`pre`,`post`}; `selectivity_threshold` ∈ [0,1]; `overfetch_factor` ∈ 1..20 and `vector_k × overfetch_factor` ≤ 1000; `explicit.categories[]` match `^[a-z-]+(\.[A-Za-z-]+)?$`; `published_from`/`published_to` ISO dates with from ≤ to; `rerank.candidates` ∈ 1..100 and ≤ `return_k`; `generation.context_k` ∈ 1..20.

Semantics of each switch:
- `retrieval.vector` / `retrieval.bm25`: run the dense leg (`retrieve.vector`, top `vector_k`) and/or the lexical leg (`retrieve.bm25`, top `bm25_k`). If both: Reciprocal Rank Fusion `score = Σ 1/(k + rank)` (ranks 1-based, ties broken by lower `chunk_id`), truncated to `return_k`. If one: that leg's order, truncated to `return_k`; no `fuse` stage.
- `contextual`: `false` → `embedding` + `tsv`, reranker text = `content`; `true` → `embedding_ctx` + `tsv_ctx`, reranker text = `context_header + "\n\n" + content`. The generation prompt is identical in both variants.
- `filters.enabled`: when false, no filters are applied, extracted or estimated. When true, applied filters = explicit filters, plus (if `extract_from_question`) filters extracted from the question for any field not given explicitly. `strategy`: `auto` chooses `pre` when estimated selectivity (matching chunks / total chunks) < `selectivity_threshold`, else `post`; `pre`/`post` force a path. `pre` = exact distance sort over the filtered chunk set; `post` = HNSW over-fetch of `vector_k × overfetch_factor` candidates (with `hnsw.ef_search` = that number), then filter, then top `vector_k`. The lexical leg always applies filters in its `WHERE`.
- `rerank.enabled`: send the top `candidates` of the fused list to the reranker; the final list = those candidates re-ordered by rerank score, followed by fused ranks `candidates+1 … return_k` unchanged.
- `routing.enabled`: when true, an LLM classifies the question as `sql` or `vector`; `sql` runs guarded generated SQL against the structured tables; when false the route is always `vector`.
- `generation.enabled`: when true, the LLM answers from the top `context_k` chunks of the final list (or from SQL rows on the SQL route).

Response `200` (`Content-Type: application/json`; headers `X-Trace-Id: <trace_id>` always, `X-RAG-Degraded: <comma-separated codes>` when `degraded` is non-empty):

```json
{
  "request_id": "6f1c2a0e-3d1b-4c0a-9a55-2f8f0e7b9b11",
  "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
  "route": "vector",
  "route_source": "llm",
  "pipeline": {"…": "fully resolved pipeline, same shape as the request, defaults applied"},
  "filters": {
    "applied": {"categories": ["cs.IR"], "authors": ["jimmy lin"], "published_from": "2024-01-01", "published_to": "2024-12-31"},
    "extracted": {"authors": ["jimmy lin"], "published_from": "2024-01-01", "published_to": "2024-12-31"},
    "strategy": "pre",
    "estimated_selectivity": 0.031,
    "matching_chunks": 187,
    "underfilled": false
  },
  "chunks": [
    {
      "rank": 1,
      "chunk_id": 48213,
      "external_id": "2403.01234",
      "ordinal": 7,
      "title": "…",
      "section_path": "4 Experiments > 4.2 Rerankers",
      "published_on": "2024-03-02",
      "primary_category": "cs.IR",
      "content": "…",
      "context_header": "…",
      "scores": {"vector": 0.8123, "vector_rank": 2, "bm25": 0.4410, "bm25_rank": 1, "fused": 0.03252, "rerank": 0.9731}
    }
  ],
  "sql": null,
  "answer": {
    "status": "ok",
    "http_status": 200,
    "text": "… [1] … [3]",
    "abstained": false,
    "citations": [48213, 48190],
    "error": null
  },
  "degraded": [],
  "timings_ms": {
    "route": 640.2, "filter.estimate": 2.1, "embed": 38.0, "retrieve.vector": 11.4,
    "retrieve.bm25": 6.9, "fuse": 0.3, "rerank": 4210.7, "generate": 8800.1, "total": 13790.4
  },
  "usage": {"embed_prompt_tokens": 14, "rerank_pairs": 20, "llm_prompt_tokens": 2310, "llm_completion_tokens": 182, "llm_calls": 2}
}
```

Field rules:
- `route` ∈ {`vector`,`sql`}; `route_source` ∈ {`llm` (classifier decided), `disabled` (routing off), `fallback` (classifier or SQL path failed → vector)}.
- `filters`: `null` when `filters.enabled` is false. Author names in `applied`/`extracted` are normalized (contract C4). `strategy` ∈ {`none` (no filter applied or no vector leg), `pre`, `post`}; `estimated_selectivity` and `matching_chunks` are `null` when `strategy` = `none`; `underfilled` = true when the post-filter path returned fewer than `vector_k` dense candidates.
- `chunks`: final ranked list (length ≤ `return_k`), `[]` on the SQL route. Score fields are `null` when the stage did not run or the chunk did not appear in that leg. `vector` = cosine similarity (1 − cosine distance); `bm25` = `ts_rank_cd(tsv, query, 32)`; `fused` = RRF score; `rerank` = reranker score. `context_header` is always returned (even when `contextual` = false) for debugging.
- `sql`: `null` on the vector route unless the SQL path was attempted and fell back. Shape: `{"query": "select …", "columns": ["n"], "rows": [[3]], "row_count": 1, "truncated": false, "error": null}`; on fallback `rows` = `[]` and `error` = `"<guard_rejected|execution_failed|generation_failed>: <detail>"`.
- `answer.status` ∈ {`ok`, `disabled`, `unavailable`, `error`}; `answer.http_status` = 200 for `ok`, **503 for `unavailable` (LLM unreachable, breaker open or timed out)**, 500 for `error`, `null` for `disabled`. `text`/`citations` are `null` unless `ok`. `abstained` = true iff the answer starts with the exact sentence `I cannot answer this from the indexed papers.` `citations` = chunk ids referenced as `[n]` in the text.
- `degraded`: subset of {`rerank_unavailable`, `llm_unavailable`, `embedding_unavailable`, `router_unavailable`, `sql_fallback`}.
- `timings_ms`: wall-clock per stage for stages that were attempted (including failed attempts), keys from the fixed set {`route`, `sql.generate`, `sql.execute`, `filter.estimate`, `embed`, `retrieve.vector`, `retrieve.bm25`, `fuse`, `rerank`, `generate`, `total`}. `total` is always present.
- `usage`: token and call counts for cost estimation; zero when a stage did not run.

HTTP status semantics:
- `200` — request served, possibly degraded (rerank down → fused order + `rerank_unavailable`; LLM down → passages returned, `answer.http_status` 503 + `llm_unavailable`; embedding down with bm25 on → lexical only + `embedding_unavailable`; router down → vector route + `router_unavailable`).
- `400` — validation error, `application/problem+json`: `{"type": "about:blank", "title": "Bad Request", "status": 400, "detail": "…", "errors": [{"field": "pipeline.rerank.candidates", "message": "…"}], "trace_id": "…"}`.
- `503` — no retrieval possible (database unavailable; or embedding down with `bm25` = false), `application/problem+json` with `trace_id`.
- `500` — unexpected error, `application/problem+json` with `trace_id`.

#### `GET /v1/info`

```json
{
  "service": {"version": "0.1.0", "git_sha": "abc1234"},
  "models": {
    "embedding": {"name": "bge-m3", "digest": "<64-hex or null if unreachable>"},
    "llm": {"provider": "ollama", "name": "llama3.1:8b", "digest": "<64-hex or null>"},
    "reranker": {"url": "http://reranker:8080", "reachable": true, "model": "BAAI/bge-reranker-v2-m3", "revision": "<sha>"}
  },
  "index": {"<index_metadata key>": "<value>"},
  "embedding_digest_matches_index": true,
  "exact_vector_search": false,
  "defaults": {"…": "default pipeline object"}
}
```
`index` is read from `index_metadata` at request time (not cached at startup).

#### `GET /actuator/health`
Spring Boot health; `UP` when the reader datasource is reachable. Open circuit breakers do NOT make it `DOWN` (degradation is by design).
<!-- /CONTRACT:query-api -->

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

<!-- CONTRACT:dataset -->
Frozen dataset: `evals/dataset/questions.v1.jsonl`, one JSON object per line, 150 lines, ids `q0001`…`q0150`. Mix: 60 `factoid`, 37 `multi_chunk`, 30 `filter`, 15 `aggregate`, 8 `unanswerable`. CI subset (`ci_subset: true`): 50 = 20 / 12 / 10 / 5 / 3, stratified, seed 42.

```json
{
  "id": "q0042",
  "type": "filter",
  "question": "Which reranking model did Jimmy Lin's 2024 paper on listwise reranking fine-tune?",
  "reference_answer": "…",
  "gold_chunks": [{"external_id": "2403.01234", "ordinal": 7, "content_sha256": "<64-hex>"}],
  "expected_route": "vector",
  "expected_filters": {"authors": ["jimmy lin"], "published_from": "2024-01-01", "published_to": "2024-12-31"},
  "expected_sql_answer": null,
  "source": {"generator": "ollama:llama3.1:8b", "seed": 42, "sampled_chunks": [{"external_id": "2403.01234", "ordinal": 7}]},
  "verification": {"status": "verified", "by": "agent:<model id>", "human_reviewed": false, "notes": ""},
  "ci_subset": true
}
```
Rules: `gold_chunks` non-empty for `factoid`, `multi_chunk` (2–4 chunks), `filter`; empty for `aggregate` and `unanswerable`. `expected_route` = `sql` for `aggregate`, `vector` otherwise. `expected_sql_answer` (string; JSON-encoded list for list answers) only for `aggregate`. `expected_filters` only for `filter` (author names normalized per C4). Gold chunks are identified by `(external_id, ordinal)`; `content_sha256` detects drift after re-indexing. `evals/dataset/MANIFEST.json` records the file's sha256, counts per type, generation seed and model digests.
<!-- /CONTRACT:dataset -->

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

<!-- CONTRACT:ollama -->
Models: embeddings `bge-m3` (1024 dims, L2-normalized by Ollama), LLM `llama3.1:8b`. Base URL: `OLLAMA_HOST_URL` (host processes) or `OLLAMA_CONTAINER_URL` / `RAG_OLLAMA_URL` (containers).

Embeddings — `POST {base}/api/embed`
```json
{"model": "bge-m3", "input": ["text 1", "text 2"], "truncate": true, "keep_alive": "30m"}
```
→ `200 {"model": "bge-m3", "embeddings": [[1024 floats], [1024 floats]], "prompt_eval_count": 37}`.
Indexer batches ≤ 32 inputs per call; the service always sends exactly one input. No query/document prefix is used with bge-m3.

Chat — `POST {base}/api/chat`
```json
{
  "model": "llama3.1:8b",
  "messages": [{"role": "system", "content": "…"}, {"role": "user", "content": "…"}],
  "stream": false,
  "format": "json",
  "keep_alive": "30m",
  "options": {"temperature": 0, "seed": 42, "num_ctx": 8192, "num_predict": 384}
}
```
→ `200 {"message": {"role": "assistant", "content": "…"}, "prompt_eval_count": 812, "eval_count": 96, "done": true}`.
`format: "json"` is sent only for structured outputs (routing, SQL generation, judge, dataset generation); omitted for free-text answers and context summaries. `num_predict` per use: route 32, SQL generation 256, answer 384, context summary 96, judge 128, dataset generation 512. `temperature` is always 0 and `seed` is always `LLM_SEED` (42).

Model inventory — `GET {base}/api/tags` → `{"models": [{"name": "bge-m3:latest", "digest": "<64-hex>", …}, …]}`. A configured name without a tag matches `<name>:latest`.

Digest pinning — `infra/models.lock.json` (generated by `make models-lock`, committed):
```json
{
  "generated_at": "2026-10-10T00:00:00Z",
  "ollama": {
    "bge-m3": {"digest": "<64-hex from /api/tags>"},
    "llama3.1:8b": {"digest": "<64-hex from /api/tags>"}
  },
  "reranker": {"model": "BAAI/bge-reranker-v2-m3", "revision": "<40-hex Hugging Face commit sha>"}
}
```
Enforcement: the indexer and the eval runner FAIL (exit code 3) if a live digest differs from the lock, unless `--allow-model-drift` is passed (recorded in their output). The service never fails on drift; it logs an ERROR and reports `embedding_digest_matches_index` in `GET /v1/info`.

Anthropic provider (optional, all three Python/Kotlin LLM clients): used only when `LLM_PROVIDER=anthropic`; then `ANTHROPIC_API_KEY` and `ANTHROPIC_MODEL` are required (startup error otherwise). Embeddings always use Ollama `bge-m3`. Nothing ever requires a key.
<!-- /CONTRACT:ollama -->

<!-- CONTRACT:author-norm -->
`normalize_author_name(s)`:
1. Unicode NFKD, then drop combining marks (category `Mn`).
2. Lowercase.
3. Replace every character not in `[a-z0-9 -]` with a space.
4. Collapse runs of whitespace to one space; trim.

Test vectors (must pass in Python and Kotlin):

| input | output |
|---|---|
| `José  Pérez-Gómez` | `jose perez-gomez` |
| `J. R. R. Tolkien` | `j r r tolkien` |
| `Seán O'Neil` | `sean o neil` |
| `ZHANG Wei` | `zhang wei` |
| `  Jimmy   Lin ` | `jimmy lin` |
<!-- /CONTRACT:author-norm -->

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

- [Judge = generator model family (self-preference bias)] → disclosed in card and results; judge-agreement tooling ready for the 30-question human subset; Anthropic judge opt-in.
- [Full grid takes hours] → `--limit` smoke runs; full grid run in the background; generation-free `--no-generation` for quick retrieval iterations (such runs never write `results.md`).
- [Agent verification quality] → stated honestly; verdict log committed for audit.
- [Cassette misses on intentional retrieval changes] → gate exits 2 with re-record instructions; re-recording needs local Ollama (documented).
- [Small n per type (e.g. 8 unanswerable)] → n reported beside every metric; no claims of significance.
- [Breaker opens mid-run] → degraded rate column; rows with degraded > 0 flagged in results.md.

## Migration Plan

`make eval` writes rows; reruns overwrite rows per config. Dataset changes require a new version file (`questions.v2.jsonl`), never edits to v1.

## Open Questions

None.
