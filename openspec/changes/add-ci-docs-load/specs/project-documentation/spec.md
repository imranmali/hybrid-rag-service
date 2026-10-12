# Spec Delta: project-documentation

## Purpose

Gives reviewers the strongest evidence first: a README ordered exactly as BUILD_SPEC prescribes, seven short ADRs, and one architecture diagram, with every number traceable to a real result file.

## ADDED Requirements

### Requirement: README order
`README.md` SHALL begin with, in this order: (1) one sentence stating what the system is and that it runs on arXiv full-text papers; (2) the six-row ablation table with nDCG@10, recall@20, p95 latency and cost per query; (3) the architecture diagram `docs/architecture.svg`; (4) three findings, each with a number, at least one negative; (5) "Run it" with `docker compose up` via `make up`, one command to index and one to query; (6) a per-stage latency budget table; (7) links to the seven ADRs, one line each. Below these it MAY add the trace waterfall screenshot, the eval dataset link, CI gate explanation, non-goals and deferred items.

#### Scenario: Results above the fold
- **WHEN** the README is rendered
- **THEN** the results table appears immediately after the first sentence with no other heading or paragraph in between

#### Scenario: No tutorial or roadmap
- **WHEN** the README is searched for a "What is RAG" section or a roadmap section
- **THEN** none exists

### Requirement: Traceable placeholders
Every metric in the README and ADRs SHALL be either a number copied from a committed result file or a placeholder of the form `{{RESULT: <metric> | source: <repo path>}}` naming an existing file that will contain it.

#### Scenario: Placeholder sources exist
- **WHEN** all placeholders are listed with `grep -o '{{RESULT:[^}]*}}' README.md docs/adr/*.md`
- **THEN** every `source:` path refers to a file produced by the runner, the indexer benchmark or the load script

### Requirement: Run instructions work from a cold clone
The "Run it" section SHALL list the exact commands `cp .env.example .env`, `make models`, `make up`, `make index` and `make query Q="…"`, which SHALL work on a machine with Docker and Ollama and no prior state.

#### Scenario: Cold clone
- **WHEN** a fresh clone runs the listed commands in order
- **THEN** the final command prints a JSON response with retrieved chunks

### Requirement: Seven ADRs
`docs/adr/` SHALL contain `0001-postgres-pgvector-over-elasticsearch.md`, `0002-hnsw-over-ivfflat.md`, `0003-kotlin-serving-python-offline.md`, `0004-self-hosted-cross-encoder.md`, `0005-contextual-chunking-and-chunk-size.md`, `0006-filter-pushdown-policy.md` and `0007-no-agent-loop.md`, each with Status, Context, Decision and Consequences sections and 120–200 words of body text.

#### Scenario: Word budget
- **WHEN** each ADR body's words are counted excluding headings
- **THEN** every count is between 120 and 200

### Requirement: Architecture diagram
`docs/architecture.svg` SHALL be a well-formed standalone SVG showing the offline path (arXiv → indexer → Ollama → Postgres), the online stages named `route`, `retrieve.vector`, `retrieve.bm25`, `fuse`, `rerank` and `generate`, the dependencies (Postgres, host Ollama, reranker container) and the tracing path (OTel collector → Tempo → Grafana).

#### Scenario: Stage names present
- **WHEN** the SVG's text elements are extracted
- **THEN** all six stage names and the words Postgres, Ollama and reranker appear
