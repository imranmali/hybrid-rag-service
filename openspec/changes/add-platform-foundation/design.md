# Design: add-platform-foundation

## Context

Greenfield repo. See proposal.md — Why. Intent lives in `docs/BUILD_SPEC.md`; locked decisions live in `openspec/config.yaml` (arXiv full text, host Ollama with `bge-m3` 1024-d and `llama3.1:8b`, bge-reranker-v2-m3 FastAPI container on CPU, Kotlin/Spring Boot read-only service, Python 3.12/uv, Flyway, Postgres + pgvector). Five dependent changes are implemented by separate agents, so this document is the **single source of truth for every cross-change contract**. Other designs copy the relevant contract blocks verbatim (each block is delimited by `<!-- CONTRACT:<name> -->` … `<!-- /CONTRACT:<name> -->` so drift can be detected mechanically with a diff).

Machine: Apple Silicon, 24 GB RAM, Docker Desktop. Containers reach host Ollama at `http://host.docker.internal:11434`; host processes (indexer, evals) use `http://localhost:11434`.

## Goals / Non-Goals

**Goals:**
- One `make up` brings up a healthy stack; one `make index`, one `make query`, one `make eval`.
- A schema that supports every ablation-grid row without schema changes (contextual on/off, filters, routing).
- Least-privilege DB roles: the service can never write; generated SQL can only see structured tables.
- Contracts precise enough that parallel agents never need to talk to each other.

**Non-Goals:**
- Metrics backend, alerting, dashboards beyond the Tempo datasource.
- Any application logic (indexer, service, reranker, evals are separate changes).
- Production hardening (TLS, secrets management). Local dev passwords live in `.env`.

## Decisions

### D1. Contextual-chunking ablation = two embedding columns + two tsvector columns on ONE chunk set
`chunks.embedding` = bge-m3(content); `chunks.embedding_ctx` = bge-m3(context_header + "\n\n" + content); `tsv` / `tsv_ctx` likewise. The service picks the column pair from the request's `pipeline.contextual` flag.
- *Why:* chunk boundaries and ordinals are identical across both variants, so the ablation isolates the effect of the header alone, and eval gold ids `(external_id, ordinal)` are valid for both. This is literally BUILD_SPEC's "switching one column in or out of the embedded text and the tsvector".
- *Alternatives:* (a) two chunk sets (rows duplicated, `variant` column) — doubles rows, breaks gold-id stability, complicates every query with a variant predicate; (b) embed on the fly — impossible within the latency budget. Cost of the choice: a second HNSW index (~6k chunks × 4 KB ≈ 25 MB) — negligible.

### D2. arXiv adaptation of the BUILD_SPEC data model
`documents.author` → normalized `authors` + `paper_authors` (order preserved); `documents.category` → `primary_category` + `paper_categories` (all cross-lists); `jurisdiction` dropped; added `abstract`, `arxiv_version`, `updated_on`, `context_summary`, `content_hash`, `pipeline_fingerprint`. Added `chunks.section_path`, `content_sha256`. Added `index_metadata` key/value table so the service and evals can report which models/config built the index.

### D3. Text search config `english`, not `simple`
English-only corpus; stemming improves lexical recall for natural-language questions. Postgres `ts_rank_cd` is not true BM25; the repo keeps BUILD_SPEC's "BM25" name for the lexical leg and the README says so honestly.

### D4. Three roles, created in `V2__roles.sql` with Flyway placeholders
Flyway runs as the compose superuser `rag_owner`. Passwords come from Flyway placeholders (`FLYWAY_PLACEHOLDERS_*` env), never literals. `rag_router` gets column-level SELECT on `documents` (no `raw_text`, `context_summary`, hashes) and table SELECT on `authors`, `paper_authors`, `paper_categories` only; `default_transaction_read_only=on` and `statement_timeout=2s` are set on the role so the guard holds even if application code forgets.

