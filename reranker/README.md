# Reranker

CPU-hosted FastAPI service for cross-encoder reranking in the Hybrid RAG query path. The canonical
HTTP contract is [C5](../openspec/changes/add-platform-foundation/design.md#c5-reranker-http-api).

## Run

From the repository root, copy the environment template once and start only the reranker:

```bash
cp .env.example .env
docker compose --env-file .env -f infra/docker-compose.yml up -d --wait reranker
curl -s http://localhost:8081/healthz
```

The first real-model start downloads `BAAI/bge-reranker-v2-m3` into the shared `hf-cache` volume and
may take several minutes. A ready response reports `device: "cpu"`.

```bash
curl -sS -X POST http://localhost:8081/rerank \
  -H 'content-type: application/json' \
  -d '{"query":"capital of France","passages":[{"id":"a","text":"Paris is the capital of France."},{"id":"b","text":"Bananas are yellow."}],"top_k":2}'
```

The first result is `a`. Each response also includes the server-measured `latency_ms` and matching
`X-Rerank-Latency-Ms` header.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `RERANKER_MODEL` | `BAAI/bge-reranker-v2-m3` | Hugging Face cross-encoder identifier; `fake` selects Jaccard scoring |
| `RERANKER_MODEL_REVISION` | `main` | Model revision passed to sentence-transformers |
| `RERANKER_MAX_LENGTH` | `512` | Maximum pair tokens |
| `RERANKER_BATCH_SIZE` | `16` | Pairs per model forward pass |
| `RERANKER_MAX_PASSAGES` | `100` | Request passage limit |
| `RERANKER_TORCH_THREADS` | `0` | Torch CPU threads (`0` leaves the runtime default) |
| `RERANKER_MAX_CONCURRENCY` | `1` | Concurrent inferences; requests queue beyond this limit |
| `PORT` | `8080` | Container listening port |

For deterministic tests or a quick image smoke check, run with `RERANKER_MODEL=fake`. To switch to
the faster fallback, set `RERANKER_MODEL=BAAI/bge-reranker-base` in `.env` and restart the reranker;
that model changes relevance scores and requires a new evaluation baseline.

## Latency measurement

The probe sends ten requests of twenty passages, each approximately 400 tokens:

```bash
cd reranker
uv run python scripts/latency_probe.py
```

Measurements were taken after the real model had loaded, through the compose container, on an
Apple M4 (10 cores, Docker Desktop with 10 CPUs and 8 GiB) with `RERANKER_TORCH_THREADS=0`
(runtime default), batch size 16 and max length 512. The host was shared and busy (load average
14–22 on 10 cores, with an ingestion job running), so treat the numbers as pessimistic and noisy.
Each fixture passage is about 880 words, so every pair fills the 512-token `max_length`.

| Run | Model | Requests × passages | p50 `latency_ms` | p95 `latency_ms` |
| --- | --- | ---: | ---: | ---: |
| 1 | `BAAI/bge-reranker-v2-m3` | 10 × 20 | 53 745 | 116 945 |
| 2 | `BAAI/bge-reranker-v2-m3` | 10 × 20 | 38 807 | 48 936 |
| 1 | `BAAI/bge-reranker-base` | 10 × 20 | 10 056 | 13 371 |

**Flag:** the default `BAAI/bge-reranker-v2-m3` misses the 10 000 ms p95 target on this CPU by a wide
margin (about 49–117 s for 20 full-length passages). `BAAI/bge-reranker-base` is about four times
faster but still has p95 above 10 000 ms under this load. Short passages are much cheaper: a
two-passage request with the default model took about 1.4 s. Reduce the passage count or
`RERANKER_MAX_LENGTH`, or give the container more CPU, before relying on the default model in the
query path.
