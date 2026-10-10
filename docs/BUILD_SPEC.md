# Hybrid RAG Service — Build Spec

Oct 10, 2026 · @Imran

## Goal and positioning

One repo that proves you can build and measure a retrieval system, not ten that prove you can follow tutorials.

Pitch line for the CV: "Hybrid RAG service on Postgres/pgvector — metadata-filtered BM25 + vector retrieval, cross-encoder reranking, SQL/vector query routing. Kotlin serving layer, Python eval harness, p95 latency and nDCG@10 committed per release."

What it signals, in the order a reviewer notices it:

1. Measurement discipline — every component justified by a before/after number, not by a blog post.
2. Backend seniority — JVM service with pooling, timeouts, circuit breakers and OTel traces around a flaky LLM dependency.
3. Operational restraint — one datastore, not five. Postgres does vectors, full-text and relational.
4. AI literacy — chunking, reranking and routing treated as tradeoffs with latency and cost attached.

The anti-goal is a Streamlit demo. There are tens of thousands of those and at senior level they read as negative signal.

## Scope

Four of the ten patterns, fused into one system. Each earns its place by moving a metric.

| Capability | Source pattern | Why it is in scope |
| --- | --- | --- |
| Contextual chunking | #4 | Chunk quality dominates everything downstream; cheapest win available |
| Metadata-filtered hybrid retrieval | #2 | BM25 + vector + structured predicates in one SQL query — the real production shape |
| Cross-encoder reranking | #3 | Largest single quality jump; forces an explicit latency-vs-quality tradeoff |
| SQL + vector routing | #5 | Proves you understand that not every question is a retrieval question |

Non-goals, stated in the README so nobody assumes you missed them:

