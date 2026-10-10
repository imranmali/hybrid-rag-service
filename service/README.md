# Query service

Kotlin / Spring Boot 3.5 service on JDK 25 (bytecode target 21). It answers questions over the indexed arXiv
papers with metadata-filtered dense and lexical retrieval, Reciprocal Rank Fusion, optional cross-encoder
reranking, guarded LLM-generated SQL for aggregate questions, and grounded answers with citations. Every
component can be switched per request so the eval harness can run its ablation grid.

Contracts live in `openspec/changes/add-platform-foundation/design.md` → "Cross-change contracts" (copied in
`openspec/changes/add-query-service/design.md`). This README explains behaviour and does not redefine them.

## Endpoints

| Endpoint | Contract | Notes |
|---|---|---|
| `POST /v1/query` | C6 `query-api` | Request: `question` + optional `pipeline`; omitted fields take the full-pipeline defaults. Response headers `X-Trace-Id` (always) and `X-RAG-Degraded` (when `degraded` is non-empty). |
| `GET /v1/info` | C6 `query-api` | Version and git sha, model names with live digests, reranker reachability, `index_metadata` read on every call, `embedding_digest_matches_index`, `exact_vector_search`, the default pipeline. |
| `GET /actuator/health` | C6 `query-api` | `UP` while the reader datasource is reachable. Open circuit breakers never make it `DOWN`. |

Status codes: `200` for every request that can be served, degraded or not; `400` for invalid requests (unknown
fields anywhere, blank question, out-of-range values; body names each offending field); `503` when no retrieval is
possible (database unavailable, or embedding down with `bm25` off); `500` for unexpected errors. Every error is
`application/problem+json` with `trace_id`.

## Pipeline

```
route ─┬─ sql ──> sql.generate ─> SqlGuard ─> sql.execute ─> generate (rows)        ─> response
       └─ vector ─> filter.estimate ─┬─ embed ─> retrieve.vector ─┐
                                     └─ retrieve.bm25 ────────────┴─> fuse ─> hydrate ─> rerank ─> generate ─> response
```

The two retrieval legs run in parallel on virtual threads (OTel context and MDC propagate to them). Any failure
on the SQL path falls back to the vector route.

### Switches (`pipeline.*`)

| Switch | Effect when off / changed |
|---|---|
| `retrieval.vector` / `retrieval.bm25` | Skip the dense leg (`embed` + `retrieve.vector`) or the lexical leg. With only one leg there is no `fuse` stage and `scores.fused` is null. At least one must be on. |
| `retrieval.fusion.k` | RRF constant: `score = Σ 1/(k + rank)`, ties by lower `chunk_id`, truncated to `return_k`. |
| `contextual` | `true` uses `embedding_ctx` / `tsv_ctx` and sends `context_header + "\n\n" + content` to the reranker; `false` uses `embedding` / `tsv` and sends `content`. The generation prompt is identical in both. |
| `filters.enabled` | Off: no filters are extracted, estimated or applied, and `filters` is `null`. |
| `filters.extract_from_question` | Off: only `filters.explicit` applies. Explicit fields always win per field. |
| `filters.strategy` | `auto` picks `pre` when matching chunks / total chunks < `selectivity_threshold`, else `post`; `pre` / `post` force a path (below). |
| `rerank.enabled` / `candidates` | The top `candidates` are re-ordered by reranker score; ranks `candidates+1 … return_k` keep fused order with null `rerank`. |
| `routing.enabled` | Off: route is always `vector` with `route_source` `disabled` and no classifier call. |
| `generation.enabled` / `context_k` | Off: no LLM answer call, `answer.status` `disabled`. On: answers from the top `context_k` chunks (or the SQL rows). |

`RAG_RETRIEVAL_EXACT_VECTOR_SEARCH=true` (contract C9) turns off index and bitmap scans in the vector transaction,
so dense retrieval is an exact distance sort with ties by `chunk_id`; used by the CI replay gate.

### Filter pushdown

