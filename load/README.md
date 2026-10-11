# Load test

k6 script `query.js` sends questions from `evals/dataset/questions.v1.jsonl` to `POST /v1/query` on the running service. The question order is deterministic (iteration counter + 42, modulo the dataset size).

## Run

```bash
make up && make index         # a running, indexed stack
make load                     # all three scenarios, one after another (about 7.5 minutes)
SCENARIO=retrieval make load  # one scenario: retrieval | rerank | full
```

`make load` runs `grafana/k6:0.54.0` in Docker against `http://host.docker.internal:$SERVICE_HOST_PORT`, with `evals/dataset` mounted read-only at `/dataset` and `load/` at `/load`.

## Scenarios and thresholds

| Scenario | Pipeline (eval config) | Rate | Duration | Threshold |
|---|---|---|---|---|
| `retrieval` | `04-contextual`, generation off | ramp 1 → 20 rps over 30 s, hold 20 rps for 60 s | 90 s | p95 < 500 ms |
| `rerank` | `05-rerank`, generation off | 0.5 rps | 120 s | p95 < 12 000 ms |
| `full` | `06-routing` | 0.05 rps | 200 s | p95 < 60 000 ms |

All scenarios also require `http_req_failed` below 1 %. The pipelines in `query.js` are copied from the eval configs; CI (`load-lint`) fails if they drift (`load/check_pipelines.py`). The rerank and full thresholds are this machine's budget (CPU reranker, local `llama3.1:8b`), not a portable SLO. Load tests do not run in CI.

Custom metrics: `rag_degraded` (share of responses with a non-empty `degraded` list; an HTTP 200 can still be degraded) and `rag_stage_<stage>` trends from the response's `timings_ms` (dots become underscores, e.g. `rag_stage_retrieve_vector`).

## Results

Each run writes `load/results/summary-<scenario|all>-<timestamp>.json` and prints a text summary. `load/results/` is git-ignored, so the files stay local; the numbers below are copied from them. Only the `retrieval` scenario was run (the `rerank` and `full` scenarios were not, for time).

Setup: Apple M4 dev machine shared with other jobs (host load average 13.6 at the time), service in Docker (reader pool 10), bge-m3 query embeddings and `llama3.1:8b` on host Ollama, 5 486 chunks, 150 questions from `questions.v1.jsonl`. A single unloaded query takes about 155 ms, of which the embedding call is about 125 ms.

| Run (summary file) | Target | Requests | p50 | p95 | HTTP failed | Degraded (of 200s) | Threshold p95 < 500 ms |
|---|---|---|---|---|---|---|---|
| `summary-retrieval-2026-10-10T22-19-35Z.json` | ramp to 20 rps, hold | 1 488 | 1 051 ms | 3 058 ms | 26.4 % | 96.9 % | failed |
| `summary-retrieval-2026-10-10T22-21-48Z.json` (`RETRIEVAL_PEAK_RPS=5`) | ramp to 5 rps, hold | 389 | 309 ms | 2 006 ms | 1.3 % | 44.5 % | failed |

Stage p95 from the same files (`rag_stage_*`): embed 1 063 ms at 20 rps and 1 995 ms at 5 rps; `retrieve.bm25` 1 456 ms and 786 ms; `retrieve.vector` 527 ms and 132 ms.

What the numbers say:

- The 20 rps target is not sustainable on this machine, and the thresholds correctly fail. The service did not collapse: 73.6 % of requests returned 200, and almost all of those were flagged `degraded`, mostly `embedding_unavailable` (lexical-only results) once the embedding call timed out or its breaker opened.
- The embedding call is the first bottleneck. Host Ollama serves one bge-m3 request at a time (about 125 ms each), so it tops out near 8 rps, and the 2 s embed timeout is hit at p95 even at 5 rps when other jobs share the machine.
- At 20 rps the database side fails too: 393 non-200 responses came from `retrieval_unavailable` (exhausted 10-connection reader pool, and `canceling statement due to statement timeout` on the OR-semantics lexical query).
- Treat these as this shared machine's numbers, not a clean benchmark: the other agents' Ollama and Postgres work overlapped the runs. The sustained-rate figure that would be meaningful (a rate the stack holds with `rag_degraded` near 0) needs a quiet machine and a lower `RETRIEVAL_PEAK_RPS` sweep.

`RETRIEVAL_PEAK_RPS` (default 20) lowers the retrieval scenario's peak rate; `make load` does not forward it, so run Docker directly:

```bash
docker run --rm -v "$PWD/load:/load" -v "$PWD/evals/dataset:/dataset:ro" \
  -e SCENARIO=retrieval -e RETRIEVAL_PEAK_RPS=5 -e K6_TARGET=http://host.docker.internal:8080 \
  grafana/k6:0.54.0 run /load/query.js
```