- No knowledge graph (#6) — Neo4j adds an operational dependency for a payoff this corpus cannot demonstrate.
- No multimodal (#9) — expensive to evaluate honestly, shallow to implement.
- No agent loop (#10) — unbounded retrieval without evals is a demo, and the eval harness here is the point.
- No fine-tuning, no custom embedding training, no UI beyond a thin query console.
- Single tenant, single corpus, English only.

Optional extension, only once the core ships with numbers: a corrective retrieval gate (#7) — score the retrieved set, rewrite the query and retry once if it falls below threshold. Bounded, measurable, honest.

Corpus choice matters more than people expect. Pick one with real metadata and a structured side: German legislation and regulation, EU public procurement notices, or arXiv with author/date/category plus a citation count table. Avoid Wikipedia dumps — no filters worth filtering on.

## Architecture

Two processes, one database. Python writes the index offline; Kotlin serves the query path and never writes to it.

&#91;embedded content: architecture · 4 query stages, 3 dependencies, 1 offline path\]

The seam is deliberate: Python owns everything with a model dependency and no latency budget, Kotlin owns everything with a latency budget and no model dependency. That is the sentence to say out loud in an interview.

The serving path degrades rather than fails. If the cross-encoder is unreachable, the breaker opens and fusion-ranked results go straight to generation with a flag on the response. If the LLM is unreachable, the API returns retrieved passages and a 503 on the generated field. Both paths are covered by tests.

## Data model

One Postgres database, three concerns: documents, chunks with embeddings, and the structured business tables the router can query directly.

```sql
create extension if not exists vector;
create extension if not exists pg_trgm;

create table documents (
  id            uuid primary key,
  external_id   text not null unique,
  title         text not null,
  author        text,
  category      text not null,
  published_on  date not null,
  jurisdiction  text,
  source_url    text,
  raw_text      text not null,
  ingested_at   timestamptz not null default now()
);

create table chunks (
  id            bigserial primary key,
  document_id   uuid not null references documents(id) on delete cascade,
  ordinal       int  not null,
  content       text not null,          -- the chunk as embedded
  context_header text,                  -- LLM-written doc-level situating sentence
  token_count   int  not null,
  embedding     vector(1024) not null,
  tsv           tsvector generated always as (
                  to_tsvector('simple', coalesce(context_header,'') || ' ' || content)
                ) stored,
  unique (document_id, ordinal)
);

create index chunks_embedding_hnsw on chunks
  using hnsw (embedding vector_cosine_ops) with (m = 16, ef_construction = 64);
create index chunks_tsv_gin on chunks using gin (tsv);
create index documents_category_date on documents (category, published_on desc);
```

Decisions worth an ADR each:

- **Embedding dimension 1024.** BGE-m3 or similar multilingual model; 1536 doubles index size for no measured gain on this corpus. Record the comparison.
- **`context_header` stored separately** from `content` so you can ablate contextual chunking by switching one column in or out of the embedded text and the tsvector.
- **Metadata lives on `documents`, not duplicated onto `chunks`.** Filters join. Measure the join cost before denormalising; if HNSW recall degrades under restrictive filters, that is a finding worth writing up, not a bug to hide.
- **HNSW over IVFFlat** — no training step, better recall at low latency, worse build time. Note the build time in the README.
- **Structured side:** two or three real tables the router can hit with generated SQL (for procurement: `tenders`, `awards`, `buyers`). Questions like "how many contracts did X win in 2025" must be answered by SQL, never by retrieval.

Filter pushdown is the subtle part. Naive pre-filtering kills HNSW recall when the predicate is selective. Implement both paths — pre-filter with a `WHERE` before the ANN scan, and over-fetch then post-filter — and pick per query based on estimated selectivity. The benchmark comparing them is one of the strongest things in the repo.

## Eval harness

This is the part that separates the repo from every other RAG repo. Build it second, before reranking, so every later component has a number attached.

**Dataset.** 150–250 question/answer pairs with gold chunk ids. Generate candidates with an LLM over sampled chunks, then hand-verify every one. Commit it as JSONL in the repo. Mix deliberately:

- 40% factoid, answerable from one chunk
- 25% multi-chunk synthesis
- 20% filter-dependent ("in 2024", "by author X") — these are what justify metadata filtering
- 10% structured/aggregate — these must route to SQL
- 5% unanswerable — the system should say so, and faithfulness should catch it when it does not

**Metrics.**

| Metric | What it catches | Reported at |
| --- | --- | --- |
| recall@k | Did the right chunk survive retrieval at all | k = 5, 20, 50 |
| nDCG@10 | Is the right chunk near the top after rerank | top 10 |
| MRR | How deep the first correct hit sits | full set |
| Faithfulness | Is the answer supported by the retrieved context | LLM judge, sampled 100% |
| Answer relevance | Does it answer the question asked | LLM judge |
| Route accuracy | SQL vs vector decision correct | confusion matrix |
| p50 / p95 latency | Per stage and end to end | every run |
| Cost per query | Embedding + rerank + generation | every run |

**Ablation protocol.** Each run is a named config in a YAML file; the runner writes a row to `evals/results.md` committed to the repo. The ablation grid:

1. Vector only (baseline)
2. - BM25 hybrid fusion
3. - metadata filtering
4. - contextual chunking
5. - cross-encoder rerank
6. - SQL routing

Six rows, each with quality and latency. That table *is* the portfolio. An honest negative result — "hybrid fusion bought nothing on this corpus, here is why" — is worth more to a reviewer than six green arrows.

**Discipline.** Fix the seed, fix the model version, pin the embedding model hash. Re-run the whole grid in CI on a 50-question subset so a regression fails the build. Never report a number you cannot reproduce with one command.

## Repo, tooling, observability

Monorepo, two languages, clear seam between them.

```
hybrid-rag/
  README.md                 # the 60-second pitch + results table
  docs/adr/                 # 0001-why-postgres-not-elastic.md, ...
  service/                  # Kotlin + Spring Boot: the query path
    src/main/kotlin/        # routing, retrieval, rerank client, generation
    src/test/kotlin/        # unit + Testcontainers integration
  indexer/                  # Python: parsing, chunking, embedding, backfill
  evals/                    # Python: dataset, runner, configs, results.md
  infra/
    docker-compose.yml      # postgres+pgvector, reranker, otel collector, grafana
    migrations/             # Flyway
  load/                     # k6 scripts
```

**Service layer non-negotiables** — these are the senior-backend tells:

- HikariCP sized deliberately, with the reasoning in a comment
- Resilience4j circuit breaker + bounded retry on the LLM and reranker calls
- Hard timeout per stage, and a degraded path: rerank unavailable means serve fusion-ranked results, never fail the request
- Structured logging with a trace id propagated end to end
- OpenTelemetry spans per stage — `route`, `retrieve.vector`, `retrieve.bm25`, `fuse`, `rerank`, `generate` — exported to a local collector, screenshot of the trace waterfall in the README
- Idempotent ingestion keyed on `external_id` + content hash

**CI.** Build both sides, Testcontainers integration tests against real Postgres with pgvector, the 50-question eval subset with a regression threshold, ktlint and ruff. The eval gate in CI is unusual and reviewers notice it.

**Reranker deployment.** Serve the cross-encoder yourself in a small FastAPI container (bge-reranker-v2-m3 or similar) rather than calling Cohere. Self-hosting is the harder, more interesting choice and it gives you a real latency number to budget against.

## README — the first 60 seconds

A reviewer gives this file one minute. Order it so the strongest evidence is above the fold.

1. **One sentence** on what it is and what corpus it runs on.
2. **The results table**, immediately — the six-row ablation with nDCG@10, recall@20, p95 and cost per query. No preamble.
3. **One architecture diagram**, the drawing from the section above, exported as SVG.
4. **Three findings in three bullets**, each with a number. Example shape: "Cross-encoder reranking raised nDCG@10 from 0.61 to 0.72 for +38 ms p95." Include one negative finding.
5. **Run it** — `docker compose up`, then one command to index and one to query. It must work from a cold clone; test that on a clean machine.
6. **Latency budget table** per stage.
7. **Links to the ADRs**, one line each.

What to leave out: a tutorial on what RAG is, a roadmap of things you did not build, apologies for scope. Link the eval dataset rather than explaining it.

The CV line itself stays one line, with the repo link and the single most defensible number in it.

## ADRs, risks, deferred decisions

Write seven short ADRs, 150 words each, in `docs/adr/`. They are read more often than the code.

1. Postgres + pgvector over Elasticsearch or a dedicated vector DB
2. HNSW over IVFFlat, with build-time and recall numbers
3. Kotlin serving layer, Python offline — why the seam sits where it does
4. Self-hosted cross-encoder over a managed rerank API
5. Contextual chunking strategy and the chunk size actually chosen
6. Pre-filter vs over-fetch-and-post-filter policy
7. Why no agent loop

**Risks, with the mitigation named:**

| Risk | Mitigation |
| --- | --- |
| Eval dataset becomes the real project and swallows weeks | Cap at 150 questions; hand-verify in two sittings, then freeze |
| Scope creeps toward the other six patterns | Non-goals written into the README on day one |
| HNSW recall collapses under selective filters | Measure it deliberately; the measurement is a deliverable, not a blocker |
| LLM judge metrics drift between runs | Pin model version, fix temperature at 0, report judge agreement on a 30-question human-labelled subset |
| Kotlin layer stalls progress early | Phase 1 and 2 are Python-only; the service arrives once retrieval quality is settled |
| Corpus has no usable structured side | Choose the corpus before writing any code — it gates the SQL routing work entirely |

**Deferred on purpose**, and say so in the README: multi-tenancy, incremental re-embedding on model upgrade, streaming responses, query caching, and conversational multi-turn rewriting. Each is a sentence explaining what you would do, which is enough to answer the interview question without building it.
