# Design: add-query-service

## Context

See proposal.md — Why. All external contracts (DB schema and roles, env vars, Ollama endpoints, author normalization, reranker API, query API, chosen numbers, replay/exact-search mode) are fixed by `add-platform-foundation/design.md` → "Cross-change contracts" and copied verbatim at the end of this document; this change MUST implement them exactly and MUST NOT redefine them. The compose `service` container (port 8080, env per contract C2, OTLP to the collector) is already defined by the foundation; this change provides `service/Dockerfile` and the code.

Constraints: local JDK 25 must build it (JVM 21 bytecode target); Kotlin is read-only against the DB; every component must be switchable per request for the ablation grid.

## Goals / Non-Goals

**Goals:**
- Contract-exact API; each pipeline switch has a single, testable effect.
- Latency dominated by models, never by our code: DB legs run in parallel, no connection held across model calls.
- Every dependency failure has a defined, tested degraded outcome.
- Deterministic results given identical inputs (tie-breaking, exact mode) so evals and the CI replay gate are reproducible.

**Non-Goals:**
- Speculative parallelism between routing and retrieval (would hide route latency but waste retrieval on SQL questions; deferred, noted in README).
- Response caching, streaming, auth.

## Decisions

### D1. Build and layout
- Gradle wrapper ≥ 9.1 (Kotlin DSL; runs on JDK 25), Kotlin ≥ 2.2.20 (`kotlin("jvm")`, `kotlin("plugin.spring")`), Spring Boot 3.5.x + `io.spring.dependency-management`, `org.jlleitschuh.gradle.ktlint` 12.x. `kotlin { compilerOptions { jvmTarget.set(JvmTarget.JVM_21) } }`, `java { sourceCompatibility = JavaVersion.VERSION_21; targetCompatibility = JavaVersion.VERSION_21 }`; no toolchain download.
- Dependencies: `spring-boot-starter-web`, `spring-boot-starter-jdbc`, `spring-boot-starter-actuator`, `jackson-module-kotlin`, `org.postgresql:postgresql`, `com.pgvector:pgvector`, `io.github.resilience4j:resilience4j-circuitbreaker` + `resilience4j-retry` (2.x, programmatic — no annotations), `io.opentelemetry:opentelemetry-api`, `net.logstash.logback:logstash-logback-encoder` 8.x, `com.github.jsqlparser:jsqlparser` 5.x. Test: `spring-boot-starter-test`, `org.testcontainers:postgresql` + `junit-jupiter`, `org.wiremock:wiremock-standalone` 3.x, `org.flywaydb:flyway-core` + `flyway-database-postgresql`, `io.opentelemetry:opentelemetry-sdk-testing`.
- Package `dev.hybridrag.service`: `api/` (controller, DTOs, validation, problem handler), `pipeline/` (QueryPipeline orchestration, Stage helper, PipelineDefaults), `retrieval/` (VectorRetriever, LexicalRetriever, FilterExtractor, FilterPlanner, RrfFusion, ChunkHydrator), `routing/` (RouteClassifier, SqlGenerator, SqlGuard, SqlExecutor), `rerank/` (RerankerClient), `llm/` (LlmClient interface, OllamaLlmClient, AnthropicLlmClient, EmbeddingClient), `generation/` (AnswerGenerator, prompts), `resilience/` (breakers, retries, dependency call wrapper), `config/` (datasources, http clients, properties), `observability/` (tracer bean, MDC filter, info endpoint, digest check).

### D2. Request flow (QueryPipeline)
1. Validate + resolve defaults (contract C6). Strict Jackson: `FAIL_ON_UNKNOWN_PROPERTIES=true`, `SNAKE_CASE` naming; explicit validator collecting `{field, message}` errors.
2. If `routing.enabled`: stage `route` (LLM classify). If `sql`: `sql.generate` → SqlGuard → `sql.execute` → optional `generate` (SQL answer) → respond. Any failure in the SQL path → vector route with `route_source=fallback` + `sql_fallback` (classifier failure → `router_unavailable`).
3. If `filters.enabled`: stage `filter.estimate` = extraction (D6) + merge with explicit + selectivity estimate + strategy decision.
4. Retrieval legs in parallel on a virtual-thread executor (`Context.taskWrapping` for OTel + MDC copy): leg A = `embed` → `retrieve.vector`; leg B = `retrieve.bm25`.
5. `fuse` (only if both legs ran and returned) → hydrate top `return_k` (one query: chunk + document fields for the ids).
6. `rerank` (if enabled and list non-empty) → merge (D5).
7. `generate` (if enabled) over top `context_k`.
8. Assemble response; log `query_completed`.
`Stage` helper: `stage(name) { … }` opens a span, measures wall time into `timings_ms[name]`, records exceptions on the span, and returns a `Result` so the pipeline decides the degraded outcome.

### D3. Two HikariCP pools
```kotlin
// HikariCP sizing (reader pool = 10):
// - Postgres here is a 4-vCPU container; its throughput peaks around (2 x cores) + 1 ≈ 9 active
//   connections (PostgreSQL wiki "number of connections"); more connections only add contention.
// - A query holds a connection only for short statements (estimate ≤ 500 ms budget, each leg
//   ≤ 1.5 s budget, typically < 20 ms) and never across model calls, so concurrency at the DB is
//   ~2 connections per in-flight retrieval, not per in-flight request.
// - The k6 retrieval-only scenario (~20 rps x ~3 statements x ~20 ms ≈ 1.2 busy connections by
//   Little's law) leaves 8x burst headroom; LLM-bound traffic is throttled upstream by Ollama.
// - connectionTimeout = 1 s: if the pool is exhausted we fail fast into a 503 rather than queue
//   behind a slow model-bound request.
// Router pool = 3: generated SQL is rare (aggregate questions) and capped at 2 s by the role.
```
Both `HikariDataSource`s are built explicitly from `rag.db.*` properties (`readOnly = true`, `maxLifetime = 30 min`, `minimumIdle` 2 / 1, pool names `reader` / `router`). The reader pool is `@Primary` (actuator DB health uses it). Every DB access runs in a short `TransactionTemplate(readOnly)` that ends before any HTTP call.

### D4. Retrieval SQL (column pair chosen from an enum, never interpolated from input)
`col` ∈ {`embedding`, `embedding_ctx`}, `tsv` ∈ {`tsv`, `tsv_ctx`}; `:q` is a `PGvector`. Filter fragment `F` (lines present only for set filters; all values bound):
```sql
and exists (select 1 from paper_categories pc where pc.document_id = d.id and pc.category = any(:categories))
and exists (select 1 from paper_authors pa join authors a on a.id = pa.author_id
            where pa.document_id = d.id and a.name_normalized = any(:authors))
and d.published_on >= :published_from
and d.published_on <= :published_to
```
- Estimate: `select count(*) from chunks c join documents d on d.id = c.document_id where true F`; total = `select count(*) from chunks` cached 60 s. `estimated_selectivity = matching / total`. `set_config('statement_timeout','500',true)`.
- Every retrieval transaction first runs `select set_config('statement_timeout', '1500', true)`.
- Plain ANN (no filters): `set_config('hnsw.ef_search','100',true)`; `select c.id, 1 - (c.<col> <=> :q) as sim from chunks c order by c.<col> <=> :q limit :vector_k` (ORDER BY distance only, so the HNSW index is usable; ties re-sorted by id in Kotlin).
- Pre-filter (exact over the filtered set): `with filtered as materialized (select c.id, c.<col> as emb from chunks c join documents d on d.id = c.document_id where true F) select id, 1 - (emb <=> :q) as sim from filtered order by emb <=> :q, id limit :vector_k`.
- Post-filter (over-fetch): `set_config('hnsw.ef_search', <vector_k × overfetch_factor>, true)`, `set_config('hnsw.iterative_scan','off',true)`; `with ann as materialized (select c.id, c.document_id, c.<col> <=> :q as dist from chunks c order by c.<col> <=> :q limit :fetch) select ann.id, 1 - ann.dist as sim from ann join documents d on d.id = ann.document_id where true F order by ann.dist, ann.id limit :vector_k`; `underfilled = rows < vector_k`.
- Exact mode (`rag.retrieval.exact-vector-search=true`, contract C9): additionally `set_config('enable_indexscan','off',true)` and `set_config('enable_bitmapscan','off',true)` in the vector transaction, and the plain/post queries use `order by <distance>, c.id`.
- Lexical: 
```sql
with q as (select replace(plainto_tsquery('english', :question)::text, '&', '|')::tsquery as tsq)
select c.id, ts_rank_cd(c.<tsv>, q.tsq, 32) as score
from q cross join chunks c join documents d on d.id = c.document_id
where numnode(q.tsq) > 0 and c.<tsv> @@ q.tsq F
order by score desc, c.id
limit :bm25_k
```
  (`plainto_tsquery` ANDs terms; rewriting `&` → `|` gives OR semantics without re-stemming.)