### D5. Tracing = OTel Java agent → collector → Tempo → Grafana
Tempo + Grafana over Jaeger because BUILD_SPEC's compose already names Grafana and Tempo keeps one UI. No metrics pipeline (operational restraint).

### D6. CI eval gate runs in replay mode (contract below)
GitHub runners have no GPU and no Ollama; running llama3.1:8b on 4 vCPU for 50 questions × 6 configs is impractical, and swapping in a smaller model would make the gate measure a different system. Instead a WireMock container replays recorded Ollama + reranker responses, the DB is restored from a data-only snapshot, and the service runs exact (non-HNSW) vector search so results are bit-for-bit deterministic. The gate catches regressions in our code (SQL, fusion, filters, routing, guard); model drift is caught locally by the digest lock. Full rationale lives in `add-ci-docs-load/design.md`.

### D7. Compose profiles
`docker compose up` (default) starts everything. Foundation verification can start the infra subset by naming services (`postgres flyway otel-collector tempo grafana`) because `service/` and `reranker/` do not exist until their changes land. The replay overlay is a separate file, not a profile, so the base file stays readable.

### D8. Python packages are independent uv projects
`indexer/`, `reranker/`, `evals/` each have their own `pyproject.toml` + `uv.lock` (no root workspace) so each agent owns its whole directory. Shared lint rules live in root `ruff.toml`; packages use `extend = "../ruff.toml"`.

## Cross-change contracts

Everything in this section is normative. Other changes copy blocks verbatim.

### C1. Database schema (full DDL)

<!-- CONTRACT:ddl -->
`infra/migrations/V1__schema.sql`:

```sql
create extension if not exists vector;
create extension if not exists pg_trgm;

-- One row per arXiv paper (the version that was ingested).
create table documents (
  id                   uuid primary key,          -- uuid5(NAMESPACE_URL, 'arxiv:' || external_id), computed by the indexer
  external_id          text not null unique,      -- arXiv id WITHOUT version, e.g. '2403.01234'
  arxiv_version        int  not null,             -- ingested version number, e.g. 2
  title                text not null,
  abstract             text not null,
  primary_category     text not null,             -- e.g. 'cs.IR'
  published_on         date not null,             -- arXiv <published> (v1 submission date)
  updated_on           date not null,             -- arXiv <updated>
  source_url           text not null,             -- 'https://arxiv.org/html/' || external_id || 'v' || arxiv_version
  raw_text             text not null,             -- normalized full text, sections joined, references removed
  context_summary      text not null,             -- LLM doc-level situating sentence (<= 50 words)
  content_hash         text not null,             -- sha256 hex, see indexer design
  pipeline_fingerprint text not null,             -- sha256 hex of indexing config, see indexer design
  ingested_at          timestamptz not null default now()
);
create index documents_category_date on documents (primary_category, published_on desc);
create index documents_published_on  on documents (published_on);

create table authors (
  id              bigserial primary key,
  name            text not null,                  -- display form as first seen on arXiv
  name_normalized text not null unique            -- see contract C4 (author-name normalization)
);
create index authors_name_trgm on authors using gin (name_normalized gin_trgm_ops);

create table paper_authors (
  document_id uuid   not null references documents(id) on delete cascade,
  author_id   bigint not null references authors(id),
  position    int    not null,                    -- 1-based author order
  primary key (document_id, position),
  unique (document_id, author_id)
);
create index paper_authors_author on paper_authors (author_id);

create table paper_categories (
  document_id uuid    not null references documents(id) on delete cascade,
  category    text    not null,                   -- e.g. 'cs.IR', 'cs.LG'
  is_primary  boolean not null,
  primary key (document_id, category)
);
create index paper_categories_category on paper_categories (category);

create table chunks (
  id             bigserial primary key,
  document_id    uuid not null references documents(id) on delete cascade,
  ordinal        int  not null,                   -- 0-based within the document; stable for identical text + chunker config
  section_path   text not null,                   -- e.g. '3 Method > 3.2 Index construction'
  content        text not null,                   -- chunk body (no header)
  context_header text not null,                   -- 'Paper: <title> (<primary_category>, <year>). Section: <section_path>. <context_summary>'
  token_count    int  not null,                   -- bge-m3 tokenizer count of content
  content_sha256 text not null,                   -- sha256 hex of content (UTF-8)
  embedding      vector(1024) not null,           -- bge-m3(content)
  embedding_ctx  vector(1024) not null,           -- bge-m3(context_header || E'\n\n' || content)
  tsv            tsvector generated always as (to_tsvector('english', content)) stored,
  tsv_ctx        tsvector generated always as (to_tsvector('english', context_header || ' ' || content)) stored,
  unique (document_id, ordinal)
);
create index chunks_embedding_hnsw     on chunks using hnsw (embedding     vector_cosine_ops) with (m = 16, ef_construction = 64);
create index chunks_embedding_ctx_hnsw on chunks using hnsw (embedding_ctx vector_cosine_ops) with (m = 16, ef_construction = 64);
create index chunks_tsv_gin            on chunks using gin (tsv);
create index chunks_tsv_ctx_gin        on chunks using gin (tsv_ctx);
create index chunks_document           on chunks (document_id);

-- Written by the indexer at the end of every ingest; read by the service (/v1/info) and evals.
create table index_metadata (
  key        text primary key,
  value      text not null,
  updated_at timestamptz not null default now()
);
```

