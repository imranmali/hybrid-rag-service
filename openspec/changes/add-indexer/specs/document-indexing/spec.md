# Spec Delta: document-indexing

## Purpose

Turns fetched arXiv papers into section-aware chunks with contextual headers and two embedding variants, and writes documents, chunks, authors and categories into the corpus database idempotently.

## ADDED Requirements

### Requirement: Section-aware parsing
The indexer SHALL parse arXiv HTML into an ordered list of sections (abstract first, then body and appendix sections with their heading path), excluding the bibliography, footnotes, navigation and acknowledgements, replacing math with its LaTeX alt text and flattening tables to pipe-separated rows.

#### Scenario: References excluded
- **WHEN** a paper's HTML contains a bibliography section
- **THEN** no chunk content contains text from that section

#### Scenario: Heading path recorded
- **WHEN** a paragraph sits in subsection "3.2 Index construction" of section "3 Method"
- **THEN** its chunk's `section_path` is `3 Method > 3.2 Index construction`

### Requirement: Deterministic chunking
Chunks SHALL be built per section from whole sentences up to 384 tokens (bge-m3 tokenizer), each chunk after the first in a section SHALL begin with trailing sentences of the previous chunk totalling at most 64 tokens, a section tail shorter than 64 tokens SHALL merge into the previous chunk of the same section, a single sentence longer than 384 tokens SHALL be split into 384-token windows with 64-token overlap, and ordinals SHALL be assigned 0..n-1 in document order.

#### Scenario: Size bound
- **WHEN** any document is chunked
- **THEN** every chunk's `token_count` is at most 448 (384 plus a merged tail under 64)

#### Scenario: No cross-section chunks
- **WHEN** two adjacent sections are chunked
- **THEN** no chunk contains text from both sections

#### Scenario: Stable ordinals
- **WHEN** the same document text is chunked twice with the same configuration
- **THEN** the chunks, their ordinals and their `content_sha256` values are identical

### Requirement: Contextual header
For each document the indexer SHALL obtain one situating sentence of at most 50 words from the configured LLM (temperature 0, seed 42), store it in `documents.context_summary`, and set every chunk's `context_header` to `Paper: <title> (<primary_category>, <year>). Section: <section_path>. <context_summary>`.

#### Scenario: One LLM call per document
- **WHEN** a document with 30 chunks is ingested
- **THEN** exactly one chat request is sent to the LLM for its context summary

#### Scenario: Header stored separately
- **WHEN** a chunk is stored
- **THEN** `content` does not contain the header and `context_header` contains the title and section path

### Requirement: Dual embeddings
Each chunk SHALL be stored with `embedding` = bge-m3(content) and `embedding_ctx` = bge-m3(context_header + blank line + content), both 1024-dimensional, obtained from host Ollama in batches of at most 32 inputs.

#### Scenario: Dimension guard
- **WHEN** the embedding endpoint returns vectors whose length is not 1024
- **THEN** ingestion of that document fails with an error naming the model and the received dimension, and nothing is written for it

### Requirement: Idempotent ingestion
Ingestion SHALL be keyed on `external_id`: a document whose `content_hash` and `pipeline_fingerprint` both match the stored row SHALL be skipped without writes or model calls; otherwise its document row, chunks, author links and category links SHALL be replaced in a single transaction.

#### Scenario: Re-run is a no-op
- **WHEN** `indexer ingest --limit 200` runs twice in a row
- **THEN** the second run reports 200 unchanged and 0 inserted or updated, sends no LLM or embedding requests, and table row counts are unchanged

#### Scenario: Changed text replaces chunks atomically
- **WHEN** a document's text changes and it is re-ingested
- **THEN** its old chunks are gone and its new chunks are present in the same committed transaction, and a failure mid-document leaves the previous version intact

#### Scenario: Config change re-ingests
- **WHEN** the chunk size, prompt version, parser version or a model digest changes
- **THEN** every document's `pipeline_fingerprint` differs and all documents are re-ingested

### Requirement: Structured tables populated
The indexer SHALL store every author in `authors` keyed by the normalized name (foundation contract C4), link authors to documents in `paper_authors` with 1-based order, and store every arXiv category in `paper_categories` with the primary category flagged.

#### Scenario: Shared author deduplicated
- **WHEN** two papers list `José Pérez-Gómez` and `Jose Perez-Gomez`
- **THEN** both link to one `authors` row whose `name_normalized` is `jose perez-gomez`

### Requirement: Index metadata recorded
After each ingest run the indexer SHALL upsert every required `index_metadata` key from foundation contract C1 and refresh planner statistics.

#### Scenario: Metadata present
- **WHEN** an ingest run completes
- **THEN** `index_metadata` contains all 13 required keys and `chunk_count` equals `select count(*) from chunks`

### Requirement: Model digest pinning
The indexer SHALL compare the live Ollama digests of the embedding and LLM models with `infra/models.lock.json` before ingesting, and SHALL exit with code 3 on a mismatch unless `--allow-model-drift` is given.

#### Scenario: Drift detected
- **WHEN** the live `bge-m3` digest differs from the lock file
- **THEN** `indexer ingest` exits with code 3 and writes nothing

### Requirement: Command-line interface
The indexer SHALL be runnable as `uv run indexer ingest --limit N` from `indexer/`, SHALL print a final JSON summary line with counts of inserted, updated, unchanged, skipped and failed documents, and SHALL exit 0 when failures are at most 5 % of processed documents.

#### Scenario: Summary printed
- **WHEN** `uv run indexer ingest --limit 5` completes
- **THEN** the last stdout line is a JSON object with keys `inserted`, `updated`, `unchanged`, `skipped`, `failed`