- `pre`: exact distance sort over the filtered chunk set (materialized CTE).
- `post`: HNSW over-fetch of `vector_k × overfetch_factor` candidates with `hnsw.ef_search` set to that number and
  `hnsw.iterative_scan = off`, then filter, then top `vector_k`. `filters.underfilled` is true when fewer than
  `vector_k` survive.
- No filters: plain ANN with `hnsw.ef_search = max(100, vector_k)` (the pgvector default of 40 would cap results).
- The lexical leg always applies the filters in its `WHERE`. `strategy` is `none` when no filter is applied or the
  vector leg is off; then `estimated_selectivity` and `matching_chunks` are null.

### Filter extraction rules

Rule-based and deterministic (`FilterExtractor`):

| Field | Rule |
|---|---|
| Years (2000–2099) | `between Y1 and Y2` → Jan 1 Y1 – Dec 31 Y2; `in / during / published in Y` → that year; `Y papers / work / studies / articles` (also `Y cs.XX papers`) → that year; `since / from Y` → from Jan 1 Y; `after Y` → from Jan 1 Y+1; `before / prior to Y` → to Dec 31 Y−1 (from/to combine). |
| Categories | Codes `cs.IR`, `cs.CL`, `cs.DB`, `cs.LG`, `cs.AI`, `cs.CV`, `cs.DS`, `cs.HC`, `cs.SE`, `cs.CR`, `cs.NE`, `cs.MA` in any case → `cs.XX`; phrases followed by `paper(s)` or `category`: `information retrieval` → cs.IR, `computation and language` / `computational linguistics` → cs.CL, `database(s)` → cs.DB. A category matches primary or cross-listed. |
| Authors | Every 2–4-token sub-span of a run of capitalized tokens (this covers `by …`, `authored by …`, `co-authored with …`, `from …`, `…'s papers` and a bare name such as "What did Jimmy Lin publish …") is normalized (contract C4) and looked up in one query; only names present in `authors` are applied, longest match first. |

## SQL guard rules

Generated SQL (`sqlgen-v1`, over the tables and columns granted to `rag_router`) runs only if `SqlGuard` accepts it:

- Trim; strip one trailing `;`; reject any other `;` (stacked statements, including inside comments); at most 4 000 characters.
- Must parse (JSqlParser) as a `SELECT`: plain, set operation (`UNION` …) or parenthesized; every `WITH` item must be a `SELECT` (no data-modifying CTEs).
- No `FOR UPDATE / SHARE`, no `SELECT INTO`.
- Tables found by an AST visitor (sub-queries and CTEs included; CTE names excluded; optional `public.` prefix) must be within `documents`, `authors`, `paper_authors`, `paper_categories`. Any other schema is rejected.
- Functions named `pg_*`, `lo_*`, `dblink*`, `query_to_xml*`, `set_config`, `current_setting` are rejected (schema-qualified too).
- Anything the visitor cannot walk is rejected (fail closed). Rejections become `sql.error = "guard_rejected: <reason>"`.

`SqlExecutor` then runs `select * from (<sql>) as q limit 101` on the router pool in a read-only transaction with a
2 s JDBC query timeout (the `rag_router` role also has `statement_timeout = 2s`), returns at most 100 rows with
`truncated`, and maps values to JSON primitives (dates as ISO strings). Errors become
`sql.error = "execution_failed: <SQLState>"`. The role itself cannot read `chunks` or `documents.raw_text`, so the
guard is not the only line of defence.

## Timeouts, retries and circuit breakers

One JDK `HttpClient` (HTTP/1.1, connect timeout 500 ms) with one `RestClient` per call type; the read timeout is the
hard stage timeout. Decorators are `Retry(CircuitBreaker(call))`.

