# Spec Delta: eval-dataset

## Purpose

Produces a frozen, versioned evaluation set of 150 questions with a deliberate type mix, gold chunk identifiers that survive re-indexing, and transparent provenance including how each question was verified.

## ADDED Requirements

### Requirement: Frozen dataset format
The harness SHALL produce `evals/dataset/questions.v1.jsonl` with exactly 150 records matching the dataset record schema in foundation contract C8 (60 factoid, 37 multi_chunk, 30 filter, 15 aggregate, 8 unanswerable), plus `evals/dataset/MANIFEST.json` recording the file's sha256, counts per type, generation seed, generator model and digests.

#### Scenario: Schema and mix validated
- **WHEN** `uv run evals dataset validate` runs on the frozen file
- **THEN** it reports 150 valid records with the exact per-type counts and a sha256 equal to the manifest's

#### Scenario: Gold rules enforced
- **WHEN** a record of type `multi_chunk` has fewer than 2 gold chunks, or an `aggregate` record has no `expected_sql_answer`
- **THEN** validation fails naming the record id

### Requirement: Stable gold identifiers
Gold chunks SHALL be identified by `(external_id, ordinal)` with the chunk's `content_sha256`, never by database row id.

#### Scenario: Re-index keeps gold valid
- **WHEN** the database is rebuilt from the same manifest and indexer configuration
- **THEN** every gold `(external_id, ordinal)` resolves to a chunk whose `content_sha256` matches the dataset

### Requirement: Candidate generation
The harness SHALL generate at least twice the required number of candidates per type from seeded samples of indexed chunks: factoid questions from one chunk, multi-chunk questions from 2–4 chunks of one paper in different sections, filter questions containing an explicit year, author or category phrase taken from the paper's metadata, unanswerable questions about plausible topics absent from the corpus, and aggregate questions from deterministic templates whose expected answers are computed with SQL against the structured tables.

#### Scenario: Aggregate answers computed, not generated
- **WHEN** an aggregate candidate "How many papers did <author> publish in 2025?" is produced
- **THEN** its `expected_sql_answer` equals the result of the corresponding SQL query over `paper_authors`, `authors` and `documents`

#### Scenario: Seeded sampling
- **WHEN** sampling runs twice with seed 42 on the same database
- **THEN** the sampled chunk identifiers are identical

### Requirement: Verification before freezing
Every candidate SHALL pass an automated LLM answerability precheck and then a recorded verification verdict (`accept`, `reject` or `edit`, with verifier identity and notes) in `evals/dataset/work/verification.jsonl` before it can be frozen; filter questions SHALL additionally have their expected filters confirmed against the service's extracted filters; only accepted or edited candidates SHALL be eligible for freezing.

#### Scenario: Unverified candidate excluded
- **WHEN** freezing runs and a candidate has no verdict
- **THEN** it is not included in the frozen dataset

#### Scenario: Filter phrase must be extractable
- **WHEN** a filter candidate's expected filters differ from the filters the service extracts from its question
- **THEN** the candidate is flagged and cannot be accepted until edited

### Requirement: Honest provenance
Each record SHALL state its generator and verification status with `human_reviewed` false unless a human reviewed it, and `evals/dataset/DATASET_CARD.md` SHALL state that candidates were LLM-generated and agent-verified against source chunks and that human review is pending.

#### Scenario: Card discloses verification method
- **WHEN** a reader opens the dataset card
- **THEN** it names the generator model, the verifying agent, the precheck, and states human review is pending

### Requirement: CI subset
Exactly 50 records SHALL be flagged `ci_subset` true, stratified 20/12/10/5/3 by type with seed 42.

#### Scenario: Subset counts
- **WHEN** records with `ci_subset` true are counted by type
- **THEN** the counts are 20 factoid, 12 multi_chunk, 10 filter, 5 aggregate and 3 unanswerable
