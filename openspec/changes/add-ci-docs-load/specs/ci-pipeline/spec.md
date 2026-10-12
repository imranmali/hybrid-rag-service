# Spec Delta: ci-pipeline

## Purpose

Runs on every push and pull request to build, lint and test the Kotlin and Python sides and to fail the build when retrieval or routing quality regresses on the 50-question eval subset.

## ADDED Requirements

### Requirement: Build, lint and test both sides
The workflow `.github/workflows/ci.yml` SHALL run on pushes to `main` and on pull requests, and SHALL fail if `ruff check`, `ruff format --check` or `pytest` fails in any of `indexer`, `reranker` or `evals` (excluding tests marked `model` or `live`), or if `./gradlew ktlintCheck build` fails in `service` (including Testcontainers integration tests).

#### Scenario: Ruff violation fails the build
- **WHEN** a pull request introduces an unused import in `evals/`
- **THEN** the `python (evals)` job fails

#### Scenario: Integration tests run against real Postgres
- **WHEN** the `kotlin` job runs
- **THEN** the service's Testcontainers tests start a `pgvector/pgvector:pg17` container and pass

### Requirement: Container images build
The workflow SHALL build the `reranker` and `service` Docker images without pushing them.

#### Scenario: Broken Dockerfile fails
- **WHEN** a change breaks `service/Dockerfile`
- **THEN** the `docker` job fails

### Requirement: Eval regression gate in replay mode
An `eval-gate` job SHALL download the DB snapshot named by `evals/ci/snapshot.lock.json` from the repository's GitHub Release, verify its sha256, run `make eval-ci` (snapshot restore, replay overlay, six grid configs on the 50-question subset with generation and judging off, gate against `evals/thresholds/ci.yaml`), and fail the build on any non-zero gate exit code, without requiring Ollama, a GPU or network access to model hosts.

#### Scenario: Regression blocks merge
- **WHEN** a pull request lowers nDCG@10 of any grid config below its threshold
- **THEN** the `eval-gate` job fails with the gate's metric table in the log

#### Scenario: Cassette miss blocks merge
- **WHEN** a pull request changes what the service sends to a model so a request has no recording
- **THEN** the `eval-gate` job fails and the log says to run `make eval-record-ci`

#### Scenario: Missing snapshot lock
- **WHEN** `evals/ci/snapshot.lock.json` is absent or its release asset cannot be downloaded
- **THEN** the job fails with a message explaining how to publish the snapshot, rather than passing silently

#### Scenario: Corrupted snapshot rejected
- **WHEN** the downloaded snapshot's sha256 differs from the lock file
- **THEN** the job fails before restoring it

### Requirement: Diagnostics on failure
When the eval gate fails the workflow SHALL upload `evals/runs/ci/` and the compose logs as artifacts.

#### Scenario: Artifacts available
- **WHEN** the `eval-gate` job fails
- **THEN** the run's artifacts include the CI run JSON files and a compose log file

### Requirement: Documentation and script checks
The workflow SHALL check that `load/query.js` parses with k6, that `docs/architecture.svg` is well-formed XML, and that `README.md` contains its sections in the BUILD_SPEC order, emitting a warning annotation (not a failure) for each unfilled `{{RESULT:` placeholder.

#### Scenario: Unfilled placeholder warned
- **WHEN** README still contains a `{{RESULT:` placeholder
- **THEN** the `docs` job passes and shows a warning annotation naming the line
