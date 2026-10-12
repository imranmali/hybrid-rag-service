# Spec Delta: eval-runner

## Purpose

Runs named pipeline configurations against the live query service and records quality, routing, latency and cost metrics reproducibly, one results row per configuration.

## ADDED Requirements

### Requirement: Config files drive the service
The runner SHALL load configs matching the foundation eval-config schema (contract C7), reject a config whose `pipeline` block is missing any key or contains unknown keys, and send its `pipeline` verbatim on every `/v1/query` request.

#### Scenario: Incomplete config rejected
- **WHEN** a config omits `pipeline.rerank.candidates`
- **THEN** the runner exits with code 3 before sending any query, naming the missing key

#### Scenario: Grid configs
- **WHEN** `evals/configs/` is listed
- **THEN** it contains `01-vector`, `02-hybrid`, `03-filters`, `04-contextual`, `05-rerank` and `06-routing` whose switches match the grid table in contract C7

### Requirement: Retrieval metrics
For questions with gold chunks the runner SHALL compute recall@5, recall@20 and recall@50 (fraction of gold chunks in the top k), nDCG@10 with binary relevance, and MRR over the returned list, matching returned chunks to gold by `(external_id, ordinal)`, and SHALL report the number of questions each metric covers.

#### Scenario: Metric arithmetic
- **WHEN** a question has gold chunks {A, B} and the response ranks A at 3 and B at 25
- **THEN** recall@5 is 0.5, recall@20 is 0.5, recall@50 is 1.0 and the reciprocal rank is 1/3

### Requirement: Answer quality via LLM judge
When judging is enabled the runner SHALL score faithfulness (answer supported by the passages or SQL rows given to the generator) and answer relevance with the configured judge model at temperature 0 and seed 42, mapping verdicts supported/partially/unsupported and relevant/partially/irrelevant to 1/0.5/0, and SHALL report abstention accuracy on unanswerable questions and false-abstention rate on answerable ones.

#### Scenario: Abstention on unanswerable
- **WHEN** an unanswerable question's response has `answer.abstained` true
- **THEN** it counts as a correct abstention

### Requirement: Routing metrics
The runner SHALL compute route accuracy over all questions against `expected_route`, a 2×2 confusion matrix, and SQL answer accuracy for aggregate questions (route `sql` and returned rows containing the expected answer).

#### Scenario: Confusion matrix
- **WHEN** 15 aggregate questions are run with routing enabled and 13 are routed to SQL
- **THEN** the matrix's expected-sql row shows 13 sql and 2 vector

### Requirement: Latency and cost
The runner SHALL report p50 and p95 of client-measured end-to-end latency, server `total`, and every stage key present in `timings_ms`, after untimed warmup queries; and cost per query as token and pair counts multiplied by the prices in `evals/pricing.yaml` (all default 0), always reporting tokens per query alongside.

#### Scenario: Zero-price default
- **WHEN** `pricing.yaml` has all prices 0
- **THEN** cost per query is reported as 0 with an "(est.)" marker and tokens per query is non-zero

### Requirement: Reproducible run records
Each run SHALL write `evals/runs/<run_id>/<config>.json` with the config and its sha256, dataset path and sha256, seed, git sha and dirty flag, live model digests and their match against `infra/models.lock.json`, the service's `/v1/info`, timestamps, aggregate metrics, degraded and error rates, and per-question requests, compact responses (chunk identifiers and scores, not chunk text) and judge verdicts; a full-dataset, non-replay run SHALL upsert its config's row in `evals/results.md`.

#### Scenario: Row upserted
- **WHEN** `uv run evals run --config configs/05-rerank.yaml` completes on the full dataset
- **THEN** `evals/results.md` has exactly one `05-rerank` row with the new numbers and a link to the run file

#### Scenario: Results never hand-written
- **WHEN** `evals/results.md` is regenerated with `uv run evals report`
- **THEN** every number in it is traceable to a run file in `evals/runs/`

### Requirement: Drift guards
Before measuring, the runner SHALL verify live model digests against the lock file and every gold chunk's `content_sha256` against the database, exiting with code 3 on mismatch unless `--allow-model-drift` or `--allow-drift` is given, in which case the run record SHALL say so.

#### Scenario: Gold drift detected
- **WHEN** a gold chunk's content hash in the database differs from the dataset
- **THEN** the runner exits with code 3 and lists the affected question ids

### Requirement: Filter pushdown benchmark
The harness SHALL run the `filter-pre`, `filter-post` and `filter-auto` configs on filter questions and write `evals/results_filter_pushdown.md` comparing recall@20, nDCG@10, dense-leg p50/p95 latency, underfilled rate and chosen strategies.

#### Scenario: Benchmark table written
- **WHEN** `uv run evals filter-bench` completes
- **THEN** `evals/results_filter_pushdown.md` has one row per strategy with those columns
