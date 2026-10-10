# ADR 0005: Contextual chunking and chunk size

Status: Accepted (2026-10)

## Context
A chunk cut from the middle of a paper often loses what it is about: "the model" or "our method" no longer names anything. Chunk size trades precision against context. Small chunks match narrowly; large chunks dilute the embedding.

## Decision
Chunks are 384 tokens by the bge-m3 tokenizer, bounded by section, aligned to sentences, with 64 tokens of overlap built from whole sentences. Each paper gets one LLM call producing a summary of at most 50 words. The stored `context_header` is that summary plus the section path. It is kept in its own column and embedded and indexed alongside the content as a second embedding and a second tsvector, so one request flag switches contextual retrieval on or off.

## Consequences
Both variants live in the index, which roughly doubles embedding storage but makes the ablation a single switch. Switching contextual headers on took nDCG@10 from {{RESULT: nDCG@10 row 3 | source: evals/results.md}} (row 3) to {{RESULT: nDCG@10 row 4 | source: evals/results.md}} (row 4). Indexing costs one extra LLM call per paper. We did not sweep other chunk sizes.
