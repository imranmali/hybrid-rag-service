# Tasks: add-reranker-service

Scope: this change owns `reranker/**` only. The compose service definition in `infra/docker-compose.yml` already exists (add-platform-foundation); do not edit it. Do not run `git commit`.

## 1. Project setup

- [ ] 1.1 Create `reranker/pyproject.toml` per design D1 (CPU torch index for Linux, ruff extends `../ruff.toml`, pytest marker `model`) and the `reranker/src/reranker/` package; run `cd reranker && uv sync`; verify `uv run python -c "import fastapi, sentence_transformers, torch; print(torch.__version__)"` succeeds.
- [ ] 1.2 Implement `config.py` (pydantic-settings for every `RERANKER_*` variable in contract C2 plus `PORT`, with the contract defaults); test that defaults match the contract values; verify `uv run pytest tests/test_config.py`.

## 2. API and scoring (rerank-api spec)

- [ ] 2.1 Implement `models.py` (request/response pydantic models with the limits of design D5, duplicate-id validator, top_k clamping helper); tests for empty query, 2001-char query, 0 and 101 passages, duplicate ids, empty text → validation errors; verify `uv run pytest tests/test_models.py`.
- [ ] 2.2 Implement `scorer.py` (`Scorer` protocol, `FakeScorer` Jaccard, `CrossEncoderScorer` with explicit sigmoid per design D2); tests: fake scores deterministic and in [0,1], ordering by overlap; verify `uv run pytest tests/test_scorer.py`.
- [ ] 2.3 Implement `app.py` (lifespan background model load, `/healthz` 503→200, `/rerank` 503 until ready, semaphore-gated thread inference, sorting with index tie-break, top_k truncation, `X-Rerank-Latency-Ms` header) and `logging.py` (JSON lines per design D6); tests with `RERANKER_MODEL=fake` via `fastapi.testclient`: ordering and truncation, top_k clamp, header equals `latency_ms`, 422 cases return no inference (scorer call counter = 0), 503 before ready (scorer whose load blocks on an event), health body fields, two concurrent requests with a slow fake scorer have non-overlapping inference intervals and the second has `queue_ms > 0`, and a captured log line contains `event`, `n_passages`, `queue_ms`, `inference_ms`, `model`; verify `uv run pytest tests/test_app.py`.
- [ ] 2.4 Add `tests/test_real_model.py` (marker `model`, skipped by default) loading `cross-encoder/ms-marco-TinyBERT-L-2-v2` through `CrossEncoderScorer` and asserting the Paris passage outranks the banana passage with scores in [0,1]; verify `uv run pytest -m model` passes locally.

## 3. Container

- [ ] 3.1 Write `reranker/Dockerfile` per design D7 and `reranker/.dockerignore` (`.venv`, `tests`, `__pycache__`); verify `docker build -t hybrid-rag-reranker reranker/` succeeds and `docker run --rm -e RERANKER_MODEL=fake -p 18081:8080 hybrid-rag-reranker` serves `curl -s localhost:18081/healthz` with `ready: true` within 10 s.
- [ ] 3.2 Start the real service via compose (`docker compose --env-file .env -f infra/docker-compose.yml up -d --wait reranker`; first start downloads the model); verify `curl -s localhost:8081/healthz` reports `BAAI/bge-reranker-v2-m3`, `device: cpu`, `ready: true`.
- [ ] 3.3 Measure real latency: send 10 requests of 20 passages (~400 tokens each; use a fixture text file in `reranker/tests/fixtures/passages.txt`) with a small script `reranker/scripts/latency_probe.py`; verify it prints p50/p95 `latency_ms`, and record the numbers (machine, model, threads) in `reranker/README.md`. If p95 > 10 000 ms, also record numbers for `RERANKER_MODEL=BAAI/bge-reranker-base` and flag it in the README.

## 4. Documentation and verification

- [ ] 4.1 Write `reranker/README.md` (API summary pointing to the contract, env vars, fake model, measured latency table, model-switch instructions); verify the curl examples in it run as written against the compose container.
- [ ] 4.2 Final verification from `reranker/`: `uv run ruff check . && uv run ruff format --check . && uv run pytest` pass (model-marked test skipped by default); from the repo root `make lint` and `make test` include the reranker and pass; `curl -s -X POST localhost:8081/rerank -H 'content-type: application/json' -d '{"query":"capital of France","passages":[{"id":"a","text":"Paris is the capital of France."},{"id":"b","text":"Bananas are yellow."}],"top_k":2}'` returns `a` first.
