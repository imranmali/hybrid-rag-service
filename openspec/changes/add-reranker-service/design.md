# Design: add-reranker-service

## Context

See proposal.md — Why. The HTTP contract, environment variables, ports and the reranker revision pin are fixed by `add-platform-foundation/design.md` → "Cross-change contracts" (blocks copied verbatim below). The compose service is already defined by the foundation (`build: ../reranker`, port 8080 → host 8081, `HF_HOME=/models` on the `hf-cache` volume, health check `GET /healthz`, 600 s start period). The query service sends at most 20 passages per request (rerank candidates), each ≈ 384 content tokens + ≈ 80 header tokens.

## Goals / Non-Goals

**Goals:**
- Contract-exact API, predictable CPU latency, honest latency numbers.
- Tests and CI image builds that never download a real model.

**Non-Goals:**
- Throughput optimization beyond batching within one request (the service is single-tenant and the query path sends one request per query).
- ONNX/quantization (possible later; would change scores and require re-baselining).

## Decisions

### D1. Layout and dependencies
`reranker/pyproject.toml` (name `reranker`, Python 3.12, ruff `extend = "../ruff.toml"`), `reranker/uv.lock`, `reranker/src/reranker/{app.py, config.py, models.py (pydantic request/response), scorer.py (Scorer protocol, CrossEncoderScorer, FakeScorer), logging.py}`, `reranker/tests/`, `reranker/Dockerfile`, `reranker/README.md`.
Deps: `fastapi`, `uvicorn[standard]`, `pydantic>=2`, `pydantic-settings`, `sentence-transformers`, `torch` (CPU). Dev: `pytest`, `httpx`, `ruff`.
CPU wheels: `[tool.uv.sources] torch = [{ index = "pytorch-cpu", marker = "sys_platform == 'linux'" }]` with `[[tool.uv.index]] name = "pytorch-cpu", url = "https://download.pytorch.org/whl/cpu", explicit = true` — avoids pulling CUDA wheels on x86 Linux (CI) while macOS uses the default wheel.

