# Spec Delta: load-testing

## Purpose

Measures the query service under concurrent load for the retrieval-only, rerank and full-pipeline paths, so latency claims are backed by a repeatable load run.

## ADDED Requirements

### Requirement: k6 scenarios
`load/query.js` SHALL define three scenarios selectable with `SCENARIO` (default: all, run one after another): `retrieval` (pipeline of grid row 4 with generation off, ramping to and holding 20 requests per second), `rerank` (grid row 5 with generation off, 0.5 requests per second) and `full` (grid row 6, 0.05 requests per second), each sending questions sampled with a fixed seed from `evals/dataset/questions.v1.jsonl` to `K6_TARGET` + `/v1/query`.

#### Scenario: Single scenario run
- **WHEN** `SCENARIO=retrieval` is set
- **THEN** only the retrieval scenario runs

#### Scenario: Pipelines match eval configs
- **WHEN** the request bodies of each scenario are inspected
- **THEN** their `pipeline` objects equal the corresponding grid config's pipeline except for the stated generation switch

### Requirement: Thresholds and metrics
The script SHALL fail the run when the HTTP failure rate exceeds 1 % or when p95 latency exceeds 500 ms (retrieval), 12 000 ms (rerank) or 60 000 ms (full), and SHALL record a `rag_degraded` rate metric from the response `degraded` field and per-stage trends from `timings_ms`.

#### Scenario: Degraded responses counted
- **WHEN** the reranker is stopped during the rerank scenario
- **THEN** the summary shows a non-zero `rag_degraded` rate while the HTTP failure rate stays near 0

### Requirement: Summaries written
Each run SHALL write a JSON summary to `load/results/summary-<scenario>-<timestamp>.json` and print a text summary.

#### Scenario: Summary file
- **WHEN** `make load` completes
- **THEN** a new summary file exists in `load/results/`