| Call | Timeout | Retry (max 2 attempts) | Breaker |
|---|---:|---|---|
| `embed` (`POST /api/embed`) | 2 000 ms | after 100 ms | `embedding` |
| `rerank` (`POST /rerank`) | 10 000 ms | after 200 ms | `reranker` |
| `route` (`POST /api/chat`, `num_predict` 32, `format: json`) | 5 000 ms | after 250 ms | `llm` |
| `sql.generate` (`num_predict` 256, `format: json`) | 15 000 ms | after 250 ms | `llm` |
| `generate` (`num_predict` 384) | 45 000 ms | after 250 ms | `llm` |
| `filter.estimate` (SQL) | 500 ms statement timeout | — | — |
| `retrieve.vector` / `retrieve.bm25` (SQL) | 1 500 ms statement timeout | — | — |
| `sql.execute` (SQL) | 2 000 ms | — | — |
| `/v1/info`: `GET /api/tags`, `GET /healthz` | 1 000 ms | — | — |

- Retries happen only after a connection failure or HTTP 5xx; never after a read timeout, HTTP 4xx or an open breaker.
- Breakers: count-based window 20, minimum 10 calls, open at 50 % failures, open for 30 s, 3 half-open calls,
  slow-call detection off. Timeouts, 5xx, 4xx (e.g. replay misses) and malformed bodies count as failures.
  Transitions are logged as `breaker_transition` (WARN). All values are `rag.resilience.*` / `rag.timeouts.*` properties.

## Degraded behaviour

Degraded responses are HTTP 200, list their codes in `degraded` and the `X-RAG-Degraded` header, and log a
`dependency_degraded` WARN line.

| Failure | Outcome | Code |
|---|---|---|
| Reranker error, timeout or open breaker | Fused order, null `rerank` scores | `rerank_unavailable` |
| LLM unreachable / timed out / open breaker during generation | Chunks returned; `answer.status` `unavailable`, `answer.http_status` 503 (other LLM errors: `error` / 500) | `llm_unavailable` |
| Route classifier fails or replies with anything but `{"route": "sql"\|"vector"}` | Vector route, `route_source` `fallback` | `router_unavailable` |
| SQL generation fails, guard rejects, or execution fails | Vector route, `route_source` `fallback`, `sql.error` set, `sql.rows` `[]` | `sql_fallback` |
| Embedding fails with `bm25` on | Lexical results only, null vector scores | `embedding_unavailable` |
| Embedding fails with `bm25` off | HTTP 503 problem | — |
| Database error in any retrieval statement, or pool timeout | HTTP 503 problem | — |

## Connection pools

Two explicitly built HikariCP pools, both read-only with `connectionTimeout` 1 000 ms and `maxLifetime` 30 min:

- **reader** (`rag_reader`, 10 connections, min idle 2), used for retrieval and `/v1/info`. Postgres here is a
  4-vCPU container whose throughput peaks around (2 × cores) + 1 ≈ 9 active connections; more only add
  contention. A query holds a connection only for short statements and never across a model call, so DB
  concurrency is about 2 connections per in-flight retrieval, not per in-flight request. The k6 retrieval-only
  scenario needs ~1.2 busy connections by Little's law, which leaves 8× burst headroom. A 1 s connection timeout
  fails fast into a 503 instead of queueing behind model-bound requests.
- **router** (`rag_router`, 3 connections, min idle 1), used only for generated SQL, which is rare and capped at 2 s.

Every DB access runs in a short read-only transaction that ends before any HTTP call. A test holds 12 concurrent
queries on a slow LLM and asserts the reader pool has 0 active connections meanwhile.

## Observability

- Manual spans per stage (`route`, `sql.generate`, `sql.execute`, `filter.estimate`, `embed`, `retrieve.vector`,
  `retrieve.bm25`, `fuse`, `rerank`, `generate`) under the request's server span; failures set ERROR status. The
  OpenTelemetry Java agent in the image adds the server, JDBC and HTTP-client spans and exports over OTLP.
- The agent must support the JVM that runs the service: agent 2.10.0 bundles a Byte Buddy that only reads class files up
  to Java 24, so on JDK 25 every instrumentation (servlet, JDBC, and the OpenTelemetry API bridge that carries the
  manual stage spans) was skipped and no span was exported. The image pins agent 2.31.1; a `--debug` check is
  `OTEL_JAVAAGENT_DEBUG=true` and `grep -c "Java 25 (69) is not supported\|Instrumentation skipped"` must print 0.
