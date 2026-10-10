# Tasks: add-ci-docs-load

Scope: this change owns `.github/**`, `load/**`, `docs/adr/**`, `docs/architecture.svg`, `docs/img/**` and `README.md`. Do not edit other directories. Prerequisite: all five other changes applied, `evals/results.md` and the CI artifacts (`evals/thresholds/ci.yaml`, `evals/cassettes/ci/`, `evals/ci/snapshot.lock.json`, a local snapshot under `data/snapshots/`) exist. Never type a metric value by hand — use placeholders. Do not run `git commit` or push.

## 1. CI workflow (ci-pipeline spec)

- [x] 1.1 Write `.github/workflows/ci.yml` with jobs `python` (matrix), `kotlin`, `docker`, `eval-gate`, `load-lint`, `docs` exactly per design D2, including the header comment summarising design D1; verify `docker run --rm -v "$PWD:/repo" -w /repo rhysd/actionlint:1.7.3 -color` reports no errors.
- [x] 1.2 Write `.github/scripts/check_readme.sh` (section order of design D4; `::warning` per `{{RESULT:` placeholder; exit 1 only on order violations) and `.github/scripts/eval_gate.sh` (lock parsing, missing-lock message, release download, sha256 check, `make eval-ci`, exit-code-2 hint) called by the workflow; verify `bash .github/scripts/check_readme.sh` exits 0 on the final README (task 3.x) and exits 1 on a copy with sections 2 and 3 swapped; verify `bash .github/scripts/eval_gate.sh` exits non-zero with the publishing message when run with `LOCK=/nonexistent`.
- [ ] 1.3 Run the gate path locally without GitHub: `SKIP_DOWNLOAD=1 SNAPSHOT=data/snapshots/<asset> bash .github/scripts/eval_gate.sh`; verify it verifies the sha256 against the lock, runs `make eval-ci`, and exits 0.
- [x] 1.4 Run the remaining job commands locally to prove they pass: for each of `indexer reranker evals`: `cd <pkg> && uv sync --frozen && uv run ruff check . && uv run ruff format --check . && uv run pytest -m "not model and not live"`; `cd service && ./gradlew ktlintCheck build --no-daemon`; `docker build reranker/` and `docker build service/`; verify all exit 0.

## 2. Load test (load-testing spec)

- [x] 2.1 Write `load/query.js` per design D3 (pipelines copied from the three eval configs with a source comment, three scenarios, `SCENARIO` filter, thresholds, `rag_degraded`, stage trends, `handleSummary`) and `load/results/.gitkeep`; verify `docker run --rm -v "$PWD/load:/load" grafana/k6:0.54.0 inspect /load/query.js` exits 0 and a small node/jq check confirms each JS pipeline equals the YAML config's (`python3 -c` comparing the JSON printed by `k6 inspect` options or a `--env PRINT_PIPELINES=1` dry path).
- [ ] 2.2 With the stack up and indexed, run `SCENARIO=retrieval make load`; verify a `load/results/summary-retrieval-*.json` file appears and the text summary shows the threshold result. Then run the full `make load`; verify three scenario results.
- [ ] 2.3 Write `load/README.md` (how to run, scenarios, thresholds, where summaries go, the measured numbers from 2.2 copied from the summary files with their filenames); verify the commands run as written.

## 3. Documentation (project-documentation spec)

- [x] 3.1 Write `docs/architecture.svg` per design D6; verify `xmllint --noout docs/architecture.svg`, file size < 50 KB, and `grep -o '<text[^>]*>[^<]*' docs/architecture.svg` includes `route`, `retrieve.vector`, `retrieve.bm25`, `fuse`, `rerank`, `generate`, `Postgres`, `Ollama`, `reranker`. Render it (`qlmanage -t -s 1200 -o /tmp docs/architecture.svg` or open in a browser) and check visually that no labels overlap.
- [x] 3.2 Write the seven ADRs in `docs/adr/` with the filenames of the project-documentation spec and the content of design D5; verify a word count script (`for f in docs/adr/0*.md; do sed '/^#/d;/^Status:/d' "$f" | wc -w; done`) gives 120–200 for each, and every number is a `{{RESULT: … | source: …}}` placeholder whose source file exists.
- [x] 3.3 Write `README.md` exactly per design D4 (sections 1–7 in order, then the below-the-fold sections), latency budget timeouts filled from contract C11, metrics as placeholders; create `docs/img/.gitkeep` and `docs/img/README.md` describing how to capture `trace-waterfall.png` from Grafana (Explore → Tempo → a `06-routing` query trace). Verify `bash .github/scripts/check_readme.sh` exits 0, every placeholder's `source:` path exists (`grep -o 'source: [^}|]*' README.md docs/adr/*.md | awk '{print $2}' | sort -u | xargs ls`), and all relative links resolve (`grep -o '](\([^)#]*\))' README.md` paths exist except `docs/img/trace-waterfall.png`).
- [ ] 3.4 Write the placeholder fill checklist at the end of this change's tasks run report (not in a file): list every placeholder with its source so the orchestrator can fill them; verify the count equals `grep -o '{{RESULT:' README.md docs/adr/*.md | wc -l`.

## 4. Verification

- [ ] 4.1 Cold-clone check of "Run it": in a fresh `git clone`/copy of the working tree in a temp dir (stack already holding an index is acceptable for `make index` idempotency), run `cp .env.example .env && make up && make index && make query Q="What is late interaction in ColBERT?"`; verify the final command prints a JSON response with non-empty `chunks`.
- [ ] 4.2 Run `docker run --rm -v "$PWD:/repo" -w /repo rhysd/actionlint:1.7.3`, `make lint`, `make test`, `bash .github/scripts/check_readme.sh` and `xmllint --noout docs/architecture.svg`; verify all exit 0.
