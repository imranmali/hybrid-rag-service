# evals

The evaluation harness for the Hybrid RAG Service. Every component of the pipeline must be
justified by a before/after number from a frozen dataset, produced by one command, with the seed,
model digests and git sha recorded. This package builds that dataset, runs the ablation grid
against the live Kotlin service, writes `results.md`, and gates CI on a 50-question subset.

Python 3.12, managed with `uv`. Run everything from this directory:

```bash
cd evals
uv sync
uv run evals --help
```

Settings come from `../.env` (copy `../.env.example`): `RAG_SERVICE_URL`, `EVALS_DATABASE_URL`
(the read-only `rag_reader` role), `OLLAMA_HOST_URL`, `OLLAMA_LLM_MODEL`, `LLM_PROVIDER`.
Nothing requires an API key; `LLM_PROVIDER=anthropic` is opt-in and needs `ANTHROPIC_API_KEY` and
`ANTHROPIC_MODEL`.

## Commands

| Command | What it does |
|---|---|
| `make eval` | `uv run evals grid`: the six grid configs, in order, full dataset, judge on |
| `uv run evals run --config configs/05-rerank.yaml` | one config |
| `uv run evals grid [--subset ci] [--no-generation] [--no-judge] [--replay] [--gate thresholds/ci.yaml]` | the six grid configs |
| `uv run evals report` | rebuild `results.md` from the run files |
| `uv run evals filter-bench` | pre- vs post-filter benchmark, writes `results_filter_pushdown.md` |
| `uv run evals thresholds --from runs/ci/<run_id>` | write `thresholds/ci.yaml` from a real baseline run |
| `uv run evals cassettes snapshot` | capture WireMock traffic into `cassettes/ci/` |
| `uv run evals ci-lock --snapshot <dump> [--release-tag T]` | write `ci/snapshot.lock.json` |
| `uv run evals judge-agreement` | judge vs human agreement; prints `pending: no dataset/human_labels.jsonl` until a human fills it |
| `make eval-record-ci` / `make eval-ci` | record the CI cassettes / run the offline gate (see below) |

Flags shared by `run` and `grid`:

| Flag | Meaning |
|---|---|
| `--subset ci` | only the 50 `ci_subset` questions. With `--no-generation` (the CI gate) results go to `runs/ci/` and never `results.md`; with generation and judge on it is a `gen_subset` results run stored under `runs/` |
| `--limit N` | first N questions, interleaved across types so a smoke run covers every type; never writes `results.md` |
| `--no-generation` | sends `generation.enabled=false`; skips judging; a full-dataset run is a `retrieval-only` results run |
| `--no-judge` | skip the LLM judge; never writes `results.md` |
| `--replay` | take model digests from `cassettes/ci/meta.json` instead of live Ollama |
| `--allow-model-drift` / `--allow-drift` | continue despite Ollama digest drift / gold chunk drift; recorded in the run file |