Required `index_metadata` keys (all values text): `embedding_model`, `embedding_model_digest`, `llm_model`, `llm_model_digest`, `chunk_size_tokens`, `chunk_overlap_tokens`, `min_chunk_tokens`, `header_prompt_version`, `parser_version`, `pipeline_fingerprint`, `document_count`, `chunk_count`, `last_ingest_at` (ISO-8601 UTC).

`infra/migrations/V2__roles.sql` (Flyway placeholders `indexer_password`, `reader_password`, `router_password`, supplied as `FLYWAY_PLACEHOLDERS_INDEXER_PASSWORD`, `FLYWAY_PLACEHOLDERS_READER_PASSWORD`, `FLYWAY_PLACEHOLDERS_ROUTER_PASSWORD`):

```sql
do $$
begin
  if not exists (select from pg_roles where rolname = 'rag_indexer') then
    create role rag_indexer login password '${indexer_password}';
  end if;
  if not exists (select from pg_roles where rolname = 'rag_reader') then
    create role rag_reader login password '${reader_password}';
  end if;
  if not exists (select from pg_roles where rolname = 'rag_router') then
    create role rag_router login password '${router_password}';
  end if;
  execute format('grant connect, temporary on database %I to rag_indexer', current_database());
  execute format('grant connect on database %I to rag_reader, rag_router', current_database());
end $$;

grant usage on schema public to rag_indexer, rag_reader, rag_router;

-- indexer: DML on everything, sequences, TEMP (used by index benchmarks), MAINTAIN (ANALYZE)
grant select, insert, update, delete
  on documents, authors, paper_authors, paper_categories, chunks, index_metadata to rag_indexer;
grant usage, select on all sequences in schema public to rag_indexer;
grant maintain
  on documents, authors, paper_authors, paper_categories, chunks, index_metadata to rag_indexer;  -- PG17: lets the indexer ANALYZE

-- service retrieval path: read-only on everything
grant select on documents, authors, paper_authors, paper_categories, chunks, index_metadata to rag_reader;
alter role rag_reader set default_transaction_read_only = on;
alter role rag_reader set statement_timeout = '5s';

-- service generated-SQL path: structured tables only, no chunk text/embeddings, no raw_text
grant select on authors, paper_authors, paper_categories to rag_router;
grant select (id, external_id, arxiv_version, title, abstract, primary_category, published_on, updated_on, source_url)
  on documents to rag_router;
alter role rag_router set default_transaction_read_only = on;
alter role rag_router set statement_timeout = '2s';
alter role rag_router set idle_in_transaction_session_timeout = '5s';
```

