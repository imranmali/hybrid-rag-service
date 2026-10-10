# Spec Delta: corpus-acquisition

## Purpose

Selects a reproducible set of arXiv papers with full-text HTML and fetches their metadata and HTML politely, caching everything on disk so re-runs never re-download.

## ADDED Requirements

### Requirement: Reproducible paper manifest
The indexer SHALL provide `indexer select` that writes `indexer/corpus/manifest.jsonl` listing 220 papers whose primary category is cs.IR, cs.CL or cs.DB, submitted 2023–2026, each with `external_id`, `version`, `primary_category`, `published_on` and `selection_reason`, chosen deterministically from a fixed seed.

#### Scenario: Same inputs give the same manifest
- **WHEN** `indexer select --seed 42` runs twice against the same cached API responses
- **THEN** both runs produce byte-identical manifests

#### Scenario: Only papers with usable HTML
- **WHEN** a candidate paper has no arXiv HTML page or its parsed text is shorter than 2000 tokens
- **THEN** it is excluded and replaced by another candidate from the same category and year where available

#### Scenario: Prolific authors included
- **WHEN** the candidate pool contains authors with three or more papers
- **THEN** the manifest includes up to five papers for each of the eight most frequent such authors, listed before the stratified sample

#### Scenario: Balanced prefixes
- **WHEN** `ingest --limit N` uses the first N manifest entries
- **THEN** after the prolific-author entries, entries alternate across (category, year) strata so any prefix stays balanced

### Requirement: Polite rate limiting
Every network request to export.arxiv.org or arxiv.org SHALL be spaced at least 3 seconds after the previous one, SHALL send a descriptive User-Agent, and SHALL retry HTTP 429 and 5xx responses up to three times with waits of 5, 15 and 45 seconds (or the `Retry-After` value when larger).

#### Scenario: Requests spaced
- **WHEN** three uncached documents are fetched in a row
- **THEN** the gap between consecutive network requests is at least 3 seconds

#### Scenario: Throttled response retried
- **WHEN** arXiv answers 429 and then 200
- **THEN** the fetch succeeds after one retry and the wait is at least 5 seconds

### Requirement: On-disk cache
API responses and HTML pages SHALL be cached under `data/cache/arxiv/` keyed by query and by `<external_id>v<version>`, including negative entries for missing HTML, and cached entries SHALL be served without network access.

#### Scenario: Second run offline
- **WHEN** `indexer fetch` runs a second time with networking disabled
- **THEN** it completes using only the cache and reports zero network requests

### Requirement: Pinned versions
Fetching and ingestion SHALL use the paper version recorded in the manifest even if arXiv reports a newer version.

#### Scenario: Newer version published
- **WHEN** the API reports version 3 for a paper the manifest lists as version 2
- **THEN** the HTML for version 2 is fetched and `documents.arxiv_version` is 2