- `strategy = none` when filters are disabled, no filter was applied, or the vector leg is off.

### D5. Fusion and rerank merge
`RrfFusion`: rank maps per leg (1-based), `score = Σ 1/(k + rank)`, sort by (score desc, id asc), truncate `return_k`. Single leg → its order, `fused = null`. Rerank: top `candidates` → `POST /rerank` with `id = chunk_id.toString()`, `text = contextual ? header + "\n\n" + content : content`, `top_k = candidates`; final = reranked (score desc, then fused rank) + fused tail. Reranker failure → fused list unchanged + `rerank_unavailable`.

### D6. Filter extraction (deterministic, `FilterExtractor`)
- Years (2000–2099): `(in|during|published in) YYYY` → that year; `(since|from) YYYY` → from Jan 1; `after YYYY` → from Jan 1 of YYYY+1; `(before|prior to) YYYY` → to Dec 31 of YYYY−1; `between YYYY and YYYY` → range; `YYYY (papers?|work|studies|articles?)` → that year.
- Categories: codes `\bcs\.(IR|CL|DB|LG|AI|CV|DS|HC|SE|CR|NE|MA)\b` (case-insensitive, normalized to `cs.XX`); phrases followed by `papers|paper|category`: `information retrieval` → cs.IR, `computation and language|computational linguistics` → cs.CL, `databases?` → cs.DB.
- Authors: candidate spans of 2–4 capitalized tokens after `by`, `authored by`, `co-authored with`, `from`, or before `'s (paper|papers|work)`; each candidate and its prefixes (longest first) normalized per contract C4 and looked up in one query `select name_normalized from authors where name_normalized = any(:cands)`; only hits are applied.
- Extraction runs only when `filters.enabled && extract_from_question`; explicit fields win per field.

### D7. Routing and SQL guard
- Classifier prompt (`route-v1`, `format: json`, `num_predict` 32): system text defines `sql` = count/list/rank/aggregate over paper metadata (titles, authors, categories, publication dates) not needing paper content; `vector` = anything about what papers say/propose/find/evaluate; 6 few-shot examples; reply `{"route": "sql"|"vector"}`.
- SQL generator prompt (`sqlgen-v1`, `format: json`, `num_predict` 256): the allowed schema (the four tables, `documents` limited to the columns granted to `rag_router`), rules (PostgreSQL, single SELECT, never `*`, authors matched with `authors.name_normalized = '<normalized name>'`, categories via `paper_categories`, dates via `published_on`), 4 few-shot examples; reply `{"sql": "…"}`.
- `SqlGuard`: trim, strip one trailing `;`, reject other `;`, length ≤ 4000; `CCJSqlParserUtil.parse` must yield a `Select` (PlainSelect / SetOperationList / WITH whose CTEs are all selects); no `FOR UPDATE`, no `SELECT INTO`; table names (via `TablesNamesFinder`, minus CTE names, optional `public.` prefix) ⊆ allowlist; function names visited with an expression visitor — reject `pg_*`, `lo_*`, `dblink*`, `set_config`, `current_setting`, `query_to_xml*`. Errors → `guard_rejected: <reason>`.
- `SqlExecutor`: router pool, read-only transaction, `select * from (<sql>) as q limit 101`, JDBC `queryTimeout = 2 s`, return ≤ 100 rows (`truncated` if 101), values as JSON-serializable primitives (dates ISO strings). Errors → `execution_failed: <SQLState>`.

### D8. Generation
- Vector route (`answer-v1`, no `format`, `num_predict` 384): system = `You answer questions about arXiv papers using ONLY the numbered passages. Cite every claim with the passage number in square brackets, e.g. [2]. If the passages do not contain the answer, reply exactly: I cannot answer this from the indexed papers.`; user = `Passages:\n[1] <title> — <section_path>\n<content>\n\n[2] …\n\nQuestion: <question>`.
- SQL route (`sqlanswer-v1`): user = question, SQL, columns, rows as JSON (≤ 100) + `Answer concisely using only these rows.`
- Citations: regex `\[(\d+)\]`, keep 1..context_k, map to chunk ids, dedupe in first-mention order. `abstained = text.trim().startsWith("I cannot answer this from the indexed papers.")`.
- Providers: `OllamaLlmClient` (contract C3 body; `prompt_eval_count`/`eval_count` → usage); `AnthropicLlmClient` only when `rag.llm.provider=anthropic` (`POST https://api.anthropic.com/v1/messages`, headers `x-api-key`, `anthropic-version: 2023-06-01`, `max_tokens = num_predict`, `temperature = 0`; structured outputs requested in the prompt; usage from `usage.input_tokens/output_tokens`); startup fails if `ANTHROPIC_API_KEY` or `ANTHROPIC_MODEL` is blank in that mode.

### D9. Resilience
- HTTP: one JDK `HttpClient` (connect timeout 500 ms, HTTP/1.1) shared; one `RestClient` per call type with `JdkClientHttpRequestFactory.setReadTimeout`: embed 2 s, rerank 10 s, llm-route 5 s, llm-sqlgen 15 s, llm-generate 45 s. Read timeout = the hard stage timeout (no separate TimeLimiter thread).
- Breakers `embedding`, `reranker`, `llm` (shared by route/sqlgen/generate): COUNT_BASED window 20, minimumNumberOfCalls 10, failureRateThreshold 50, waitDurationInOpenState 30 s, permittedNumberOfCallsInHalfOpenState 3, automaticTransitionFromOpenToHalfOpen, slowCallRateThreshold 100 (off). Recorded failures: IOExceptions (incl. timeouts), HTTP 5xx, HTTP 4xx from the dependency (e.g. replay misses), malformed bodies.
- Retry (outermost decorator): maxAttempts 2; waits embed 100 ms, reranker 200 ms, llm 250 ms; `retryOnException` only for `ConnectException`/`HttpConnectTimeoutException` and a `DependencyHttpException` with status ≥ 500; `ignoreExceptions(CallNotPermittedException)`; never on `HttpTimeoutException`.
- Order: `Retry(CircuitBreaker(call))`. All thresholds are `rag.resilience.*` properties so tests can shrink timeouts and windows.
- `management.health.circuitbreakers.enabled=false`; breaker state transitions logged at WARN.

