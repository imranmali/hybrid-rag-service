# Tasks: add-eval-harness

Scope: this change owns `evals/**` only. Read-only inputs: `infra/models.lock.json`, `.env`, the running stack. Groups 1–3 need no stack (unit tests with fixtures/respx). Groups 4–7 need add-indexer (indexed DB, ≥ 150 papers), add-query-service and add-reranker-service done and `make up` healthy. Do not run `git commit`; never write numbers into `results.md` by hand.

## 1. Project setup and schemas

- [ ] 1.1 Create `evals/pyproject.toml` per design D1 and the `evals/src/evals/` package; `cd evals && uv sync`; verify `uv run evals --help` lists `dataset`, `run`, `grid`, `report`, `filter-bench`, `thresholds`, `cassettes`, `ci-lock`, `judge-agreement`.
- [ ] 1.2 Implement `config_schema.py` (contract C7, `extra="forbid"`, full `pipeline` required) and write `evals/configs/01-vector.yaml` … `06-routing.yaml` and `filter-pre.yaml`, `filter-post.yaml`, `filter-auto.yaml` exactly per the C7 grid table; tests: all nine load, grid switches match the table (asserted from a table in the test), a config missing `pipeline.rerank.candidates` or with an unknown key fails; verify `uv run pytest tests/test_configs.py`.
- [ ] 1.3 Implement `dataset/schema.py` (contract C8 rules) and `evals dataset validate`; tests with small fixture JSONL files for each rule violation (multi_chunk < 2 gold, aggregate without `expected_sql_answer`, wrong counts, manifest hash mismatch); verify `uv run pytest tests/test_dataset_schema.py`.

## 2. Metrics and runner core (eval-runner spec)

- [ ] 2.1 Implement `metrics/retrieval.py`, `metrics/routing.py`, `metrics/latency.py`, `metrics/cost.py` per design D7 and `evals/pricing.yaml` (all prices 0, comment explaining the estimate); tests: the spec example (gold {A,B} at ranks 3 and 25 → R@5 0.5, R@20 0.5, R@50 1.0, RR 1/3), nDCG hand-computed case, empty list → zeros, confusion matrix example (13/2), SQL answer normalization (scalar, list, name), percentile of a known list, zero-price cost with non-zero tokens; verify `uv run pytest tests/test_metrics.py`.
- [ ] 2.2 Implement `ollama.py` (chat per contract C3, `/api/tags`, optional Anthropic) and `metrics/judge.py` (`faith-v1`, `rel-v1`, verdict mapping 1/0.5/0, abstention handling); respx tests assert the request body (`temperature` 0, `seed` 42, `num_predict` 128, `format` json) and mapping, including malformed judge JSON → recorded as `judge_error` and excluded with a count; verify `uv run pytest tests/test_judge.py`.
- [ ] 2.3 Implement `client.py`, `drift.py` (lock comparison, gold `content_sha256` check), `runner.py` (design D6 incl. warmup, `--no-generation`, `--limit`, `--subset`), `report.py` (results.md format of design D8, upsert by config, `evals report`); respx-mocked service tests: a 3-question run writes `runs/<run_id>/<config>.json` with all required provenance fields, compact responses without chunk text, and upserts exactly one row; `--subset ci` or `--replay` runs do not touch `results.md`; incomplete config exits 3 before any request; model digest mismatch exits 3 unless `--allow-model-drift`; gold drift exits 3 listing ids (DB access mocked behind an interface). Verify `uv run pytest tests/test_runner.py tests/test_report.py`.

## 3. CI gate tooling (eval-ci-gate spec)

- [ ] 3.1 Implement `thresholds.py` and `gate.py` (design D9; exit codes 0/1/2/3; printed table); tests: thresholds = baseline − tolerance floored at 0; a breach → 1 with the row marked; any degraded response → 2 with the re-record hint; missing run → 3. Verify `uv run pytest tests/test_gate.py`.
- [ ] 3.2 Implement `cassettes.py` (wiremock snapshot per design D9, mapping-dir replacement, `meta.json`), `ci_lock.py`, and `--replay` digest sourcing; respx tests against a fake `/__admin/recordings/snapshot` response (files written, old files removed, meta fields present) and a lock file whose sha256 equals a temp file's; verify `uv run pytest tests/test_cassettes.py tests/test_ci_lock.py`.