- JSON logs (LogstashEncoder) on stdout with `trace_id` and `request_id` on every line, worker threads included.
  Events: `query_completed` (INFO), `dependency_degraded` (WARN), `breaker_transition` (WARN),
  `embedding_digest_mismatch` (ERROR; checked at startup and on each `/v1/info`, never fatal).

## Configuration

`src/main/resources/application.yml` holds the defaults for a local run outside Docker; compose overrides them
with the environment of contract C2 through Spring relaxed binding (`RAG_DB_URL` → `rag.db.url`, and so on).

| Property | Env | Default |
|---|---|---|
| `rag.db.url` | `RAG_DB_URL` | `jdbc:postgresql://localhost:5433/rag` |
| `rag.db.reader.user` / `.password` | `RAG_DB_READER_USER` / `_PASSWORD` | `rag_reader` / `rag_reader_dev` |
| `rag.db.router.user` / `.password` | `RAG_DB_ROUTER_USER` / `_PASSWORD` | `rag_router` / `rag_router_dev` |
| `rag.ollama.url` | `RAG_OLLAMA_URL` | `http://localhost:11434` |
| `rag.reranker.url` | `RAG_RERANKER_URL` | `http://localhost:8081` (compose: `http://host.docker.internal:8081`) |
| `rag.embed.model`, `rag.llm.model`, `rag.llm.seed` | `RAG_EMBED_MODEL`, `RAG_LLM_MODEL`, `RAG_LLM_SEED` | `bge-m3`, `llama3.1:8b`, `42` |
| `rag.llm.provider` | `RAG_LLM_PROVIDER` | `ollama` (`anthropic` requires `ANTHROPIC_API_KEY` and `ANTHROPIC_MODEL`, else startup fails) |
| `rag.retrieval.exact-vector-search` | `RAG_RETRIEVAL_EXACT_VECTOR_SEARCH` | `false` |

## Run locally (outside Docker)

Needs Postgres with the migrations applied (`make up-infra` from the repo root starts postgres, flyway and
tracing), host Ollama with `bge-m3` and `llama3.1:8b`, and optionally the reranker on `localhost:8081`
(`make reranker`). The defaults already point there.

```bash
cd service
./gradlew bootRun
curl -s localhost:8080/v1/info | jq .index
curl -s -X POST localhost:8080/v1/query -H 'content-type: application/json' \
  -d '{"question": "Which cs.IR papers were published in 2024?", "pipeline": {"generation": {"enabled": false}}}' | jq '.chunks[0]'
curl -s -X POST localhost:8080/v1/query -H 'content-type: application/json' \
  -d '{"question":"x","pipeline":{"rerank":{"enable":true}}}'    # 400: unknown field pipeline.rerank.enable
```

Without a reranker, responses carry `rerank_unavailable`; without Ollama, retrieval still works lexically
(`embedding_unavailable`) and answers are `unavailable`.

## Tests

Docker must be running: integration tests start their own `pgvector/pgvector:pg17` container through Testcontainers
(Flyway applies `../infra/migrations`, then a seeded 6-document × 20-chunk fixture) and stub Ollama and the reranker
with WireMock. They never touch the compose stack.

```bash
cd service
./gradlew ktlintCheck test                 # everything (also run by `make lint` / `make test` at the repo root)
./gradlew test --tests '*Pipeline*'        # end-to-end degraded scenarios
./gradlew test --tests '*SqlGuard*'        # guard accept / reject table
./gradlew test --tests '*Outage*' --tests '*PoolHold*'
./gradlew ktlintFormat                     # fix formatting
```

The container image (`Dockerfile`: multi-stage Temurin 25 build, OpenTelemetry Java agent 2.31.1, non-root user)
is built by `make up` through compose.

## Known limitations

- Routing and retrieval run sequentially; speculative parallel retrieval would hide route latency but waste work on
  SQL questions (deferred).
- No response caching, streaming or authentication.