### D10. Observability
- OTel Java agent 2.10.0 in the runtime image (`JAVA_TOOL_OPTIONS=-javaagent:/otel/opentelemetry-javaagent.jar`) provides the server span, JDK HttpClient and JDBC client spans, OTLP export and logback MDC (`trace_id`, `span_id`).
- Manual stage spans via a `Tracer` bean (`GlobalOpenTelemetry.getTracer("dev.hybridrag.service")`); tests replace it with an SDK tracer + `InMemorySpanExporter`. Attributes: `rag.route`, `rag.route_source`, `rag.contextual`, `rag.filter.strategy`, `rag.filter.selectivity`, `rag.candidates`, `rag.rerank.pairs`, `rag.degraded`, `gen_ai.request.model`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`.
- `TraceIdFilter`: `trace_id` = current span's trace id, or a random 32-hex id when no agent is active (tests, IDE runs); put `trace_id` and `request_id` into MDC, set `X-Trace-Id`. Worker tasks copy the MDC map and OTel context.
- `logback-spring.xml`: `LogstashEncoder` to stdout, MDC included. Events: `query_completed` (INFO), `dependency_degraded` (WARN: dependency, reason, breaker state), `breaker_transition` (WARN), `embedding_digest_mismatch` (ERROR).
- Startup digest check (`ApplicationReadyEvent`): `GET /api/tags` (1 s timeout, no breaker) vs `index_metadata.embedding_model_digest`; result cached for `/v1/info` and refreshed on each `/v1/info` call.

### D11. Error mapping
`@RestControllerAdvice` → `application/problem+json` with `trace_id`: validation/unknown field/unreadable body → 400 with `errors[]`; `RetrievalUnavailableException` (DB error in any retrieval statement, pool timeout, or embedding failure with lexical leg disabled) → 503; anything else → 500. Degraded outcomes never throw past the pipeline.

### D12. Configuration (`application.yml`, env overrides per contract C2)
`rag.db.url|reader.user|reader.password|reader.pool-size(10)|router.user|router.password|router.pool-size(3)`, `rag.ollama.url`, `rag.ollama.keep-alive(30m)`, `rag.embed.model(bge-m3)`, `rag.llm.provider(ollama)|model(llama3.1:8b)|seed(42)`, `rag.reranker.url`, `rag.retrieval.exact-vector-search(false)`, `rag.timeouts.*`, `rag.resilience.*`, `rag.version(0.1.0)`, `rag.git-sha(${GIT_SHA:unknown})`, `spring.threads.virtual.enabled=true`, `spring.jackson.property-naming-strategy=SNAKE_CASE`, `spring.jackson.deserialization.fail-on-unknown-properties=true`, `server.port=8080`. Local run outside Docker uses defaults pointing at `localhost:5433`, `localhost:11434`, `localhost:8081`.

### D13. Testing strategy
- Unit: validation and defaults; RRF (ranks, ties, k); rerank merge; FilterExtractor table tests (≥ 25 cases); FilterPlanner thresholds; SqlGuard accept (≥ 8) / reject (≥ 15, incl. stacked statements, comments hiding `;`, data-modifying CTE, `FOR UPDATE`, `SELECT INTO`, `pg_sleep`, `set_config`, `chunks`, `pg_catalog.pg_tables`); citation parsing and abstention; author normalization vectors (contract C4); retry predicate.
- Integration (`@SpringBootTest(webEnvironment = RANDOM_PORT)`): singleton Testcontainers `pgvector/pgvector:pg17`; Flyway (test scope) migrates `filesystem:../infra/migrations` with placeholders as the container superuser; a seeder inserts 6 documents (3 categories × 2 years, shared authors) × 20 chunks with seeded, L2-normalized 1024-d vectors and header-only marker words; the app connects as `rag_reader` / `rag_router`. WireMock (dynamic port) stubs `/api/embed` (returns a chosen chunk's vector), `/api/chat` (by request-body matching on the system prompt) and `/rerank`; test properties shrink timeouts (rerank 500 ms, generate 1 s) and breaker windows.
- Failure-mode matrix (WireMock): reranker 500 / delay / 503-then-200 / open breaker (request count stops increasing); LLM connection refused (stop server) → `answer.http_status` 503; router returns garbage → fallback; embed down with/without lexical leg; DB down (stop container in a dedicated test class) → 503.
- Observability: span names and parent/child via `InMemorySpanExporter`; `OutputCaptureExtension` asserts `query_completed` JSON with matching `trace_id`.

### D14. Dockerfile
Multi-stage: `eclipse-temurin:25-jdk` → `./gradlew bootJar --no-daemon -x test`; runtime `eclipse-temurin:25-jre` + `curl`, agent jar downloaded from the pinned GitHub release `opentelemetry-java-instrumentation v2.10.0`, non-root user, `ARG GIT_SHA=unknown` → `ENV GIT_SHA`, `EXPOSE 8080`, `ENTRYPOINT ["java", "-XX:MaxRAMPercentage=75", "-jar", "/app/app.jar"]`.

### Contracts used (copied verbatim from add-platform-foundation)

<!-- CONTRACT:query-api -->
Base URL: `http://service:8080` (containers) / `http://localhost:8080` (host, `RAG_SERVICE_URL`).

#### `POST /v1/query`

Request (`Content-Type: application/json`). Only `question` is required; every omitted `pipeline` field takes the default shown, and the defaults are the full pipeline (grid row 6). Unknown fields anywhere in the body → `400` (strict parsing, so a typo in an eval config cannot silently disable a component). `explicit: null` is allowed.

```json
{
  "question": "Which rerankers did Jimmy Lin's 2024 cs.IR papers evaluate?",
  "pipeline": {
    "retrieval": {
      "vector": true,
      "bm25": true,
      "vector_k": 50,
      "bm25_k": 50,
      "fusion": {"method": "rrf", "k": 60},
      "return_k": 50
    },
    "contextual": true,
    "filters": {
      "enabled": true,
      "extract_from_question": true,
      "strategy": "auto",
      "selectivity_threshold": 0.25,
      "overfetch_factor": 5,
      "explicit": {
        "categories": ["cs.IR"],
        "authors": ["Jimmy Lin"],
        "published_from": "2024-01-01",
        "published_to": "2024-12-31"
      }
    },
    "rerank": {"enabled": true, "candidates": 20},
    "routing": {"enabled": true},
    "generation": {"enabled": true, "context_k": 6}
  }
}
```

Defaults: as shown, except `filters.explicit` defaults to `null`.

Validation (→ `400`): `question` 1..2000 chars after trim; at least one of `retrieval.vector` / `retrieval.bm25` true; `vector_k`, `bm25_k`, `return_k` ∈ 1..200; `fusion.method` = `"rrf"`; `fusion.k` ∈ 1..1000; `filters.strategy` ∈ {`auto`,`pre`,`post`}; `selectivity_threshold` ∈ [0,1]; `overfetch_factor` ∈ 1..20 and `vector_k × overfetch_factor` ≤ 1000; `explicit.categories[]` match `^[a-z-]+(\.[A-Za-z-]+)?$`; `published_from`/`published_to` ISO dates with from ≤ to; `rerank.candidates` ∈ 1..100 and ≤ `return_k`; `generation.context_k` ∈ 1..20.