## 4. Dataset generation and verification (eval-dataset spec)

- [ ] 4.1 Implement `dataset/sample.py`, `dataset/generate.py` (prompts of design D3), `dataset/aggregate.py` (templates T1–T6 with SQL-computed answers), `dataset/precheck.py`, `dataset/check_filters.py`, `dataset/review.py` + `verdict`; tests: seeded sampling is identical twice (DB behind an interface with a fixture), aggregate T3 answer equals a fixture SQL result, verdict appends with `by`, filter phrase regex check; verify `uv run pytest tests/test_dataset_gen.py`.
- [ ] 4.2 Run for real: `uv run evals dataset sample --seed 42 && uv run evals dataset generate && uv run evals dataset precheck && uv run evals dataset check-filters`; verify `work/candidates.jsonl` has ≥ 2× the quota per type (≥ 120/74/60/30/16) and `work/precheck.jsonl` has one entry per candidate.
- [ ] 4.3 Agent verification (design D4): review every prechecked candidate with `uv run evals dataset review --next 20` and record a verdict for each with `uv run evals dataset verdict … --notes …`; regenerate a type with `generate --type` if accepted candidates fall short; verify `work/verification.jsonl` covers every prechecked candidate and accepted counts reach the quotas.
- [ ] 4.4 Run `uv run evals dataset freeze --version v1 --seed 42` then `uv run evals dataset validate`; verify 150 records with counts 60/37/30/15/8, CI subset 20/12/10/5/3, manifest sha256 matches, every record has `human_reviewed: false`, `DATASET_CARD.md` states agent verification and pending human review, and `human_labels.template.jsonl` lists 30 ids.

## 5. Full runs

- [ ] 5.1 Smoke: `uv run evals run --config configs/06-routing.yaml --limit 10`; verify exit 0, the run file has non-empty `service_info`, digests matching the lock, 0 % errors; it does not write `results.md` (limited run).
- [ ] 5.2 Full grid: `make eval` (runs in the background for hours; judge on); verify `evals/results.md` has six rows ordered 1–6, each with every column filled and a run link, and the provenance table lists seed 42, both Ollama digests and the git sha. Report any row whose degraded rate > 0 and rerun it after fixing the cause.
- [ ] 5.3 `uv run evals filter-bench`; verify `evals/results_filter_pushdown.md` has rows for `pre`, `post`, `auto` with all columns.
- [ ] 5.4 `uv run evals judge-agreement`; verify it prints "pending: no dataset/human_labels.jsonl" (or agreement numbers if a human has filled it).

## 6. CI baseline, cassettes and snapshot

- [ ] 6.1 `make snapshot`; then `uv run evals ci-lock --snapshot data/snapshots/rag-index-<12hex>.dump --release-tag index-snapshot-<12hex>`; verify `evals/ci/snapshot.lock.json` sha256 equals `shasum -a 256` of the dump and counts equal the DB counts.
- [ ] 6.2 `make eval-record-ci` (restores nothing; uses the live DB in exact mode via the overlay, record mode); verify `evals/cassettes/ci/mappings/` is non-empty and `meta.json` has the digests and snapshot sha256.
- [ ] 6.3 `uv run evals thresholds --from runs/ci/<recorded run_id>`; verify `evals/thresholds/ci.yaml` values equal baseline − tolerance for all six configs.
- [ ] 6.4 Replay check: stop Ollama (or set `OLLAMA_HOST_URL=http://localhost:1` for the runner) and run `make eval-ci SNAPSHOT=data/snapshots/rag-index-<12hex>.dump`; verify exit 0, no degraded responses, and replayed metrics equal the recorded ones exactly. Then edit `evals/thresholds/ci.yaml` temporarily to require nDCG@10 1.01 for `01-vector`, verify exit 1, and revert.

## 7. Documentation and verification

- [ ] 7.1 Write `evals/README.md`: commands, config schema pointer (C7), metric definitions (D7), cost model, dataset provenance, how to re-record cassettes, exit codes, runtime expectations. Verify each command in it runs as written.
- [ ] 7.2 Final verification from `evals/`: `uv run ruff check . && uv run ruff format --check . && uv run pytest` pass; from the repo root `make lint` and `make test` include evals and pass; `uv run evals dataset validate` passes.