Rules: migrations are append-only (new `V3__…` files, never edits). The service connects only as `rag_reader` / `rag_router`; the indexer only as `rag_indexer`; evals (dataset generation, drift checks) only as `rag_reader`.
<!-- /CONTRACT:ddl -->

### C2. Containers, ports and environment variables

<!-- CONTRACT:env -->
Compose project: `infra/docker-compose.yml`, invoked as `docker compose --env-file .env -f infra/docker-compose.yml …` (the Makefile does this). Compose project name `hybrid-rag`.

| Service | Image / build | Container port(s) | Host port | Depends on | Health check |
|---|---|---|---|---|---|
| `postgres` | `pgvector/pgvector:pg17` | 5432 | `${POSTGRES_HOST_PORT:-5433}` | — | `pg_isready -U ${POSTGRES_USER} -d ${POSTGRES_DB}` |
| `flyway` | `flyway/flyway:11` (one-shot, `migrate`) | — | — | postgres healthy | exit code 0 |
| `reranker` (opt-in profile `reranker-container`; `RERANKER_DEVICE=cpu`) | build `../reranker` | 8080 | `${RERANKER_HOST_PORT:-8081}` | — | `GET /healthz` = 200, `start_period: 600s` (first model download) |
| `service` | build `../service` | 8080 | `${SERVICE_HOST_PORT:-8080}` | postgres healthy, flyway completed successfully | `GET /actuator/health` = 200, `start_period: 60s` |
| `otel-collector` | `otel/opentelemetry-collector-contrib:0.111.0` | 4317 (gRPC), 4318 (HTTP), 13133 (health) | 4317, 4318 | tempo | `GET :13133/` |
| `tempo` | `grafana/tempo:2.6.1` | 3200 (HTTP), 4317 (OTLP gRPC, internal) | 3200 | — | `GET :3200/ready` |
| `grafana` | `grafana/grafana:11.3.0` | 3000 | `${GRAFANA_HOST_PORT:-3000}` | tempo | `GET /api/health` |
| `wiremock` (replay overlay only) | `wiremock/wiremock:3.9.1` | 8080 | 8089 | — | `GET /__admin/health` |

`service` and `reranker` declare `extra_hosts: ["host.docker.internal:host-gateway"]` (needed on Linux/CI; harmless on Docker Desktop). Volumes: `pgdata` (postgres), `hf-cache` (reranker `HF_HOME=/models`), `tempo-data`.

**Reranker placement.** The default reranker is a host process, `make reranker` (`uv run uvicorn reranker.app:app --port ${RERANKER_HOST_PORT}`, `RERANKER_DEVICE=auto` → `mps` on Apple Silicon, else `cuda`, else `cpu`). Measured on the M4 dev machine with 20 passages of ~384 tokens: MPS p50 6.0 s / p95 6.7 s, the CPU container p50 39–54 s, so the container cannot meet the 10 s rerank timeout (Docker Desktop has no GPU access). The container stays as the opt-in `reranker-container` profile (`make up-reranker-container`) for Linux/CUDA-less hosts, publishing the same host port. Either way the service and WireMock reach it at `http://host.docker.internal:${RERANKER_HOST_PORT}`; `make up` does not start a reranker.

`.env.example` (copied to `.env`; `.env` is git-ignored):