Semantics of each switch:
- `retrieval.vector` / `retrieval.bm25`: run the dense leg (`retrieve.vector`, top `vector_k`) and/or the lexical leg (`retrieve.bm25`, top `bm25_k`). If both: Reciprocal Rank Fusion `score = Σ 1/(k + rank)` (ranks 1-based, ties broken by lower `chunk_id`), truncated to `return_k`. If one: that leg's order, truncated to `return_k`; no `fuse` stage.
- `contextual`: `false` → `embedding` + `tsv`, reranker text = `content`; `true` → `embedding_ctx` + `tsv_ctx`, reranker text = `context_header + "\n\n" + content`. The generation prompt is identical in both variants.
- `filters.enabled`: when false, no filters are applied, extracted or estimated. When true, applied filters = explicit filters, plus (if `extract_from_question`) filters extracted from the question for any field not given explicitly. `strategy`: `auto` chooses `pre` when estimated selectivity (matching chunks / total chunks) < `selectivity_threshold`, else `post`; `pre`/`post` force a path. `pre` = exact distance sort over the filtered chunk set; `post` = HNSW over-fetch of `vector_k × overfetch_factor` candidates (with `hnsw.ef_search` = that number), then filter, then top `vector_k`. The lexical leg always applies filters in its `WHERE`.
- `rerank.enabled`: send the top `candidates` of the fused list to the reranker; the final list = those candidates re-ordered by rerank score, followed by fused ranks `candidates+1 … return_k` unchanged.
- `routing.enabled`: when true, an LLM classifies the question as `sql` or `vector`; `sql` runs guarded generated SQL against the structured tables; when false the route is always `vector`.
- `generation.enabled`: when true, the LLM answers from the top `context_k` chunks of the final list (or from SQL rows on the SQL route).

Response `200` (`Content-Type: application/json`; headers `X-Trace-Id: <trace_id>` always, `X-RAG-Degraded: <comma-separated codes>` when `degraded` is non-empty):

```json
{
  "request_id": "6f1c2a0e-3d1b-4c0a-9a55-2f8f0e7b9b11",
  "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
  "route": "vector",
  "route_source": "llm",
  "pipeline": {"…": "fully resolved pipeline, same shape as the request, defaults applied"},
  "filters": {
    "applied": {"categories": ["cs.IR"], "authors": ["jimmy lin"], "published_from": "2024-01-01", "published_to": "2024-12-31"},
    "extracted": {"authors": ["jimmy lin"], "published_from": "2024-01-01", "published_to": "2024-12-31"},
    "strategy": "pre",
    "estimated_selectivity": 0.031,
    "matching_chunks": 187,
    "underfilled": false
  },
  "chunks": [
    {
      "rank": 1,
      "chunk_id": 48213,
      "external_id": "2403.01234",
      "ordinal": 7,
      "title": "…",
      "section_path": "4 Experiments > 4.2 Rerankers",
      "published_on": "2024-03-02",
      "primary_category": "cs.IR",
      "content": "…",
      "context_header": "…",
      "scores": {"vector": 0.8123, "vector_rank": 2, "bm25": 0.4410, "bm25_rank": 1, "fused": 0.03252, "rerank": 0.9731}
    }
  ],
  "sql": null,
  "answer": {
    "status": "ok",
    "http_status": 200,
    "text": "… [1] … [3]",
    "abstained": false,
    "citations": [48213, 48190],
    "error": null
  },
  "degraded": [],
  "timings_ms": {
    "route": 640.2, "filter.estimate": 2.1, "embed": 38.0, "retrieve.vector": 11.4,
    "retrieve.bm25": 6.9, "fuse": 0.3, "rerank": 4210.7, "generate": 8800.1, "total": 13790.4
  },
  "usage": {"embed_prompt_tokens": 14, "rerank_pairs": 20, "llm_prompt_tokens": 2310, "llm_completion_tokens": 182, "llm_calls": 2}
}
```

Field rules:
- `route` ∈ {`vector`,`sql`}; `route_source` ∈ {`llm` (classifier decided), `disabled` (routing off), `fallback` (classifier or SQL path failed → vector)}.
- `filters`: `null` when `filters.enabled` is false. Author names in `applied`/`extracted` are normalized (contract C4). `strategy` ∈ {`none` (no filter applied or no vector leg), `pre`, `post`}; `estimated_selectivity` and `matching_chunks` are `null` when `strategy` = `none`; `underfilled` = true when the post-filter path returned fewer than `vector_k` dense candidates.
- `chunks`: final ranked list (length ≤ `return_k`), `[]` on the SQL route. Score fields are `null` when the stage did not run or the chunk did not appear in that leg. `vector` = cosine similarity (1 − cosine distance); `bm25` = `ts_rank_cd(tsv, query, 32)`; `fused` = RRF score; `rerank` = reranker score. `context_header` is always returned (even when `contextual` = false) for debugging.
- `sql`: `null` on the vector route unless the SQL path was attempted and fell back. Shape: `{"query": "select …", "columns": ["n"], "rows": [[3]], "row_count": 1, "truncated": false, "error": null}`; on fallback `rows` = `[]` and `error` = `"<guard_rejected|execution_failed|generation_failed>: <detail>"`.
- `answer.status` ∈ {`ok`, `disabled`, `unavailable`, `error`}; `answer.http_status` = 200 for `ok`, **503 for `unavailable` (LLM unreachable, breaker open or timed out)**, 500 for `error`, `null` for `disabled`. `text`/`citations` are `null` unless `ok`. `abstained` = true iff the answer starts with the exact sentence `I cannot answer this from the indexed papers.` `citations` = chunk ids referenced as `[n]` in the text.
- `degraded`: subset of {`rerank_unavailable`, `llm_unavailable`, `embedding_unavailable`, `router_unavailable`, `sql_fallback`}.
- `timings_ms`: wall-clock per stage for stages that were attempted (including failed attempts), keys from the fixed set {`route`, `sql.generate`, `sql.execute`, `filter.estimate`, `embed`, `retrieve.vector`, `retrieve.bm25`, `fuse`, `rerank`, `generate`, `total`}. `total` is always present.
- `usage`: token and call counts for cost estimation; zero when a stage did not run.

HTTP status semantics:
- `200` — request served, possibly degraded (rerank down → fused order + `rerank_unavailable`; LLM down → passages returned, `answer.http_status` 503 + `llm_unavailable`; embedding down with bm25 on → lexical only + `embedding_unavailable`; router down → vector route + `router_unavailable`).
- `400` — validation error, `application/problem+json`: `{"type": "about:blank", "title": "Bad Request", "status": 400, "detail": "…", "errors": [{"field": "pipeline.rerank.candidates", "message": "…"}], "trace_id": "…"}`.
- `503` — no retrieval possible (database unavailable; or embedding down with `bm25` = false), `application/problem+json` with `trace_id`.
- `500` — unexpected error, `application/problem+json` with `trace_id`.

#### `GET /v1/info`

```json
{
  "service": {"version": "0.1.0", "git_sha": "abc1234"},
  "models": {
    "embedding": {"name": "bge-m3", "digest": "<64-hex or null if unreachable>"},
    "llm": {"provider": "ollama", "name": "llama3.1:8b", "digest": "<64-hex or null>"},
    "reranker": {"url": "http://reranker:8080", "reachable": true, "model": "BAAI/bge-reranker-v2-m3", "revision": "<sha>"}
  },
  "index": {"<index_metadata key>": "<value>"},
  "embedding_digest_matches_index": true,
  "exact_vector_search": false,
  "defaults": {"…": "default pipeline object"}
}
```
`index` is read from `index_metadata` at request time (not cached at startup).

#### `GET /actuator/health`
Spring Boot health; `UP` when the reader datasource is reachable. Open circuit breakers do NOT make it `DOWN` (degradation is by design).
<!-- /CONTRACT:query-api -->

