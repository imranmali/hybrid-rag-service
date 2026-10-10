# Spec Delta: query-routing

## Purpose

Sends structured/aggregate questions to generated SQL over the metadata tables and everything else to retrieval, with guards that make generated SQL safe to run.

## ADDED Requirements

### Requirement: LLM route classification
When routing is enabled the service SHALL ask the LLM to classify the question as `sql` or `vector` and SHALL follow that route; when routing is disabled the route SHALL be `vector` with `route_source` `disabled`.

#### Scenario: Aggregate question routed to SQL
- **WHEN** routing is enabled and the LLM classifies "How many cs.IR papers did Jimmy Lin publish in 2025?" as `sql`
- **THEN** the response has `route` `sql`, `route_source` `llm`, an empty `chunks` list and a populated `sql` object

#### Scenario: Unparseable classification
- **WHEN** the LLM returns text that is not a JSON object with `route` `sql` or `vector`
- **THEN** the request is served on the vector route with `route_source` `fallback` and `degraded` containing `router_unavailable`

### Requirement: Guarded SQL execution
Generated SQL SHALL be executed only if it parses as a single SELECT (optionally with non-modifying CTEs or set operations), references only `documents`, `authors`, `paper_authors` and `paper_categories`, calls no function whose name starts with `pg_`, `lo_` or `dblink` or is `set_config`, and is at most 4000 characters; it SHALL be wrapped so at most 100 rows are returned with a `truncated` flag, and executed on a read-only connection as the `rag_router` role with a 2 second timeout.

#### Scenario: Write statement rejected
- **WHEN** the generated SQL is `delete from documents`
- **THEN** it is not executed, `sql.error` starts with `guard_rejected`, and the request falls back to the vector route with `sql_fallback` in `degraded`

#### Scenario: Stacked statement rejected
- **WHEN** the generated SQL is `select 1; drop table authors`
- **THEN** it is not executed and the request falls back to the vector route

#### Scenario: Disallowed table rejected
- **WHEN** the generated SQL selects from `chunks`
- **THEN** it is rejected by the guard and the request falls back to the vector route

#### Scenario: Row cap
- **WHEN** a valid generated query would return 500 rows
- **THEN** `sql.rows` has 100 rows and `sql.truncated` is true

#### Scenario: Slow query cancelled
- **WHEN** a guard-passing query runs longer than 2 seconds
- **THEN** it is cancelled, `sql.error` starts with `execution_failed`, and the request falls back to the vector route

#### Scenario: Privilege boundary holds without the guard
- **WHEN** a statement selecting `documents.raw_text` is executed on the router connection
- **THEN** the database rejects it with a permission error