```dotenv
# ---- Postgres ----
POSTGRES_DB=rag
POSTGRES_USER=rag_owner
POSTGRES_PASSWORD=rag_owner_dev
POSTGRES_HOST_PORT=5433
RAG_INDEXER_PASSWORD=rag_indexer_dev
RAG_READER_PASSWORD=rag_reader_dev
RAG_ROUTER_PASSWORD=rag_router_dev

# ---- Host-side DSNs (indexer and evals run on the host via uv) ----
INDEXER_DATABASE_URL=postgresql://rag_indexer:rag_indexer_dev@localhost:5433/rag
EVALS_DATABASE_URL=postgresql://rag_reader:rag_reader_dev@localhost:5433/rag

# ---- Models (host Ollama) ----
OLLAMA_HOST_URL=http://localhost:11434
OLLAMA_CONTAINER_URL=http://host.docker.internal:11434
OLLAMA_EMBED_MODEL=bge-m3
OLLAMA_LLM_MODEL=llama3.1:8b
OLLAMA_KEEP_ALIVE=30m
LLM_SEED=42
# ollama | anthropic  (anthropic is opt-in and requires ANTHROPIC_API_KEY and ANTHROPIC_MODEL)
LLM_PROVIDER=ollama
ANTHROPIC_API_KEY=
ANTHROPIC_MODEL=

# ---- Reranker ----
# Default: run natively on the host with `make reranker` (MPS on Apple Silicon).
# Opt-in CPU container: `make up-reranker-container` (same host port).
RERANKER_DEVICE=auto
RERANKER_CONTAINER_URL=http://host.docker.internal:8081
RERANKER_MODEL=BAAI/bge-reranker-v2-m3
RERANKER_MODEL_REVISION=main
RERANKER_MAX_LENGTH=512
RERANKER_BATCH_SIZE=16
RERANKER_MAX_PASSAGES=100
RERANKER_TORCH_THREADS=0
RERANKER_HOST_PORT=8081

# ---- Query service ----
SERVICE_HOST_PORT=8080
RAG_SERVICE_URL=http://localhost:8080
RAG_RETRIEVAL_EXACT_VECTOR_SEARCH=false

# ---- Observability ----
GRAFANA_HOST_PORT=3000

# ---- arXiv politeness (optional contact for the User-Agent) ----
ARXIV_CONTACT=
```

Environment passed to each container by compose:

- `postgres`: `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`.
- `flyway`: `FLYWAY_URL=jdbc:postgresql://postgres:5432/${POSTGRES_DB}`, `FLYWAY_USER=${POSTGRES_USER}`, `FLYWAY_PASSWORD=${POSTGRES_PASSWORD}`, `FLYWAY_LOCATIONS=filesystem:/flyway/sql` (mount `./migrations:/flyway/sql:ro`), `FLYWAY_PLACEHOLDERS_INDEXER_PASSWORD=${RAG_INDEXER_PASSWORD}`, `FLYWAY_PLACEHOLDERS_READER_PASSWORD=${RAG_READER_PASSWORD}`, `FLYWAY_PLACEHOLDERS_ROUTER_PASSWORD=${RAG_ROUTER_PASSWORD}`, `FLYWAY_CONNECT_RETRIES=10`.
- `reranker` (container profile only): `RERANKER_DEVICE=cpu`, `RERANKER_MODEL`, `RERANKER_MODEL_REVISION`, `RERANKER_MAX_LENGTH`, `RERANKER_BATCH_SIZE`, `RERANKER_MAX_PASSAGES`, `RERANKER_TORCH_THREADS`, `RERANKER_MAX_CONCURRENCY=1`, `PORT=8080`, `HF_HOME=/models`.
- `service` (Spring relaxed binding: `RAG_DB_URL` → `rag.db.url`):
  `RAG_DB_URL=jdbc:postgresql://postgres:5432/${POSTGRES_DB}`,
  `RAG_DB_READER_USER=rag_reader`, `RAG_DB_READER_PASSWORD=${RAG_READER_PASSWORD}`,
  `RAG_DB_ROUTER_USER=rag_router`, `RAG_DB_ROUTER_PASSWORD=${RAG_ROUTER_PASSWORD}`,
  `RAG_OLLAMA_URL=${OLLAMA_CONTAINER_URL}`, `RAG_EMBED_MODEL=${OLLAMA_EMBED_MODEL}`, `RAG_LLM_MODEL=${OLLAMA_LLM_MODEL}`,
  `RAG_LLM_PROVIDER=${LLM_PROVIDER}`, `RAG_LLM_SEED=${LLM_SEED}`, `RAG_OLLAMA_KEEP_ALIVE=${OLLAMA_KEEP_ALIVE}`,
  `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL`,
  `RAG_RERANKER_URL=${RERANKER_CONTAINER_URL:-http://host.docker.internal:8081}`,
  `RAG_RETRIEVAL_EXACT_VECTOR_SEARCH=${RAG_RETRIEVAL_EXACT_VECTOR_SEARCH}`,
  `GIT_SHA` (build arg, default `unknown`),
  `OTEL_SERVICE_NAME=rag-query-service`, `OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318`, `OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf`, `OTEL_TRACES_SAMPLER=always_on`, `OTEL_METRICS_EXPORTER=none`, `OTEL_LOGS_EXPORTER=none`.
