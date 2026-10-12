# Proposal: add-ci-docs-load

## Why

BUILD_SPEC's reviewer reads the README for one minute, looks for the CI eval gate ("unusual and reviewers notice it"), and reads ADRs "more often than the code". The project also needs a load test so the latency claims hold under concurrency. This change packages the finished system: CI that builds, lints and tests both sides and gates on eval regressions, a k6 load script, seven ADRs, the README in BUILD_SPEC's exact order, and the architecture SVG.

## What Changes

- `.github/workflows/ci.yml`: jobs `python` (matrix indexer/reranker/evals: ruff + pytest incl. Testcontainers), `kotlin` (ktlint + `./gradlew build` with Testcontainers), `docker` (build reranker and service images), `eval-gate` (50-question subset in deterministic replay mode per foundation contract C9: DB snapshot from a GitHub Release + WireMock cassettes + exact vector search, thresholds from `evals/thresholds/ci.yaml`), `load-lint` (k6 script syntax), `docs` (README section order, SVG well-formed, unfilled-placeholder warnings).
- `load/query.js` (k6) with three scenarios — retrieval-only at 20 rps, rerank at 0.5 rps, full pipeline at 0.05 rps — thresholds, a degraded-rate metric and JSON summaries in `load/results/`; `load/README.md`.
- `docs/adr/0001…0007` (topics from BUILD_SPEC), ~150 words each, numbers as `{{RESULT: …}}` placeholders pointing at the exact source file.
- `README.md` structured exactly per BUILD_SPEC "README — the first 60 seconds", with placeholders the orchestrator fills from real results, plus non-goals and deferred-on-purpose sections below the fold.
- `docs/architecture.svg` (hand-authored diagram: offline path, online stages, dependencies, observability) and `docs/img/` for the trace-waterfall screenshot.

## Capabilities

### New Capabilities

- `ci-pipeline`: The GitHub Actions workflow: build, lint, test both languages, image builds, and the replay-mode eval regression gate.
- `load-testing`: The k6 load script, its scenarios, thresholds and outputs.
- `project-documentation`: README structure and placeholders, the seven ADRs, and the architecture diagram.

### Modified Capabilities

(none)

## Impact

- Owns `.github/**`, `load/**`, `docs/adr/**`, `docs/architecture.svg`, `docs/img/**`, `README.md`.
- The eval-gate job needs a GitHub Release asset holding the DB snapshot named by `evals/ci/snapshot.lock.json`; uploading it (`gh release create … --notes … <dump>`) is an orchestrator step after the repo is pushed.
- Reads (never edits) `Makefile`, `infra/`, `evals/`, `indexer/bench/`, `service/`, `reranker/`.

## Implementation order

Last. Depends on all five other changes: CI calls their test commands, the eval gate needs evals' cassettes, thresholds and snapshot lock, and the README/ADRs point at results produced by add-eval-harness and add-indexer's benchmark.

## Non-goals

- No deployment, release automation or container registry publishing (no production target).
- No live-model eval in CI (no GPU/Ollama on hosted runners; see design D1). Faithfulness and answer relevance are measured locally only.
- No load testing in CI (results depend on the dev machine's local models).
- No roadmap, RAG tutorial or scope apologies in the README (BUILD_SPEC "what to leave out"); README states BUILD_SPEC non-goals and deferred items in one line each.
- No invented numbers: every metric in README/ADRs is a placeholder until filled from a real run file.