Only the run kinds listed under [Behaviours worth knowing](#behaviours-worth-knowing) reach
`results.md`; every other run is stored under `runs/` and ignored by `evals report`.

### Dataset commands

`evals dataset sample | generate | precheck | check-filters | review | verdict | freeze | validate`.
See [Dataset](#dataset) for the workflow. `uv run evals dataset validate` checks the frozen file
against schema, gold rules, exact type counts and the manifest hash.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | ok (with `--gate`: every metric meets its threshold and nothing degraded) |
| 1 | a metric is below its threshold (`--gate`) |
| 2 | a response was degraded or a request failed (`--gate`); in `--replay` this usually means a cassette miss |
| 3 | setup, validation or drift error: bad config, unreachable service, model digest drift, gold chunk drift, invalid dataset, missing run |

Exit 2 outranks exit 1: a cassette miss both degrades the response and lowers the metrics.

## Behaviours worth knowing

- **`--limit N` interleaves question types.** It takes the first N questions round-robin across
  types, so a 10-question smoke run covers every type rather than 10 factoids.
- **Exit 2 outranks exit 1.** A cassette miss both degrades a response and lowers the metrics, so
  the gate reports the degraded/failed-request code, and prints the re-record hint in `--replay`.
- **Failed requests are never dropped.** An errored request scores zero retrieval, a wrong route
  and a wrong SQL answer, is excluded from latency and the confusion matrix, and is reported in
  the error rate. Rows with degraded or failed requests are flagged in `results.md`.
- **`results.md` is built from three kinds of run, never from anything else.** It is rebuilt
  from the latest run file per config and role: `full` (all five question types, generation and
  judge on) fills every column; `retrieval` (all types, `--no-generation`) fills the retrieval,
  routing and latency columns and the Mode column reads `retrieval-only`; `gen_subset`
  (`--subset ci` with generation and judge on) fills the generation/judge columns, marked
  `(ci50)`, with a separate "Gen/judge run" link. A cell with no measurement shows `—`. Runs with
  `--limit`, `--replay`, `--no-judge`, or `--subset ci --no-generation` (the CI gate runs, kept
  under `runs/ci/`) never reach `results.md`.
- The judge runs after all queries, so judge calls never sit inside latency measurements.
- `--replay` still compares the digests recorded in `cassettes/ci/meta.json` with
  `infra/models.lock.json` and exits 3 on mismatch unless `--allow-model-drift` is given.

## Config files

`configs/<name>.yaml` follows the eval-config contract (C7 in
`openspec/changes/add-platform-foundation/design.md`). The `pipeline` block is required and
complete (every key of the `/v1/query` `pipeline` object); unknown keys anywhere are rejected, and
a missing key such as `pipeline.rerank.candidates` makes the runner exit 3 before it sends a
request. The `pipeline` is sent verbatim on every query.

| grid_row | name | vector | bm25 | filters | contextual | rerank | routing |
|---|---|---|---|---|---|---|---|
| 1 | `01-vector` | yes | no | no | no | no | no |
| 2 | `02-hybrid` | yes | yes | no | no | no | no |
| 3 | `03-filters` | yes | yes | yes | no | no | no |
| 4 | `04-contextual` | yes | yes | yes | yes | no | no |
| 5 | `05-rerank` | yes | yes | yes | yes | yes | no |
| 6 | `06-routing` | yes | yes | yes | yes | yes | yes |

`filter-pre`, `filter-post` and `filter-auto` are auxiliary configs (filter questions only,
retrieval only) for the pushdown benchmark.

## Metrics

Definitions (all means are over the questions the metric covers; `n` is reported beside each):

- **recall@k** = |gold ∩ top-k| / |gold|, k in {5, 20, 50}. Chunks are matched to gold by
  `(external_id, ordinal)`.
- **nDCG@10**, binary relevance: DCG = sum over the top 10 of 1/log2(rank + 1) for gold hits;
  IDCG assumes min(|gold|, 10) hits at the top.
- **MRR** = mean of 1 / rank of the first gold hit (0 if none).
- A SQL-routed response has no chunks, so a mis-routed question scores 0 retrieval. A failed
  request also scores 0 and is counted in the error rate.
- **Faithfulness** (answer supported by the passages or SQL rows given to the generator) and
  **answer relevance**: LLM judge at temperature 0, seed 42; verdicts supported / partially /
  unsupported and relevant / partially / irrelevant map to 1 / 0.5 / 0. A malformed judge reply is
  recorded as `judge_error` and excluded (the count is reported). Abstentions score faithfulness 1
  and, on answerable questions, relevance 0.
- **Abstention accuracy** = abstained / unanswerable. **False-abstention rate** = abstained /
  answerable questions whose answer status is `ok`.
- **Route accuracy** over all questions, with a 2x2 confusion matrix; **SQL answer accuracy** over
  aggregate questions (route is `sql` and the rows contain the expected answer; list answers
  compare the set of first-column values, case-insensitively).
- **Latency**: p50 and p95 (linear interpolation) of client end-to-end time, server `total` and
  every stage key in `timings_ms`, after untimed warmup queries. Concurrency is 1 so the numbers
  are not contended.
- **Degraded rate** (non-empty `degraded`, with counts per code) and **error rate** (non-200).

## Cost model

Cost per query = `embed_prompt_tokens x embedding_per_1m / 1e6 + llm_prompt_tokens x
llm_prompt_per_1m / 1e6 + llm_completion_tokens x llm_completion_per_1m / 1e6 + rerank_pairs x
rerank_per_1k / 1e3`, using the prices in `pricing.yaml`. Everything runs locally, so all prices
default to 0 and the column reads `0 (est.)`; tokens per query is always shown next to it. Judge
tokens are excluded (evaluation overhead, not serving cost).

## Run records

Each run writes `runs/<run_id>/<config>.json` (CI: `runs/ci/<run_id>/`) plus `grid.json` for a
grid. `run_id` is `YYYYMMDDTHHMMSSZ-<git sha>`. A record contains the config and its sha256, the
exact `pipeline` sent, dataset path and sha256, seed, git sha and dirty flag, live model digests
and whether they match `infra/models.lock.json`, the service's `/v1/info`, timestamps, aggregate
metrics, degraded and error rates, and per-question requests, compact responses (chunk ids and
scores, not chunk text) and judge verdicts. `results.md` and `results_filter_pushdown.md` are
generated from these files; nothing in them is written by hand.

Before measuring, the runner verifies the Ollama digests against `infra/models.lock.json` and
every gold chunk's `content_sha256` against the database. Either mismatch exits 3 unless the
matching `--allow-*` flag is passed, in which case the run record says so.

## Dataset

`dataset/questions.v1.jsonl` has 150 questions (60 factoid, 37 multi_chunk, 30 filter, 15
aggregate, 8 unanswerable); 50 of them are flagged `ci_subset` (20/12/10/5/3). Gold chunks are
`(external_id, ordinal)` plus `content_sha256`, never database row ids, so they survive a
re-index. `dataset/MANIFEST.json` records the sha256, counts, seed, generator and digests;
`dataset/DATASET_CARD.md` describes provenance.

**Provenance, stated plainly (details and deviations are in `dataset/DATASET_CARD.md`):** candidates are LLM-generated (llama3.1:8b by default; aggregate
questions come from SQL templates whose answers are computed by SQL, never by the LLM), pass an
automated LLM precheck, and are then verified by an agent against the source chunks, with every
verdict (verifier identity and notes) logged in `dataset/work/verification.jsonl`. **Human review
is pending**; every record has `human_reviewed: false`. The judge shares a model family with the
generator, so self-preference bias is possible. `dataset/human_labels.template.jsonl` lists 30
seeded answerable ids for the judge-agreement check.

Workflow (needs the indexed database and the running service):

```bash
uv run evals dataset sample --seed 42     # seeded chunk samples (--oversample, default 2x)
uv run evals dataset generate             # LLM candidates + SQL-template aggregates (--type, --n, --oversample)
uv run evals dataset precheck             # LLM answerability check
uv run evals dataset check-filters        # expected filters vs what the service extracts
uv run evals dataset review --next 20     # evidence for the verifier
uv run evals dataset verdict --id cand-factoid-0001 --status accept --by agent:<model id> --notes "..."
uv run evals dataset freeze --version v1 --seed 42
uv run evals dataset validate
```

`verdict` refuses to accept a candidate that failed the precheck, and refuses to accept a filter
candidate whose expected filters differ from the extracted ones (edit the question until they
match). `freeze` refuses if a type lacks accepted candidates and names the
`generate --type` command to rerun. A frozen version is never edited: a change is a new
`questions.v2.jsonl`.

## CI gate

`make eval-ci` restores the DB snapshot, starts the service against WireMock replaying recorded
model and reranker traffic, and runs
`uv run evals grid --subset ci --no-generation --no-judge --replay --gate thresholds/ci.yaml`.
No Ollama or GPU is needed. Dense retrieval runs in exact-search mode so the reranker requests
are identical between recording and replay.

- `thresholds/ci.yaml` is generated by `uv run evals thresholds --from runs/ci/<run_id>` from a
  real baseline run: each minimum is the baseline minus the tolerance (0.03 for nDCG@10 and
  recall@20; 0.05 route accuracy and 0.10 SQL answer accuracy for `06-routing`), floored at 0,
  with zero allowed degraded responses.
- `ci/snapshot.lock.json` describes the DB snapshot the cassettes were recorded against (release
  tag, asset name, sha256, pipeline fingerprint, counts); generate it with
  `uv run evals ci-lock --snapshot data/snapshots/rag-index-<12hex>.dump`.

### Re-recording the cassettes

Needed whenever an intentional change alters the model or reranker requests (prompts, retrieval
parameters, models). Requires local Ollama, the reranker and the indexed database:

```bash
make snapshot                                  # data/snapshots/rag-index-<12hex>.dump
uv run evals ci-lock --snapshot data/snapshots/rag-index-<12hex>.dump
make eval-record-ci                            # records cassettes/ci/ via WireMock proxying
uv run evals thresholds --from runs/ci/<run_id>
```

`cassettes snapshot` calls WireMock's `POST /__admin/recordings/snapshot` with exact JSON body
matching, replaces `cassettes/ci/mappings/`, and writes `cassettes/ci/meta.json` (recording time,
git sha, model digests, reranker model and revision, snapshot sha256). A request with no recorded
response in replay mode makes the gate exit 2 and print the re-record hint.

## Runtime expectations

Estimates from the example timings in the API contract (rerank about 4 s on CPU, generation
about 9 s, routing about 0.6 s), not measurements: roughly 15 to 25 s per question end to end, so
one config over 150 questions takes about 40 to 60 minutes of querying plus judging, and the full
grid takes several hours. Run it in the background (`make eval`); use `--limit` for smoke runs and
`--no-generation --no-judge` for fast retrieval-only iterations. The CI subset without generation
runs in minutes.

## Tests

```bash
uv run ruff check . && uv run ruff format --check . && uv run pytest
```

Unit tests use fixtures, respx-mocked HTTP and an in-memory corpus; nothing needs the live stack.
Tests that do are marked `live` and deselected by default.