- `otel-collector`: config `infra/otel/collector.yaml` — receivers `otlp` (grpc 4317, http 4318); processors `batch`; exporters `otlp/tempo` (`tempo:4317`, insecure) and `debug` (verbosity basic); pipeline `traces` only; extension `health_check` on 13133.
- `tempo`: config `infra/tempo/tempo.yaml` (single binary, local storage under `/var/tempo`, OTLP gRPC receiver).
- `grafana`: `GF_AUTH_ANONYMOUS_ENABLED=true`, `GF_AUTH_ANONYMOUS_ORG_ROLE=Admin`; provisioning `infra/grafana/provisioning/datasources/tempo.yaml` (Tempo at `http://tempo:3200`, default datasource).

Host processes: the reranker (`make reranker`) reads every `RERANKER_*` variable from `.env`; indexer reads `INDEXER_DATABASE_URL`, `OLLAMA_HOST_URL`, model vars, `LLM_*`, `ANTHROPIC_*`, `ARXIV_CONTACT`; evals reads `EVALS_DATABASE_URL`, `RAG_SERVICE_URL`, `OLLAMA_HOST_URL`, model vars, `LLM_*`, `ANTHROPIC_*`. Both load the repo-root `.env` (python-dotenv, path resolved relative to the package directory as `../.env`) and real environment variables override it.
<!-- /CONTRACT:env -->

### C3. Ollama endpoints, model names and digest pinning

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

### C4. Author-name normalization (indexer writes, service and evals match)

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

### C5. Reranker HTTP API

<!-- CONTRACT:reranker-api -->
Base URL: `http://host.docker.internal:8081` (from containers) / `http://localhost:8081` (host). The server runs on the host by default (`make reranker`) or as the opt-in container; see C2.

`POST /rerank` — request:
```json
{
  "query": "which rerankers were evaluated on BEIR?",
  "passages": [{"id": "48213", "text": "…"}, {"id": "48190", "text": "…"}],
  "top_k": 20
}
```
- `query`: non-empty string, ≤ 2000 chars. `passages`: 1..`RERANKER_MAX_PASSAGES` (100) items; `id` non-empty string, unique within the request; `text` non-empty string. `top_k`: optional int ≥ 1; default = number of passages; values larger than the passage count are clamped.
- Response `200`:
```json
{
  "model": "BAAI/bge-reranker-v2-m3",
  "results": [{"id": "48190", "index": 1, "score": 0.9731}, {"id": "48213", "index": 0, "score": 0.4410}],
  "latency_ms": 2310.4
}
```
  `results` sorted by `score` descending (ties: lower `index` first), truncated to `top_k`; `index` is the 0-based position in the request; `score` = sigmoid(cross-encoder logit) ∈ [0, 1]; `latency_ms` = queue wait + inference, server-measured. Header `X-Rerank-Latency-Ms` carries the same value.
- Errors: `422` for validation failures (FastAPI default body); `503 {"detail": "model not ready"}` while the model loads; `500` on inference failure.
- Each (query, passage) pair is truncated to `RERANKER_MAX_LENGTH` (512) tokens.

`GET /healthz` → `200 {"status": "ok", "model": "BAAI/bge-reranker-v2-m3", "revision": "<sha or main>", "device": "<resolved device: mps | cuda | cpu>", "max_length": 512, "ready": true}` once loaded; `503` with `"ready": false` before.