<!-- CONTRACT:ddl -->
`infra/migrations/V1__schema.sql`:

```sql
create extension if not exists vector;
create extension if not exists pg_trgm;

-- One row per arXiv paper (the version that was ingested).
create table documents (
  id                   uuid primary key,          -- uuid5(NAMESPACE_URL, 'arxiv:' || external_id), computed by the indexer
  external_id          text not null unique,      -- arXiv id WITHOUT version, e.g. '2403.01234'
  arxiv_version        int  not null,             -- ingested version number, e.g. 2
  title                text not null,
  abstract             text not null,
  primary_category     text not null,             -- e.g. 'cs.IR'
  published_on         date not null,             -- arXiv <published> (v1 submission date)
  updated_on           date not null,             -- arXiv <updated>
  source_url           text not null,             -- 'https://arxiv.org/html/' || external_id || 'v' || arxiv_version
  raw_text             text not null,             -- normalized full text, sections joined, references removed
  context_summary      text not null,             -- LLM doc-level situating sentence (<= 50 words)
  content_hash         text not null,             -- sha256 hex, see indexer design
  pipeline_fingerprint text not null,             -- sha256 hex of indexing config, see indexer design
  ingested_at          timestamptz not null default now()
);
create index documents_category_date on documents (primary_category, published_on desc);
create index documents_published_on  on documents (published_on);

create table authors (
  id              bigserial primary key,
  name            text not null,                  -- display form as first seen on arXiv
  name_normalized text not null unique            -- see contract C4 (author-name normalization)
);
create index authors_name_trgm on authors using gin (name_normalized gin_trgm_ops);

create table paper_authors (
  document_id uuid   not null references documents(id) on delete cascade,
  author_id   bigint not null references authors(id),
  position    int    not null,                    -- 1-based author order
  primary key (document_id, position),
  unique (document_id, author_id)
);
create index paper_authors_author on paper_authors (author_id);

create table paper_categories (
  document_id uuid    not null references documents(id) on delete cascade,
  category    text    not null,                   -- e.g. 'cs.IR', 'cs.LG'
  is_primary  boolean not null,
  primary key (document_id, category)
);
create index paper_categories_category on paper_categories (category);

create table chunks (
  id             bigserial primary key,
  document_id    uuid not null references documents(id) on delete cascade,
  ordinal        int  not null,                   -- 0-based within the document; stable for identical text + chunker config
  section_path   text not null,                   -- e.g. '3 Method > 3.2 Index construction'
  content        text not null,                   -- chunk body (no header)
  context_header text not null,                   -- 'Paper: <title> (<primary_category>, <year>). Section: <section_path>. <context_summary>'
  token_count    int  not null,                   -- bge-m3 tokenizer count of content
  content_sha256 text not null,                   -- sha256 hex of content (UTF-8)
  embedding      vector(1024) not null,           -- bge-m3(content)
  embedding_ctx  vector(1024) not null,           -- bge-m3(context_header || E'\n\n' || content)
  tsv            tsvector generated always as (to_tsvector('english', content)) stored,
  tsv_ctx        tsvector generated always as (to_tsvector('english', context_header || ' ' || content)) stored,
  unique (document_id, ordinal)
);
create index chunks_embedding_hnsw     on chunks using hnsw (embedding     vector_cosine_ops) with (m = 16, ef_construction = 64);
create index chunks_embedding_ctx_hnsw on chunks using hnsw (embedding_ctx vector_cosine_ops) with (m = 16, ef_construction = 64);
create index chunks_tsv_gin            on chunks using gin (tsv);
create index chunks_tsv_ctx_gin        on chunks using gin (tsv_ctx);
create index chunks_document           on chunks (document_id);

-- Written by the indexer at the end of every ingest; read by the service (/v1/info) and evals.
create table index_metadata (
  key        text primary key,
  value      text not null,
  updated_at timestamptz not null default now()
);
```

Required `index_metadata` keys (all values text): `embedding_model`, `embedding_model_digest`, `llm_model`, `llm_model_digest`, `chunk_size_tokens`, `chunk_overlap_tokens`, `min_chunk_tokens`, `header_prompt_version`, `parser_version`, `pipeline_fingerprint`, `document_count`, `chunk_count`, `last_ingest_at` (ISO-8601 UTC).

`infra/migrations/V2__roles.sql` (Flyway placeholders `indexer_password`, `reader_password`, `router_password`, supplied as `FLYWAY_PLACEHOLDERS_INDEXER_PASSWORD`, `FLYWAY_PLACEHOLDERS_READER_PASSWORD`, `FLYWAY_PLACEHOLDERS_ROUTER_PASSWORD`):

```sql
do $$
begin
  if not exists (select from pg_roles where rolname = 'rag_indexer') then
    create role rag_indexer login password '${indexer_password}';
  end if;
  if not exists (select from pg_roles where rolname = 'rag_reader') then
    create role rag_reader login password '${reader_password}';
  end if;
  if not exists (select from pg_roles where rolname = 'rag_router') then
    create role rag_router login password '${router_password}';
  end if;
  execute format('grant connect, temporary on database %I to rag_indexer', current_database());
  execute format('grant connect on database %I to rag_reader, rag_router', current_database());
end $$;

grant usage on schema public to rag_indexer, rag_reader, rag_router;

-- indexer: DML on everything, sequences, TEMP (used by index benchmarks), MAINTAIN (ANALYZE)
grant select, insert, update, delete
  on documents, authors, paper_authors, paper_categories, chunks, index_metadata to rag_indexer;
grant usage, select on all sequences in schema public to rag_indexer;
grant maintain
  on documents, authors, paper_authors, paper_categories, chunks, index_metadata to rag_indexer;  -- PG17: lets the indexer ANALYZE

-- service retrieval path: read-only on everything
grant select on documents, authors, paper_authors, paper_categories, chunks, index_metadata to rag_reader;
alter role rag_reader set default_transaction_read_only = on;
alter role rag_reader set statement_timeout = '5s';

-- service generated-SQL path: structured tables only, no chunk text/embeddings, no raw_text
grant select on authors, paper_authors, paper_categories to rag_router;
grant select (id, external_id, arxiv_version, title, abstract, primary_category, published_on, updated_on, source_url)
  on documents to rag_router;
alter role rag_router set default_transaction_read_only = on;
alter role rag_router set statement_timeout = '2s';
alter role rag_router set idle_in_transaction_session_timeout = '5s';
```

Rules: migrations are append-only (new `V3__…` files, never edits). The service connects only as `rag_reader` / `rag_router`; the indexer only as `rag_indexer`; evals (dataset generation, drift checks) only as `rag_reader`.
<!-- /CONTRACT:ddl -->

<!-- CONTRACT:env -->
Compose project: `infra/docker-compose.yml`, invoked as `docker compose --env-file .env -f infra/docker-compose.yml …` (the Makefile does this). Compose project name `hybrid-rag`.

