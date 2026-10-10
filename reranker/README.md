# Reranker

FastAPI service for cross-encoder reranking in the Hybrid RAG query path. The canonical
HTTP contract is [C5](../openspec/changes/add-platform-foundation/design.md#c5-reranker-http-api).

## Run

The default is a native host process, which uses the Apple GPU (MPS) when available. Docker Desktop
cannot reach the GPU, so the container is CPU-only and is an opt-in fallback. From the repository
root, copy the environment template once (`cp .env.example .env`), then:

```bash
make reranker                      # foreground; RERANKER_DEVICE=auto -> mps, then cuda, then cpu
curl -s http://localhost:8081/healthz
```

The first real-model start downloads `BAAI/bge-reranker-v2-m3` into `~/.cache/huggingface/hub`.
`/healthz` reports the resolved `device` (`mps`, `cuda` or `cpu`).

Opt-in CPU container (same host port; stop the host process first):

```bash
docker compose --env-file .env -f infra/docker-compose.yml up -d --wait reranker
```

Example request:

```bash
curl -sS -X POST http://localhost:8081/rerank \
  -H 'content-type: application/json' \
  -d '{"query":"capital of France","passages":[{"id":"a","text":"Paris is the capital of France."},{"id":"b","text":"Bananas are yellow."}],"top_k":2}'
```

The first result is `a`. Each response also includes the server-measured `latency_ms` and matching
`X-Rerank-Latency-Ms` header. The canonical HTTP contract is
[C5](../openspec/changes/add-platform-foundation/design.md#c5-reranker-http-api).

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `RERANKER_MODEL` | `BAAI/bge-reranker-v2-m3` | Hugging Face cross-encoder identifier; `fake` selects Jaccard scoring |
| `RERANKER_MODEL_REVISION` | `main` | Model revision passed to sentence-transformers |
| `RERANKER_MAX_LENGTH` | `512` | Maximum pair tokens |
| `RERANKER_BATCH_SIZE` | `16` | Pairs per model forward pass |
| `RERANKER_MAX_PASSAGES` | `100` | Request passage limit |
| `RERANKER_DEVICE` | `auto` | `auto` (mps, then cuda, then cpu), `mps`, `cuda` or `cpu` |
| `RERANKER_TORCH_THREADS` | `0` | Torch CPU threads (`0` leaves the runtime default) |
| `RERANKER_MAX_CONCURRENCY` | `1` | Concurrent inferences; requests queue beyond this limit |
| `PORT` | `8080` | Container listening port |

For deterministic tests or a quick image smoke check, run with `RERANKER_MODEL=fake`. To switch to
the faster fallback, set `RERANKER_MODEL=BAAI/bge-reranker-base` in `.env` and restart the reranker;
that model changes relevance scores and requires a new evaluation baseline.

## Latency measurement

The probe sends ten requests of twenty passages and prints the p50/p95 of the server-measured
`latency_ms`. `--words` sets the passage size; the default of 225 words is about 380 tokens
(measured with the model tokenizer on the fixture text), the real chunk size. `--words 880` gives
passages that exceed the 512-token `max_length`, so every pair is truncated: the worst case.

```bash
cd reranker
uv run python scripts/latency_probe.py             # ~380-token passages
uv run python scripts/latency_probe.py --words 880 # worst case, truncated to 512
```

Measured after the model had loaded, on an Apple M4 (10 cores) with batch size 16 and max length 512.
The host was shared and busy throughout (load average 14–22 on 10 cores, with an ingestion job and
the Docker VM running), so the numbers are pessimistic and noisy.

| Placement | Model | Passages | Run | p50 `latency_ms` | p95 `latency_ms` |
| --- | --- | --- | ---: | ---: | ---: |
| **Host, MPS (default)** | `BAAI/bge-reranker-v2-m3` | ~380 tokens | 1 | 8 075 | 11 235 |
| Host, MPS | `BAAI/bge-reranker-v2-m3` | ~380 tokens | 2 | 9 471 | 14 361 |
| Host, MPS | `BAAI/bge-reranker-v2-m3` | ~380 tokens | 3 | 9 861 | 15 749 |
| Host, MPS | `BAAI/bge-reranker-v2-m3` | worst case (512) | 1 | 11 224 | 15 084 |
| CPU container (opt-in) | `BAAI/bge-reranker-v2-m3` | worst case (512) | 1 | 53 745 | 116 945 |
| CPU container | `BAAI/bge-reranker-v2-m3` | worst case (512) | 2 | 38 807 | 48 936 |
| CPU container | `BAAI/bge-reranker-base` | worst case (512) | 1 | 10 056 | 13 371 |

**p95 ≤ 10 s is not met in these runs.** On the host with MPS the default model is far faster than the
CPU container (about 8–10 s p50 versus 39–54 s), but under this machine's load the p95 for 20
~380-token passages was 11–16 s. A separate MPS measurement for the platform design (20 passages of
about 300 words, batch 16, max length 512, same loaded host) gave p50 6.0 s and p95 6.7 s, so results
vary a lot between runs with contention for the GPU and CPU. The CPU container measured 39–54 s p50
for 20 truncated passages, so it cannot meet the target at that size; use it only where no GPU is
available, and reduce the rerank candidate count or `RERANKER_MAX_LENGTH` there.

Note: on MPS the first request after the model loads or after an idle period was observed to take
14–28 s even for two short passages, while the following requests took 26–200 ms. Send a warm-up
request before measuring or serving latency-sensitive traffic.
