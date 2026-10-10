# Proposal: add-eval-harness

## Why

BUILD_SPEC: "This is the part that separates the repo from every other RAG repo." Every component must be justified by a before/after number from a frozen dataset, produced by one command, with seed, model digests and git sha recorded — and a 50-question subset must fail CI on regression. Without the harness the ablation table that "*is* the portfolio" cannot exist.

## What Changes

- New uv project `evals/` (Python 3.12) with a Typer CLI `evals`.
- Dataset pipeline: seeded chunk sampling from the indexed DB, LLM candidate generation for factoid / multi-chunk / filter / unanswerable questions, deterministic SQL-template generation for aggregate questions, automated LLM precheck, an agent verification step with a recorded verdict log, and a freeze step producing `evals/dataset/questions.v1.jsonl` (150 questions, mix 60/37/30/15/8, gold chunks keyed by `(external_id, ordinal)` + content hash), `MANIFEST.json` and an honest `DATASET_CARD.md`.
- Six grid configs `evals/configs/01-vector.yaml` … `06-routing.yaml` plus `filter-pre|post|auto.yaml`, validated against the foundation eval-config schema and sent verbatim as the service's `pipeline`.
- Runner calling the Kotlin service over HTTP: recall@5/20/50, nDCG@10, MRR, faithfulness and answer relevance (LLM judge, temperature 0), abstention accuracy, route accuracy with confusion matrix, SQL answer accuracy, p50/p95 per stage and end to end, cost per query (token counts × configurable price table, default 0), degraded rate.
- Output: one row per config in `evals/results.md` and raw JSON per run in `evals/runs/`, recording seed, model digests, service info, dataset hash and git sha; drift checks for models and gold chunks.
- CI gate: 50-question subset, regression thresholds file generated from a real baseline run, WireMock cassette recording for replay mode, DB snapshot lock file, pre/post filter benchmark and judge-agreement tooling.

## Capabilities

### New Capabilities

- `eval-dataset`: Generating, verifying and freezing the question set with stable gold-chunk identifiers and provenance.
- `eval-runner`: Running named configs against the query service, computing quality/latency/cost metrics, and writing reproducible results.
- `eval-ci-gate`: The 50-question subset, regression thresholds, replay-mode cassettes, snapshot lock and the gate's exit codes.

### Modified Capabilities

(none)

## Impact

- Owns `evals/**` only. Reads the DB as `rag_reader` (`EVALS_DATABASE_URL`), calls the service (`RAG_SERVICE_URL`), host Ollama (generation, judge, digests) and wiremock admin (`localhost:8089`) during recording; reads `infra/models.lock.json`.
- Produces the numbers README, ADRs and CI consume (`add-ci-docs-load`).
- A full six-row grid with generation and judging takes several hours on the dev machine; the CI subset in replay mode takes minutes.

## Implementation order

Depends on `add-platform-foundation`, and for real runs on `add-indexer` (indexed DB with ≥ 150 papers) and `add-query-service` (running API); grid rows 5–6 also need `add-reranker-service`. Code and unit tests can start in parallel with those changes; dataset generation and all real runs start once they are done. Must finish before `add-ci-docs-load` fills the README and wires the CI gate.

## Non-goals

- No hand-labelled gold set beyond what an agent can verify now; human review is explicitly pending and stated as such (config: never overstate verification).
- No more than 150 questions (BUILD_SPEC risk table cap).
- No fabricated or hand-edited metric numbers: `results.md` is written only by the runner.
- No online/A-B evaluation, no user-feedback loop, no agentic evaluation (BUILD_SPEC non-goal #10).
- No paid-API dependency: the judge defaults to local `llama3.1:8b`; Anthropic is opt-in.