| Service | Image / build | Container port(s) | Host port | Depends on | Health check |
|---|---|---|---|---|---|
| `postgres` | `pgvector/pgvector:pg17` | 5432 | `${POSTGRES_HOST_PORT:-5433}` | — | `pg_isready -U ${POSTGRES_USER} -d ${POSTGRES_DB}` |
| `flyway` | `flyway/flyway:11` (one-shot, `migrate`) | — | — | postgres healthy | exit code 0 |
| `reranker` (opt-in profile `reranker-container`; `RERANKER_DEVICE=cpu`) | build `../reranker` | 8080 | `${RERANKER_HOST_PORT:-8081}` | — | `GET /healthz` = 200, `start_period: 600s` (first model download) |
| `service` | build `../service` | 8080 | `${SERVICE_HOST_PORT:-8080}` | postgres healthy, flyway completed successfully | `GET /actuator/health` = 200, `start_period: 60s` |
| `otel-collector` | `otel/opentelemetry-collector-contrib:0.111.0` | 4317 (gRPC), 4318 (HTTP), 13133 (health) | 4317, 4318 | tempo | `GET :13133/` |
| `tempo` | `grafana/tempo:2.6.1` | 3200 (HTTP), 4317 (OTLP gRPC, internal) | 3200 | — | `GET :3200/ready` |
| `grafana` | `grafana/grafana:11.3.0` | 3000 | `${GRAFANA_HOST_PORT:-3000}` | tempo | `GET /api/health` |
| `wiremock` (replay overlay only) | `wiremock/wiremock:3.9.1` | 8080 | 8089 | — | `GET /__admin/health` |

`service` and `reranker` declare `extra_hosts: ["host.docker.internal:host-gateway"]` (needed on Linux/CI; harmless on Docker Desktop). Volumes: `pgdata` (postgres), `hf-cache` (reranker `HF_HOME=/models`), `tempo-data`.

**Reranker placement.** The default reranker is a host process, `make reranker` (`uv run uvicorn reranker.app:app --port ${RERANKER_HOST_PORT}`, `RERANKER_DEVICE=auto` → `mps` on Apple Silicon, else `cuda`, else `cpu`). Measured on the M4 dev machine with 20 passages of ~384 tokens: MPS p50 6.0 s / p95 6.7 s, the CPU container p50 39–54 s, so the container cannot meet the 10 s rerank timeout (Docker Desktop has no GPU access). The container stays as the opt-in `reranker-container` profile (`make up-reranker-container`) for Linux/CUDA-less hosts, publishing the same host port. Either way the service and WireMock reach it at `http://host.docker.internal:${RERANKER_HOST_PORT}`; `make up` does not start a reranker.

`.env.example` (copied to `.env`; `.env` is git-ignored):

```dotenv
# ---- Postgres ----
POSTGRES_DB=rag
POSTGRES_USER=rag_owner
POSTGRES_PASSWORD=rag_owner_dev
POSTGRES_HOST_PORT=5433
RAG_INDEXER_PASSWORD=rag_indexer_dev
RAG_READER_PASSWORD=rag_reader_dev
RAG_ROUTER_PASSWORD=rag_router_dev

# ---- Host-side DSNs (indexer and evals run on the host via uv) ----
INDEXER_DATABASE_URL=postgresql://rag_indexer:rag_indexer_dev@localhost:5433/rag
EVALS_DATABASE_URL=postgresql://rag_reader:rag_reader_dev@localhost:5433/rag

# ---- Models (host Ollama) ----
OLLAMA_HOST_URL=http://localhost:11434
OLLAMA_CONTAINER_URL=http://host.docker.internal:11434
OLLAMA_EMBED_MODEL=bge-m3
OLLAMA_LLM_MODEL=llama3.1:8b
OLLAMA_KEEP_ALIVE=30m
LLM_SEED=42
# ollama | anthropic  (anthropic is opt-in and requires ANTHROPIC_API_KEY and ANTHROPIC_MODEL)
LLM_PROVIDER=ollama
ANTHROPIC_API_KEY=
ANTHROPIC_MODEL=

# ---- Reranker ----
# Default: run natively on the host with `make reranker` (MPS on Apple Silicon).
# Opt-in CPU container: `make up-reranker-container` (same host port).
RERANKER_DEVICE=auto
RERANKER_CONTAINER_URL=http://host.docker.internal:8081
RERANKER_MODEL=BAAI/bge-reranker-v2-m3
RERANKER_MODEL_REVISION=main
RERANKER_MAX_LENGTH=512
RERANKER_BATCH_SIZE=16
RERANKER_MAX_PASSAGES=100
RERANKER_TORCH_THREADS=0
RERANKER_HOST_PORT=8081

# ---- Query service ----
SERVICE_HOST_PORT=8080
RAG_SERVICE_URL=http://localhost:8080
RAG_RETRIEVAL_EXACT_VECTOR_SEARCH=false

# ---- Observability ----
GRAFANA_HOST_PORT=3000

# ---- arXiv politeness (optional contact for the User-Agent) ----
ARXIV_CONTACT=
```

Environment passed to each container by compose:

- `postgres`: `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`.
- `flyway`: `FLYWAY_URL=jdbc:postgresql://postgres:5432/${POSTGRES_DB}`, `FLYWAY_USER=${POSTGRES_USER}`, `FLYWAY_PASSWORD=${POSTGRES_PASSWORD}`, `FLYWAY_LOCATIONS=filesystem:/flyway/sql` (mount `./migrations:/flyway/sql:ro`), `FLYWAY_PLACEHOLDERS_INDEXER_PASSWORD=${RAG_INDEXER_PASSWORD}`, `FLYWAY_PLACEHOLDERS_READER_PASSWORD=${RAG_READER_PASSWORD}`, `FLYWAY_PLACEHOLDERS_ROUTER_PASSWORD=${RAG_ROUTER_PASSWORD}`, `FLYWAY_CONNECT_RETRIES=10`.
- `reranker` (container profile only): `RERANKER_DEVICE=cpu`, `RERANKER_MODEL`, `RERANKER_MODEL_REVISION`, `RERANKER_MAX_LENGTH`, `RERANKER_BATCH_SIZE`, `RERANKER_MAX_PASSAGES`, `RERANKER_TORCH_THREADS`, `RERANKER_MAX_CONCURRENCY=1`, `PORT=8080`, `HF_HOME=/models`.
- `service` (Spring relaxed binding: `RAG_DB_URL` → `rag.db.url`):
  `RAG_DB_URL=jdbc:postgresql://postgres:5432/${POSTGRES_DB}`,
  `RAG_DB_READER_USER=rag_reader`, `RAG_DB_READER_PASSWORD=${RAG_READER_PASSWORD}`,
  `RAG_DB_ROUTER_USER=rag_router`, `RAG_DB_ROUTER_PASSWORD=${RAG_ROUTER_PASSWORD}`,
  `RAG_OLLAMA_URL=${OLLAMA_CONTAINER_URL}`, `RAG_EMBED_MODEL=${OLLAMA_EMBED_MODEL}`, `RAG_LLM_MODEL=${OLLAMA_LLM_MODEL}`,
  `RAG_LLM_PROVIDER=${LLM_PROVIDER}`, `RAG_LLM_SEED=${LLM_SEED}`, `RAG_OLLAMA_KEEP_ALIVE=${OLLAMA_KEEP_ALIVE}`,
  `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL`,
  `RAG_RERANKER_URL=${RERANKER_CONTAINER_URL:-http://host.docker.internal:8081}`,
  `RAG_RETRIEVAL_EXACT_VECTOR_SEARCH=${RAG_RETRIEVAL_EXACT_VECTOR_SEARCH}`,
  `GIT_SHA` (build arg, default `unknown`),
  `OTEL_SERVICE_NAME=rag-query-service`, `OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318`, `OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf`, `OTEL_TRACES_SAMPLER=always_on`, `OTEL_METRICS_EXPORTER=none`, `OTEL_LOGS_EXPORTER=none`.
