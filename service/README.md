# Query service

The Kotlin/Spring query path serves metadata-filtered dense and lexical retrieval, optional cross-encoder
reranking, guarded structured SQL, and grounded answer generation. Its public contract is C6 in
`openspec/changes/add-platform-foundation/design.md`.

## Endpoints

| Endpoint | Purpose |
| --- | --- |
| `POST /v1/query` | Resolve the request pipeline, retrieve or run guarded SQL, then optionally generate an answer. |
| `GET /v1/info` | Live model/index metadata, digest comparison, defaults and exact-search setting. |
| `GET /actuator/health` | Reader database health. |

`POST /v1/query` accepts only `question` plus the C6 `pipeline` object. Unknown fields are rejected with
`application/problem+json`; omitted pipeline fields are the full-pipeline defaults.

## Pipeline switches

- `retrieval.vector` and `retrieval.bm25` run dense and PostgreSQL full-text retrieval; both are fused by RRF.
- `contextual` selects `embedding_ctx`/`tsv_ctx`; it only changes reranker input, not generation passages.
- `filters` combines explicit values with deterministic extraction (years, arXiv categories and verified authors).
- `rerank`, `routing`, and `generation` each independently disable their corresponding model stage.
- `RAG_RETRIEVAL_EXACT_VECTOR_SEARCH=true` disables index scans for deterministic replay retrieval.

## SQL safety and degradation

Generated SQL must be one ≤4,000-character SELECT over `documents`, `authors`, `paper_authors`, or
`paper_categories`. The router role has no access to chunks or raw paper text; queries are additionally
wrapped at 101 rows and return at most 100.

| Dependency outcome | Request result |
| --- | --- |
| Reranker unavailable | Return fused order with `rerank_unavailable`. |
| LLM unavailable | Return passages; `answer.http_status` is 503 with `llm_unavailable`. |
| Embedding unavailable + BM25 | Return lexical results with `embedding_unavailable`. |
| Embedding unavailable + no BM25 | HTTP 503 problem response. |
| SQL/router failure | Fall back to vector retrieval with `sql_fallback` or `router_unavailable`. |

| Dependency | Timeout | Retry |
| --- | ---:| --- |
| Embedding | 2 s | once after connect/5xx, 100 ms |
| Reranker | 10 s | once after connect/5xx, 200 ms |
| Route / SQL generation | 5 s / 15 s | once after connect/5xx, 250 ms |
| Answer generation | 45 s | once after connect/5xx, 250 ms |

The reader pool has 10 connections because short database statements on the local four-vCPU PostgreSQL
container need burst headroom but more connections only add contention. The separate three-connection router
pool contains rare generated aggregate queries; neither pool is held while calling a model.

## Run and verify

```bash
cp .env.example .env
make up
curl -sS -X POST localhost:8080/v1/query \
  -H 'content-type: application/json' \
  -d '{"question":"Which cs.IR papers were published in 2024?","pipeline":{"generation":{"enabled":false}}}'
cd service && ./gradlew ktlintCheck test
```

For local service-only development, start Postgres/Flyway with `make up-infra`; the defaults point to
`localhost:5433`, host Ollama at `localhost:11434`, and a reranker at `localhost:8081`.