### D2. Scoring
`CrossEncoder(model, revision=…, max_length=RERANKER_MAX_LENGTH, device="cpu")`; `predict(pairs, batch_size=RERANKER_BATCH_SIZE)` returning logits, then sigmoid applied explicitly in our code (not relying on the library's default activation, which changed across sentence-transformers versions). `torch.set_num_threads(RERANKER_TORCH_THREADS)` when > 0. `torch.inference_mode()`.
`FakeScorer`: lowercase alphanumeric token sets, score = Jaccard(query, passage) — deterministic, dependency-free; selected by `RERANKER_MODEL=fake`.

### D3. Concurrency and latency accounting
Inference is CPU-bound; parallel inferences thrash cores and inflate every request's latency. An `asyncio.Semaphore(RERANKER_MAX_CONCURRENCY)` (default 1) gates inference, which runs in a worker thread (`anyio.to_thread.run_sync`). `queue_ms` = time waiting on the semaphore; `inference_ms` = tokenize + forward + sigmoid; `latency_ms = queue_ms + inference_ms` (server-measured, excludes HTTP parsing). Uvicorn runs one worker (`--workers 1`); a second worker would load a second 2.2 GB model copy.

### D4. Startup and readiness
Model loads in a FastAPI lifespan background task so `/healthz` can answer 503 (`ready: false`) during the download/load; `/rerank` returns 503 `{"detail": "model not ready"}` until ready. Load failure → log the error and keep `ready: false` (the compose health check stays unhealthy; the query service degrades).

### D5. Validation
Pydantic models with `max_length` on `query` (2000), `passages` 1..`RERANKER_MAX_PASSAGES`, non-empty `id`/`text`, a model validator rejecting duplicate ids, `top_k` ≥ 1 (clamped to passage count). FastAPI returns its default 422 body.

### D6. Logging
JSON lines on stdout via a small `logging.Formatter` (no extra dependency): `{"ts", "level", "event": "rerank", "n_passages", "top_k", "queue_ms", "inference_ms", "latency_ms", "model"}`; startup logs `event: "model_loaded"` with load seconds. Access logs disabled (one line per request is enough).

### D7. Docker image
`python:3.12-slim` base; copy `uv` from `ghcr.io/astral-sh/uv:0.5` (pinned minor); `uv sync --frozen --no-dev`; non-root user `app`; `HF_HOME=/models`; `EXPOSE 8080`; `CMD ["uv", "run", "--no-sync", "uvicorn", "reranker.app:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1", "--no-access-log"]`; `curl` installed for the compose health check. The model is NOT baked into the image (keeps the image small and lets `RERANKER_MODEL` switch without rebuilds).

### D8. Latency expectation (to be measured, not promised)
bge-reranker-v2-m3 (568M params) on Docker Desktop CPU with 20 pairs of ~490 tokens is expected to take several seconds; the query service's rerank timeout is 10 000 ms and its candidate count is 20 for that reason. If measured p95 exceeds the timeout, the documented mitigation is `RERANKER_MODEL=BAAI/bge-reranker-base` (requires re-baselining evals). The measured numbers go into the README latency table via the eval runner, not from this change.

### Contracts used (copied verbatim from add-platform-foundation)

<!-- CONTRACT:reranker-api -->
Base URL: `http://reranker:8080` (containers) / `http://localhost:8081` (host).

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

`GET /healthz` → `200 {"status": "ok", "model": "BAAI/bge-reranker-v2-m3", "revision": "<sha or main>", "device": "cpu", "max_length": 512, "ready": true}` once loaded; `503` with `"ready": false` before.

`RERANKER_MODEL=fake` selects a deterministic, dependency-free scorer (token-overlap Jaccard) for tests and CI image builds.
<!-- /CONTRACT:reranker-api -->

<!-- CONTRACT:env -->
Compose project: `infra/docker-compose.yml`, invoked as `docker compose --env-file .env -f infra/docker-compose.yml …` (the Makefile does this). Compose project name `hybrid-rag`.

| Service | Image / build | Container port(s) | Host port | Depends on | Health check |
|---|---|---|---|---|---|
| `postgres` | `pgvector/pgvector:pg17` | 5432 | `${POSTGRES_HOST_PORT:-5433}` | — | `pg_isready -U ${POSTGRES_USER} -d ${POSTGRES_DB}` |
| `flyway` | `flyway/flyway:11` (one-shot, `migrate`) | — | — | postgres healthy | exit code 0 |
| `reranker` | build `../reranker` | 8080 | `${RERANKER_HOST_PORT:-8081}` | — | `GET /healthz` = 200, `start_period: 600s` (first model download) |
| `service` | build `../service` | 8080 | `${SERVICE_HOST_PORT:-8080}` | postgres healthy, flyway completed successfully | `GET /actuator/health` = 200, `start_period: 60s` |
| `otel-collector` | `otel/opentelemetry-collector-contrib:0.111.0` | 4317 (gRPC), 4318 (HTTP), 13133 (health) | 4317, 4318 | tempo | `GET :13133/` |
| `tempo` | `grafana/tempo:2.6.1` | 3200 (HTTP), 4317 (OTLP gRPC, internal) | 3200 | — | `GET :3200/ready` |
| `grafana` | `grafana/grafana:11.3.0` | 3000 | `${GRAFANA_HOST_PORT:-3000}` | tempo | `GET /api/health` |
| `wiremock` (replay overlay only) | `wiremock/wiremock:3.9.1` | 8080 | 8089 | — | `GET /__admin/health` |

`service` and `reranker` declare `extra_hosts: ["host.docker.internal:host-gateway"]` (needed on Linux/CI; harmless on Docker Desktop). Volumes: `pgdata` (postgres), `hf-cache` (reranker `HF_HOME=/models`), `tempo-data`.

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

# ---- Reranker container ----
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
- `reranker`: `RERANKER_MODEL`, `RERANKER_MODEL_REVISION`, `RERANKER_MAX_LENGTH`, `RERANKER_BATCH_SIZE`, `RERANKER_MAX_PASSAGES`, `RERANKER_TORCH_THREADS`, `RERANKER_MAX_CONCURRENCY=1`, `PORT=8080`, `HF_HOME=/models`.
- `service` (Spring relaxed binding: `RAG_DB_URL` → `rag.db.url`):
  `RAG_DB_URL=jdbc:postgresql://postgres:5432/${POSTGRES_DB}`,
  `RAG_DB_READER_USER=rag_reader`, `RAG_DB_READER_PASSWORD=${RAG_READER_PASSWORD}`,
  `RAG_DB_ROUTER_USER=rag_router`, `RAG_DB_ROUTER_PASSWORD=${RAG_ROUTER_PASSWORD}`,
  `RAG_OLLAMA_URL=${OLLAMA_CONTAINER_URL}`, `RAG_EMBED_MODEL=${OLLAMA_EMBED_MODEL}`, `RAG_LLM_MODEL=${OLLAMA_LLM_MODEL}`,
  `RAG_LLM_PROVIDER=${LLM_PROVIDER}`, `RAG_LLM_SEED=${LLM_SEED}`, `RAG_OLLAMA_KEEP_ALIVE=${OLLAMA_KEEP_ALIVE}`,
  `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL`,
  `RAG_RERANKER_URL=http://reranker:8080`,
  `RAG_RETRIEVAL_EXACT_VECTOR_SEARCH=${RAG_RETRIEVAL_EXACT_VECTOR_SEARCH}`,
  `GIT_SHA` (build arg, default `unknown`),
  `OTEL_SERVICE_NAME=rag-query-service`, `OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318`, `OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf`, `OTEL_TRACES_SAMPLER=always_on`, `OTEL_METRICS_EXPORTER=none`, `OTEL_LOGS_EXPORTER=none`.
- `otel-collector`: config `infra/otel/collector.yaml` — receivers `otlp` (grpc 4317, http 4318); processors `batch`; exporters `otlp/tempo` (`tempo:4317`, insecure) and `debug` (verbosity basic); pipeline `traces` only; extension `health_check` on 13133.
- `tempo`: config `infra/tempo/tempo.yaml` (single binary, local storage under `/var/tempo`, OTLP gRPC receiver).
- `grafana`: `GF_AUTH_ANONYMOUS_ENABLED=true`, `GF_AUTH_ANONYMOUS_ORG_ROLE=Admin`; provisioning `infra/grafana/provisioning/datasources/tempo.yaml` (Tempo at `http://tempo:3200`, default datasource).

Host processes: indexer reads `INDEXER_DATABASE_URL`, `OLLAMA_HOST_URL`, model vars, `LLM_*`, `ANTHROPIC_*`, `ARXIV_CONTACT`; evals reads `EVALS_DATABASE_URL`, `RAG_SERVICE_URL`, `OLLAMA_HOST_URL`, model vars, `LLM_*`, `ANTHROPIC_*`. Both load the repo-root `.env` (python-dotenv, path resolved relative to the package directory as `../.env`) and real environment variables override it.
<!-- /CONTRACT:env -->

## Risks / Trade-offs

- [CPU latency too high for the 10 s budget] → candidate count 20, max_length 512, fallback model; measured and published rather than hidden.
- [sentence-transformers API drift (activation, revision kwarg)] → explicit sigmoid; pin `sentence-transformers` major version in `pyproject.toml`; the opt-in real-model test catches breakage.
- [Image size from torch] → CPU wheels only (~800 MB image); acceptable for local use.
- [Model download on first start fails offline] → readiness stays false, service degrades with `rerank_unavailable`; README notes the first-start download.

## Migration Plan

Built and started by `make up`. Switching models: change `RERANKER_MODEL` in `.env`, `make up` (container restarts, model cached in volume).

## Open Questions

None.
