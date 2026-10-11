# Dataset card: questions.v1

Frozen evaluation set for the Hybrid RAG Service. 150 questions over the indexed
arXiv corpus. sha256 `891fd1f3d3f45d506d44766acc80789f9542ae4aad5854663d5e609e248d67f9`.

## Verification status: human review is pending

Candidates were **LLM-generated** and **agent-verified** against the source chunks. No human has
reviewed them yet; every record has `human_reviewed: false`. Treat the numbers in
`evals/results.md` as measured against an agent-verified, not human-verified, set.

## Mix

| Type | Count | CI subset |
|---|---|---|
| factoid | 60 | 20 |
| multi_chunk | 37 | 12 |
| filter | 30 | 10 |
| aggregate | 15 | 5 |
| unanswerable | 8 | 3 |

## How it was built

1. **Sampling.** Seeded (seed 42) samples of indexed chunks, 2x oversampled:
   factoid from one chunk, multi-chunk from 2-4 chunks of one paper in different sections, filter
   questions from a metadata phrase (year, author or category + year), unanswerable from topic
   seeds only.
2. **Generation.** Generator model `ollama:llama3.1:8b`
   (digest `46e0c10c039e019119339687c3c1757cc81b9da49709a3b3924863ba87ca666e`),
   temperature 0, seed 42, versioned prompts `qgen-*-v1`. Aggregate questions come
   from deterministic templates; their expected answers are computed with SQL, never by the LLM.
3. **Automated precheck** (`precheck-v1`, model `ollama:llama3.1:8b`): it must judge
   that the gold passages fully answer the question with the reference answer (for unanswerable
   questions, that the service's top-5 passages do not).
4. **Agent verification.** The verifying agent(s) `agent:claude-sonnet-5-5`
   read every
   candidate against its gold chunk text, recorded an accept / reject / edit verdict with notes in
   `evals/dataset/work/verification.jsonl`, and confirmed each filter question's expected filters
   against what the service extracts. Only accepted or edited candidates were frozen.
5. **Freeze.** Seeded selection to the quota, ids `q0001`... in type order, `content_sha256`
   copied from the database. Index pipeline fingerprint: `33b6d6becba537c6b8bbe2b22f1d27812f1fdcf64dae662693d0cabcef6583eb`.

## Sampling scope

Candidates were drawn only from documents present in the database at the time of each step (`sample` at 2026-10-10T20:04Z saw 86 documents / 4829 chunks; `generate` at 2026-10-10T20:04Z saw 86 documents / 4829 chunks; `generate` at 2026-10-10T20:26Z saw 92 documents / 5486 chunks; `generate` at 2026-10-10T20:27Z saw 92 documents / 5486 chunks; `generate` at 2026-10-10T20:44Z saw 92 documents / 5486 chunks; `generate` at 2026-10-10T21:05Z saw 92 documents / 5486 chunks; `generate` at 2026-10-10T21:22Z saw 92 documents / 5486 chunks; `generate` at 2026-10-10T21:23Z saw 92 documents / 5486 chunks; `generate` at 2026-10-10T21:23Z saw 92 documents / 5486 chunks; `generate` at 2026-10-10T21:27Z saw 92 documents / 5486 chunks; `generate` at 2026-10-10T21:41Z saw 92 documents / 5486 chunks; `generate` at 2026-10-10T21:55Z saw 92 documents / 5486 chunks; `generate` at 2026-10-10T22:06Z saw 92 documents / 5486 chunks). Indexing may have continued afterwards, so papers added later are not sampled; aggregate (SQL) answers were re-verified against the final index at freeze.

## Known biases and limitations

- The generator, the precheck and the answer judge are all the same model family (llama3.1:8b by
  default), so self-preference bias is possible; judge agreement against human labels is tooled
  (`evals judge-agreement`, `human_labels.template.jsonl`) but pending.
- Questions are written from single chunks or small chunk groups, which favours retrieval of that
  chunk; real user questions are messier.
- Author names are matched by normalized name, so homonymous authors can be conflated.
- Small n per type (8 unanswerable, 15 aggregate): differences of a few points are not significant.

## Regenerating

Gold chunks are keyed by `(external_id, ordinal)` with `content_sha256`, so the dataset survives a
re-index of the same manifest and configuration (the runner checks the hashes). To rebuild from
scratch: `evals dataset sample`, `generate`, `precheck`, `check-filters`, review every candidate,
then `freeze`. A new dataset is a new version file; `questions.v1.jsonl`
is never edited.

## Construction notes and deviations from the plan (read these)

- **Time-boxed sampling.** Candidates were drawn at 1.4x the quota (the plan said 2x) from the
  documents indexed at that moment (86 of the final 92; see "Sampling scope"). Aggregate (SQL)
  candidates were generated against the final 92-document index and their answers were
  recomputed at freeze.
- **Multi-chunk questions are composed, not single-prompt.** A first attempt asked the 8B model
  for one question needing several chunks (`qgen-multi-v1`); none of the first 12 reviewed needed
  two chunks, so all 52 were logged as rejected/superseded. The frozen questions come from
  `qgen-multi-v2`: one single-fact question per chunk (two chunks from different sections of one
  paper), asked together for a named paper, so each part needs a different gold chunk.
- **Filter questions embed their phrase deterministically** (`qgen-filter-v2`): the LLM wrote a
  single-fact question and the metadata phrase (`Among papers published in 2024:`,
  `In a paper by <author>:`, `Among cs.IR papers published in 2025:`) is prepended verbatim,
  because the model dropped the phrase in 86 of 150 attempts (`qgen-filter-v1`). Candidates whose
  expected filters differ from what the service extracts (11: "papers from 2024" is read as
  "since 2024", and initial-bearing author names are not extracted) were rejected unedited.
- **Candidates that failed the automated precheck** were rejected without individual review (the
  notes say so). Candidates beyond the quota that were never reviewed carry no verdict and are not
  frozen.
- **The agent verifier was lenient on vague questions but strict on correctness**: a part that
  could not be seen in the gold chunk, or a garbled LaTeX answer, was rejected or edited (edits keep
  the original text in the notes).
- **Corpus size.** The corpus is 92 papers (the build was cut for time), so the type mix is
  drawn from a small pool; several aggregate answers are 1.
- **Not human-reviewed.** Every record has `human_reviewed: false`.