- `otel-collector`: config `infra/otel/collector.yaml` — receivers `otlp` (grpc 4317, http 4318); processors `batch`; exporters `otlp/tempo` (`tempo:4317`, insecure) and `debug` (verbosity basic); pipeline `traces` only; extension `health_check` on 13133.
- `tempo`: config `infra/tempo/tempo.yaml` (single binary, local storage under `/var/tempo`, OTLP gRPC receiver).
- `grafana`: `GF_AUTH_ANONYMOUS_ENABLED=true`, `GF_AUTH_ANONYMOUS_ORG_ROLE=Admin`; provisioning `infra/grafana/provisioning/datasources/tempo.yaml` (Tempo at `http://tempo:3200`, default datasource).

Host processes: the reranker (`make reranker`) reads every `RERANKER_*` variable from `.env`; indexer reads `INDEXER_DATABASE_URL`, `OLLAMA_HOST_URL`, model vars, `LLM_*`, `ANTHROPIC_*`, `ARXIV_CONTACT`; evals reads `EVALS_DATABASE_URL`, `RAG_SERVICE_URL`, `OLLAMA_HOST_URL`, model vars, `LLM_*`, `ANTHROPIC_*`. Both load the repo-root `.env` (python-dotenv, path resolved relative to the package directory as `../.env`) and real environment variables override it.
<!-- /CONTRACT:env -->

<!-- CONTRACT:ollama -->
Models: embeddings `bge-m3` (1024 dims, L2-normalized by Ollama), LLM `llama3.1:8b`. Base URL: `OLLAMA_HOST_URL` (host processes) or `OLLAMA_CONTAINER_URL` / `RAG_OLLAMA_URL` (containers).

Embeddings — `POST {base}/api/embed`
```json
{"model": "bge-m3", "input": ["text 1", "text 2"], "truncate": true, "keep_alive": "30m"}
```
→ `200 {"model": "bge-m3", "embeddings": [[1024 floats], [1024 floats]], "prompt_eval_count": 37}`.
Indexer batches ≤ 32 inputs per call; the service always sends exactly one input. No query/document prefix is used with bge-m3.

Chat — `POST {base}/api/chat`
```json
{
  "model": "llama3.1:8b",
  "messages": [{"role": "system", "content": "…"}, {"role": "user", "content": "…"}],
  "stream": false,
  "format": "json",
  "keep_alive": "30m",
  "options": {"temperature": 0, "seed": 42, "num_ctx": 8192, "num_predict": 384}
}
```
→ `200 {"message": {"role": "assistant", "content": "…"}, "prompt_eval_count": 812, "eval_count": 96, "done": true}`.
`format: "json"` is sent only for structured outputs (routing, SQL generation, judge, dataset generation); omitted for free-text answers and context summaries. `num_predict` per use: route 32, SQL generation 256, answer 384, context summary 96, judge 128, dataset generation 512. `temperature` is always 0 and `seed` is always `LLM_SEED` (42).

Model inventory — `GET {base}/api/tags` → `{"models": [{"name": "bge-m3:latest", "digest": "<64-hex>", …}, …]}`. A configured name without a tag matches `<name>:latest`.

Digest pinning — `infra/models.lock.json` (generated by `make models-lock`, committed):
```json
{
  "generated_at": "2026-10-10T00:00:00Z",
  "ollama": {
    "bge-m3": {"digest": "<64-hex from /api/tags>"},
    "llama3.1:8b": {"digest": "<64-hex from /api/tags>"}
  },
  "reranker": {"model": "BAAI/bge-reranker-v2-m3", "revision": "<40-hex Hugging Face commit sha>"}
}
```
Enforcement: the indexer and the eval runner FAIL (exit code 3) if a live digest differs from the lock, unless `--allow-model-drift` is passed (recorded in their output). The service never fails on drift; it logs an ERROR and reports `embedding_digest_matches_index` in `GET /v1/info`.

Anthropic provider (optional, all three Python/Kotlin LLM clients): used only when `LLM_PROVIDER=anthropic`; then `ANTHROPIC_API_KEY` and `ANTHROPIC_MODEL` are required (startup error otherwise). Embeddings always use Ollama `bge-m3`. Nothing ever requires a key.
<!-- /CONTRACT:ollama -->

<!-- CONTRACT:author-norm -->
`normalize_author_name(s)`:
1. Unicode NFKD, then drop combining marks (category `Mn`).
2. Lowercase.
3. Replace every character not in `[a-z0-9 -]` with a space.
4. Collapse runs of whitespace to one space; trim.

Test vectors (must pass in Python and Kotlin):

| input | output |
|---|---|
| `José  Pérez-Gómez` | `jose perez-gomez` |
| `J. R. R. Tolkien` | `j r r tolkien` |
| `Seán O'Neil` | `sean o neil` |
| `ZHANG Wei` | `zhang wei` |
| `  Jimmy   Lin ` | `jimmy lin` |
<!-- /CONTRACT:author-norm -->

<!-- CONTRACT:reranker-api -->
Base URL: `http://host.docker.internal:8081` (from containers) / `http://localhost:8081` (host). The server runs on the host by default (`make reranker`) or as the opt-in container; see C2.

`POST /rerank` — request:
```json
{
  "query": "which rerankers were evaluated on BEIR?",
  "passages": [{"id": "48213", "text": "…"}, {"id": "48190", "text": "…"}],
  "top_k": 20
}
```
- `query`: non-empty string, ≤ 2000 chars. `passages`: 1..`RERANKER_MAX_PASSAGES` (100) items; `id` non-empty string, unique within the request; `text` non-empty string. `top_k`: optional int ≥ 1; default = number of passages; values larger than the passage count are clamped.
- Response `200`:
```json
{
  "model": "BAAI/bge-reranker-v2-m3",
  "results": [{"id": "48190", "index": 1, "score": 0.9731}, {"id": "48213", "index": 0, "score": 0.4410}],
  "latency_ms": 2310.4
}
```
  `results` sorted by `score` descending (ties: lower `index` first), truncated to `top_k`; `index` is the 0-based position in the request; `score` = sigmoid(cross-encoder logit) ∈ [0, 1]; `latency_ms` = queue wait + inference, server-measured. Header `X-Rerank-Latency-Ms` carries the same value.
- Errors: `422` for validation failures (FastAPI default body); `503 {"detail": "model not ready"}` while the model loads; `500` on inference failure.
- Each (query, passage) pair is truncated to `RERANKER_MAX_LENGTH` (512) tokens.

`GET /healthz` → `200 {"status": "ok", "model": "BAAI/bge-reranker-v2-m3", "revision": "<sha or main>", "device": "<resolved device: mps | cuda | cpu>", "max_length": 512, "ready": true}` once loaded; `503` with `"ready": false` before.

`RERANKER_MODEL=fake` selects a deterministic, dependency-free scorer (token-overlap Jaccard) for tests and CI image builds.
<!-- /CONTRACT:reranker-api -->

<!-- CONTRACT:replay -->
Purpose: run the 50-question eval gate on GitHub runners (no Ollama, no GPU) deterministically.

- **Snapshot.** `make snapshot` writes `data/snapshots/rag-index-<first 12 hex of pipeline_fingerprint>.dump` = `pg_dump --data-only --format=custom --exclude-table=flyway_schema_history` of the `rag` DB (run inside the `postgres` container as `rag_owner`) and prints its sha256. `make snapshot-restore SNAPSHOT=<path>` (as `rag_owner`, into a DB that Flyway has already migrated) first runs `truncate documents, authors, paper_authors, paper_categories, chunks, index_metadata restart identity cascade`, then `pg_restore --data-only --disable-triggers --single-transaction`, then `ANALYZE` — so it works on a fresh CI database and on a populated local one alike. `data/snapshots/` is git-ignored; CI downloads the file from a GitHub Release described by `evals/ci/snapshot.lock.json`:
  ```json
  {"release_tag": "index-snapshot-<12hex>", "asset_name": "rag-index-<12hex>.dump", "sha256": "<64-hex>", "pipeline_fingerprint": "<64-hex>", "document_count": 212, "chunk_count": 5480}
  ```
