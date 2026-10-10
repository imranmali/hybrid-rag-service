# Spec Delta: answer-generation

## Purpose

Produces grounded, cited answers from the top retrieved chunks or from SQL rows, abstains with a fixed sentence when the context is insufficient, and never blocks returning retrieved passages.

## ADDED Requirements

### Requirement: Grounded answer with citations
When generation is enabled on the vector route the service SHALL prompt the LLM with the question and the top `context_k` chunks numbered from 1 (title, section path and content; identical prompt for both contextual variants) and SHALL return the answer text with `citations` listing, in first-mention order without duplicates, the chunk ids of passages cited as `[n]`.

#### Scenario: Citations mapped to chunk ids
- **WHEN** the LLM answers "ColBERT uses late interaction [2][1][2]." with context_k 6
- **THEN** `answer.citations` is `[<chunk id of passage 2>, <chunk id of passage 1>]`

#### Scenario: Out-of-range citation ignored
- **WHEN** the answer cites `[9]` with context_k 6
- **THEN** `[9]` contributes no citation

### Requirement: Abstention
The prompt SHALL instruct the LLM to reply exactly `I cannot answer this from the indexed papers.` when the passages do not contain the answer, and the response SHALL set `answer.abstained` true exactly when the answer text starts with that sentence.

#### Scenario: Abstained answer flagged
- **WHEN** the LLM returns "I cannot answer this from the indexed papers."
- **THEN** `answer.status` is `ok` and `answer.abstained` is true

### Requirement: SQL-route answers
When generation is enabled on the SQL route the service SHALL answer from the question, the executed SQL, and the returned rows; when generation is disabled the rows SHALL still be returned with `answer.status` `disabled`.

#### Scenario: Generation disabled on SQL route
- **WHEN** a question is routed to SQL with generation disabled
- **THEN** `sql.rows` is populated and `answer.status` is `disabled` with `answer.http_status` null

### Requirement: Generation disabled
When generation is disabled the service SHALL make no generation call and return `answer.status` `disabled` with null text and citations.

#### Scenario: Retrieval-only request
- **WHEN** a request disables generation
- **THEN** no answer-generation request reaches the LLM and `answer.status` is `disabled`

### Requirement: Provider selection
The service SHALL use host Ollama `llama3.1:8b` with temperature 0 and seed 42 by default and SHALL use the Anthropic provider only when explicitly configured with an API key and model id, failing at startup if that configuration is incomplete.

#### Scenario: Anthropic misconfigured
- **WHEN** the LLM provider is set to anthropic without an API key
- **THEN** the service fails to start with an error naming the missing setting