`RERANKER_MODEL=fake` selects a deterministic, dependency-free scorer (token-overlap Jaccard) for tests and CI image builds.
<!-- /CONTRACT:reranker-api -->

### C6. Query service HTTP API

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

### C7. Eval config YAML schema and the ablation grid

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

### C8. Eval dataset record schema

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

### C9. CI replay mode and DB snapshot

<!-- CONTRACT:replay -->
Purpose: run the 50-question eval gate on GitHub runners (no Ollama, no GPU) deterministically.

- **Snapshot.** `make snapshot` writes `data/snapshots/rag-index-<first 12 hex of pipeline_fingerprint>.dump` = `pg_dump --data-only --format=custom --exclude-table=flyway_schema_history` of the `rag` DB (run inside the `postgres` container as `rag_owner`) and prints its sha256. `make snapshot-restore SNAPSHOT=<path>` (as `rag_owner`, into a DB that Flyway has already migrated) first runs `truncate documents, authors, paper_authors, paper_categories, chunks, index_metadata restart identity cascade`, then `pg_restore --data-only --disable-triggers --single-transaction`, then `ANALYZE` — so it works on a fresh CI database and on a populated local one alike. `data/snapshots/` is git-ignored; CI downloads the file from a GitHub Release described by `evals/ci/snapshot.lock.json`:
  ```json
  {"release_tag": "index-snapshot-<12hex>", "asset_name": "rag-index-<12hex>.dump", "sha256": "<64-hex>", "pipeline_fingerprint": "<64-hex>", "document_count": 212, "chunk_count": 5480}
  ```
- **Exact search.** With `RAG_RETRIEVAL_EXACT_VECTOR_SEARCH=true` the service disables index scans for vector queries (`SET LOCAL enable_indexscan = off`, `SET LOCAL enable_bitmapscan = off` in the vector-query transaction), so dense retrieval is an exact, deterministic sort (ties by `chunk_id`). HNSW graphs are rebuilt non-deterministically on restore, so ANN would not be replayable.
- **Overlay.** `infra/docker-compose.replay.yml` adds `wiremock` and overrides `service` env: `RAG_OLLAMA_URL=http://wiremock:8080`, `RAG_RERANKER_URL=http://wiremock:8080`, `RAG_RETRIEVAL_EXACT_VECTOR_SEARCH=true`, `OTEL_TRACES_EXPORTER=none`.
  - `WIREMOCK_MODE=record` (default for `make eval-record-ci`): wiremock loads proxy stubs from `infra/wiremock/proxy-mappings/` (`/api/.*` → `http://host.docker.internal:11434`, `/rerank` and `/healthz` → `http://host.docker.internal:8081`) and journals traffic; a reranker must be running on the host port (`make eval-record-ci` checks `/healthz` first).
  - `WIREMOCK_MODE=replay` (`make eval-ci`): wiremock loads only `evals/cassettes/ci/mappings/` (mounted read-only); unmatched requests return 404, which the service treats as a dependency failure (→ a `degraded` code), which the gate counts as a failure.
- **Recording.** `make eval-record-ci` = up with overlay in record mode → `uv run evals grid --subset ci --no-generation --no-judge` → `uv run evals cassettes snapshot` (calls `POST /__admin/recordings/snapshot` with `requestBodyPattern` `equalToJson`, `ignoreArrayOrder: false`, `ignoreExtraElements: false`, dedup identical requests, `persist: false`, writes stub files to `evals/cassettes/ci/mappings/` and `evals/cassettes/ci/meta.json` with recording time, git sha, model digests from `/api/tags`, reranker model/revision, snapshot sha256).
- **Gate.** `make eval-ci` = restore snapshot → up with overlay in replay mode → `uv run evals grid --subset ci --no-generation --no-judge --replay --gate thresholds/ci.yaml`. `--replay` makes the runner take model digests from `evals/cassettes/ci/meta.json` instead of live Ollama. Exit code 0 = pass, 1 = threshold breach, 2 = any degraded response (cassette miss), 3 = setup error.
<!-- /CONTRACT:replay -->