- **Exact search.** With `RAG_RETRIEVAL_EXACT_VECTOR_SEARCH=true` the service disables index scans for vector queries (`SET LOCAL enable_indexscan = off`, `SET LOCAL enable_bitmapscan = off` in the vector-query transaction), so dense retrieval is an exact, deterministic sort (ties by `chunk_id`). HNSW graphs are rebuilt non-deterministically on restore, so ANN would not be replayable.
- **Overlay.** `infra/docker-compose.replay.yml` adds `wiremock` and overrides `service` env: `RAG_OLLAMA_URL=http://wiremock:8080`, `RAG_RERANKER_URL=http://wiremock:8080`, `RAG_RETRIEVAL_EXACT_VECTOR_SEARCH=true`, `OTEL_TRACES_EXPORTER=none`.
  - `WIREMOCK_MODE=record` (default for `make eval-record-ci`): wiremock loads proxy stubs from `infra/wiremock/proxy-mappings/` (`/api/.*` → `http://host.docker.internal:11434`, `/rerank` and `/healthz` → `http://host.docker.internal:8081`) and journals traffic; a reranker must be running on the host port (`make eval-record-ci` checks `/healthz` first).
  - `WIREMOCK_MODE=replay` (`make eval-ci`): wiremock loads only `evals/cassettes/ci/mappings/` (mounted read-only); unmatched requests return 404, which the service treats as a dependency failure (→ a `degraded` code), which the gate counts as a failure.
- **Recording.** `make eval-record-ci` = up with overlay in record mode → `uv run evals grid --subset ci --no-generation --no-judge` → `uv run evals cassettes snapshot` (calls `POST /__admin/recordings/snapshot` with `requestBodyPattern` `equalToJson`, `ignoreArrayOrder: false`, `ignoreExtraElements: false`, dedup identical requests, `persist: false`, writes stub files to `evals/cassettes/ci/mappings/` and `evals/cassettes/ci/meta.json` with recording time, git sha, model digests from `/api/tags`, reranker model/revision, snapshot sha256).
- **Gate.** `make eval-ci` = restore snapshot → up with overlay in replay mode → `uv run evals grid --subset ci --no-generation --no-judge --replay --gate thresholds/ci.yaml`. `--replay` makes the runner take model digests from `evals/cassettes/ci/meta.json` instead of live Ollama. Exit code 0 = pass, 1 = threshold breach, 2 = any degraded response (cassette miss), 3 = setup error.
<!-- /CONTRACT:replay -->

<!-- CONTRACT:numbers -->
| Parameter | Value | Where used |
|---|---|---|
| Chunk size | 384 tokens (bge-m3 tokenizer), section-bounded, sentence-aligned | indexer |
| Chunk overlap | 64 tokens (whole sentences, ≤ 64 tokens) | indexer |
| Min chunk | 64 tokens (shorter section tails merge into the previous chunk) | indexer |
| Context summary | 1 LLM call per document, ≤ 50 words, prompt version `ctx-v1` | indexer |
| Embedding batch | 32 inputs per `/api/embed` call | indexer |
| Dense / lexical candidates | `vector_k` = 50, `bm25_k` = 50 | service |
| RRF k | 60 | service |
| `return_k` | 50 (supports recall@50) | service |
| Rerank candidates | 20 (top 20 of the fused list; CPU latency budget) | service, reranker |
| Generation context | 6 chunks | service |
| Selectivity threshold | 0.25 of chunks; below → pre-filter | service |
| Over-fetch factor | 5 (→ 250 ANN candidates, `hnsw.ef_search` = 250) | service |
| `hnsw.ef_search` without filters | 100 (must be ≥ `vector_k`; pgvector default 40 would cap results at 40) | service |
| HNSW build | m = 16, ef_construction = 64 | schema |
| Stage timeouts | route 5 000 ms · embed 2 000 ms · filter.estimate 500 ms · retrieve.vector 1 500 ms · retrieve.bm25 1 500 ms · rerank 10 000 ms · generate 45 000 ms · sql.generate 15 000 ms · sql.execute 2 000 ms · HTTP connect 500 ms for every dependency | service |
| Retries | embedding: 2 attempts, 100 ms backoff · reranker: 2 attempts, 200 ms · LLM: 2 attempts, 250 ms. All three retry only on connect failure or HTTP 5xx, never on read timeout, HTTP 4xx or an open breaker | service |
| Circuit breakers (`embedding`, `reranker`, `llm`) | count-based window 20, minimum calls 10, failure-rate threshold 50 %, open 30 s, 3 half-open calls; timeouts count as failures; slow-call detection off | service |
| HikariCP | reader pool 10 (min idle 2), router pool 3; connectionTimeout 1 000 ms; maxLifetime 30 min | service |
| SQL guard | single SELECT/WITH, table allowlist, wrapper `LIMIT 101` (return ≤ 100, `truncated` flag), statement timeout 2 s, max 4 000 chars | service |
| LLM options | temperature 0, seed 42, num_ctx 8192 | all LLM callers |
| Reranker | max_length 512, batch 16, max passages 100, max concurrency 1; device `auto` (host, MPS) by default, `cpu` in the container | reranker |
| arXiv politeness | ≥ 3 s between requests; retries on 429/5xx at 5 s, 15 s, 45 s | indexer |
| Corpus | manifest of 220 papers (cs.IR, cs.CL, cs.DB, 2023–2026); `make index` ingests the first 200 | indexer |
| Dataset | 150 = 60 factoid / 37 multi_chunk / 30 filter / 15 aggregate / 8 unanswerable; CI subset 50 = 20/12/10/5/3 | evals |
| CI regression tolerance | baseline − 0.03 for nDCG@10 and recall@20; − 0.05 route accuracy; − 0.10 SQL answer accuracy; 0 degraded responses | evals, CI |
<!-- /CONTRACT:numbers -->

## Risks / Trade-offs

- [HNSW `ef_search` default (40) silently caps results below `vector_k`] → set per transaction (100 / 250); integration test asserts 50 results.
- [Planner ignores the HNSW index for the post-filter CTE] → `materialized` CTE with ORDER BY distance + LIMIT; integration test checks `EXPLAIN` uses `chunks_embedding_hnsw` (skipped in exact mode).
- [Kotlin 2.x / Gradle / Spring Boot on JDK 25 incompatibilities] → versions chosen for JDK 25 support (Gradle ≥ 9.1, Kotlin ≥ 2.2.20, Boot 3.5.x); bytecode target 21.
- [Breaker opening mid-eval contaminates a row] → responses carry `degraded`; the eval runner reports degraded rate per row and the CI gate fails on any.
- [llama3.1:8b mis-routes or writes bad SQL] → guard + role + fallback to vector; route accuracy and SQL answer accuracy are measured, not assumed.
- [Rule-based extraction misses phrasings] → measured by the filter question subset; misses are visible in `filters.extracted`.
- [Virtual threads pinning on JDBC] → JDK 24+ removed synchronized pinning (JEP 491); we run JDK 25.
- [Router and generation share one LLM breaker] → intended: if Ollama is down both are down; a tripped breaker short-circuits both quickly.

## Migration Plan

`make up` builds and starts the container. No data migration (read-only). Rollback: previous image.

## Open Questions

None.
