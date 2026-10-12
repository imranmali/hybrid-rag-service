# Spec Delta: corpus-database

## Purpose

Defines the single Postgres database holding arXiv papers, chunks with embeddings and full-text vectors, and the structured author/category tables, together with the least-privilege roles that separate writing, reading and generated SQL.

## ADDED Requirements

### Requirement: Canonical schema
The database SHALL contain the extensions `vector` and `pg_trgm` and the tables `documents`, `authors`, `paper_authors`, `paper_categories`, `chunks` and `index_metadata` with exactly the columns, constraints and indexes in foundation design contract C1.

#### Scenario: Schema matches the contract
- **WHEN** migrations have run on an empty database
- **THEN** `\d chunks` shows `embedding vector(1024)`, `embedding_ctx vector(1024)`, generated columns `tsv` and `tsv_ctx`, two HNSW indexes and two GIN indexes

### Requirement: Contextual variant stored side by side
Each chunk SHALL store its content-only embedding and its header-plus-content embedding in separate columns, and its content-only and header-plus-content full-text vectors in separate generated columns, so either variant can be queried without re-indexing.

#### Scenario: Header changes only the contextual vectors
- **WHEN** a chunk is inserted with a `context_header` containing a word absent from its `content`
- **THEN** `tsv_ctx` matches that word and `tsv` does not

### Requirement: Stable chunk identity
A chunk SHALL be uniquely identified by `(document_id, ordinal)`, and a document by `external_id` (arXiv id without version).

#### Scenario: Duplicate ordinal rejected
- **WHEN** a second chunk with the same `document_id` and `ordinal` is inserted
- **THEN** the insert fails with a unique-constraint violation

### Requirement: Cascade on document replacement
Deleting a document SHALL delete its chunks, author links and category links.

#### Scenario: Document delete cascades
- **WHEN** a document row is deleted
- **THEN** no rows in `chunks`, `paper_authors` or `paper_categories` reference it afterwards

### Requirement: Least-privilege roles
The database SHALL provide the login roles `rag_indexer` (read-write on all corpus tables), `rag_reader` (read-only on all corpus tables, read-only transactions by default) and `rag_router` (read-only on `authors`, `paper_authors`, `paper_categories` and the non-text columns of `documents`, read-only transactions, 2 s statement timeout), with passwords supplied at migration time rather than stored in migration files.

#### Scenario: Reader cannot write
- **WHEN** `rag_reader` executes `insert into index_metadata(key, value) values ('x', 'y')`
- **THEN** the statement fails

#### Scenario: Router cannot see chunk text or embeddings
- **WHEN** `rag_router` executes `select content from chunks limit 1` or `select raw_text from documents limit 1`
- **THEN** the statement fails with a permission error

#### Scenario: Router statement timeout
- **WHEN** `rag_router` executes a query that runs longer than 2 seconds
- **THEN** the database cancels it with a statement-timeout error

#### Scenario: No passwords in migration files
- **WHEN** the files in `infra/migrations` are searched for the default role passwords from `.env.example`
- **THEN** no match is found