### C10. Makefile command contract

<!-- CONTRACT:make -->
Root `Makefile` (owned by add-platform-foundation). `COMPOSE = docker compose --env-file .env -f infra/docker-compose.yml`. Targets and the package commands they call (each package change MUST provide the command exactly as written):

| Target | Runs | Command provided by |
|---|---|---|
| `make doctor` | `infra/scripts/doctor.sh` — checks docker daemon, `.env` present, Ollama reachable at `OLLAMA_HOST_URL`, both models pulled, `uv`, JDK ≥ 21 | foundation |
| `make models` | `ollama pull bge-m3 && ollama pull llama3.1:8b` | foundation |
| `make models-lock` | `python3 infra/scripts/models_lock.py > infra/models.lock.json` | foundation |
| `make up` | `$(COMPOSE) up -d --build --wait` (no reranker; see next rows) | foundation |
| `make reranker` | `cd reranker && (load ../.env) RERANKER_DEVICE=$(RERANKER_DEVICE) uv run uvicorn reranker.app:app --host 0.0.0.0 --port $(RERANKER_HOST_PORT) --workers 1 --no-access-log` (foreground) | foundation (command), add-reranker-service (app) |
| `make up-reranker-container` | `$(COMPOSE) --profile reranker-container up -d --build --wait reranker` | foundation |
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

### C11. Chosen numbers

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
| Reranker | max_length 512, batch 16, max passages 100, max concurrency 1; device `auto` (host, MPS) by default, `cpu` in the container | reranker |
| arXiv politeness | ≥ 3 s between requests; retries on 429/5xx at 5 s, 15 s, 45 s | indexer |
| Corpus | manifest of 220 papers (cs.IR, cs.CL, cs.DB, 2023–2026); `make index` ingests the first 200 | indexer |
| Dataset | 150 = 60 factoid / 37 multi_chunk / 30 filter / 15 aggregate / 8 unanswerable; CI subset 50 = 20/12/10/5/3 | evals |
| CI regression tolerance | baseline − 0.03 for nDCG@10 and recall@20; − 0.05 route accuracy; − 0.10 SQL answer accuracy; 0 degraded responses | evals, CI |
<!-- /CONTRACT:numbers -->

## Risks / Trade-offs

- [Flyway placeholder env-var casing differs between versions] → task 2.3 verifies by logging in as each role with `psql`; if `FLYWAY_PLACEHOLDERS_*` is not picked up, switch to `-placeholders.<name>=` command args in compose (contract unchanged: same placeholder names).
- [Port 5433 / 8080 / 3000 collide with local services] → every host port is overridable in `.env`; container-side ports are fixed.
- [Reranker first start downloads ~2.2 GB] → `hf-cache` volume, 600 s health start period; `make up` documents the wait.
- [Two HNSW indexes slow ingestion] → corpus is ~6k chunks; incremental insert cost is seconds. HNSW build time is measured separately by the indexer benchmark.
- [Contract change after parallel work starts] → contract blocks are marker-delimited; any change must be re-copied into every dependent design (check with `diff` of the blocks) before the dependent agents continue.
- [Docker Desktop memory] → postgres ~0.5 GB, reranker ~2.5 GB, service ~0.5 GB, observability ~0.5 GB; host Ollama ~6.5 GB with both models resident. Fits in 24 GB; document a ≥ 8 GB Docker Desktop memory setting in `make doctor` output.

## Migration Plan

Greenfield: `cp .env.example .env && make models && make models-lock && make up`. Rollback = `make clean` (drops volumes). Future schema changes are new Flyway versions; the indexer re-ingests when `pipeline_fingerprint` changes.

## Open Questions

None that change specs or tasks. Image tags above are the planned pins; an implementer may bump a patch version if a tag is unavailable, recording it in the compose file comment.
