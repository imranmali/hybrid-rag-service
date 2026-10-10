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

Each run writes `load/results/summary-<scenario|all>-<timestamp>.json` and prints a text summary. `load/results/` is git-ignored, so the files stay local; the numbers below are copied from them.

Not measured yet.
