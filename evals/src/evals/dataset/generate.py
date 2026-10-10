"""LLM candidate generation (design D3) plus the deterministic aggregate templates.

Prompts are versioned (`qgen-*-v1`); output is `{"question", "answer"}` JSON at temperature 0 and
seed 42. Questions must be self-contained and filter questions must contain their metadata phrase
verbatim; anything else is dropped and counted."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from evals.dataset.aggregate import generate_aggregates
from evals.dataset.corpus import Chunk, Corpus, chunk_lookup
from evals.dataset.sample import SEED, SampleRow, draw_samples
from evals.ollama import LLM, LLMError

GEN_NUM_PREDICT = 512
MAX_CHUNK_CHARS = 3000

SYSTEM_COMMON = (
    "You write evaluation questions for a retrieval-augmented QA system over arXiv papers. "
    'Reply with JSON: {"question": "...", "answer": "..."}. The question must be '
    "self-contained and understandable without seeing the passage: never say 'this paper', "
    "'the paper', 'the authors', 'the passage' or 'the text'; name the method, dataset or topic "
    "explicitly. The answer must be short (1-3 sentences) and fully supported by the passages."
)
PROMPTS = {
    "factoid": (
        "qgen-factoid-v1",
        SYSTEM_COMMON + " Write ONE factual question whose answer is stated in the passage.",
    ),
    "multi_chunk": (
        "qgen-multi-v1",
        SYSTEM_COMMON
        + " Write ONE question that can only be answered by combining information from EVERY "
        "numbered passage below; a question answerable from a single passage is wrong.",
    ),
    "filter": (
        "qgen-filter-v1",
        SYSTEM_COMMON
        + " Write ONE factual question answered by the passage. The question MUST contain the "
        "exact phrase given as PHRASE, verbatim, in a natural position.",
    ),
    "unanswerable": (
        "qgen-unanswerable-v1",
        "You write evaluation questions for a retrieval-augmented QA system over arXiv papers on "
        'information retrieval, NLP and databases. Reply with JSON: {"question": "...", '
        '"answer": "..."}. Write ONE plausible, specific, self-contained question about the '
        "given TOPIC and ANGLE that the corpus very likely does NOT answer (about a system, "
        "product or result that is not published in such papers). Never say 'this paper'. The "
        "answer field must be exactly: I cannot answer this from the indexed papers.",
    ),
}
_NOT_SELF_CONTAINED = re.compile(
    r"\b(this|the|that) (paper|study|work|article|passage|text|chunk|section|excerpt)\b"
    r"|\bthe authors\b",
    re.IGNORECASE,
)


def phrase_in_question(phrase: str, question: str) -> bool:
    """The filter phrase must appear verbatim (case-insensitive, whitespace-normalised)."""
    norm = lambda s: " ".join(s.lower().split())  # noqa: E731
    return norm(phrase) in norm(question)


def is_self_contained(question: str) -> bool:
    return _NOT_SELF_CONTAINED.search(question) is None


def _user_prompt(sample: SampleRow, chunks: list[Chunk]) -> str:
    if sample.type == "unanswerable":
        t = sample.topic or {}
        return f"TOPIC: {t.get('topic')} ({t.get('category')})\nANGLE: {t.get('angle')}"
    passages = "\n\n".join(
        f"[Passage {i}] (section: {c.section_path})\n{c.content[:MAX_CHUNK_CHARS]}"
        for i, c in enumerate(chunks, start=1)
    )
    if sample.type == "filter" and sample.phrase:
        return f"PHRASE: {sample.phrase['text']}\n\n{passages}"
    return passages


@dataclass
class GenerationStats:
    generated: int = 0
    dropped: dict[str, int] = field(default_factory=dict)

    def drop(self, why: str) -> None:
        self.dropped[why] = self.dropped.get(why, 0) + 1


def _candidate(
    cid: str,
    sample: SampleRow,
    chunks: list[Chunk],
    question: str,
    answer: str,
    generator: str,
    version: str,
) -> dict[str, Any]:
    return {
        "candidate_id": cid,
        "sample_id": sample.sample_id,
        "type": sample.type,
        "question": question.strip(),
        "reference_answer": answer.strip(),
        "gold_chunks": [
            {"external_id": c.external_id, "ordinal": c.ordinal, "content_sha256": c.content_sha256}
            for c in chunks
        ],
        "expected_route": "vector",
        "expected_filters": sample.phrase["expected_filters"] if sample.phrase else None,
        "expected_sql_answer": None,
        "source": {
            "generator": generator,
            "seed": sample.seed,
            "sampled_chunks": [
                {"external_id": c.external_id, "ordinal": c.ordinal} for c in chunks
            ],
        },
        "filter_phrase": sample.phrase["text"] if sample.phrase else None,
        "prompt_version": version,
    }


def generator_name(llm: LLM) -> str:
    return f"{llm.provider}:{llm.model}"


def generate_from_samples(
    llm: LLM,
    corpus: Corpus,
    samples: list[SampleRow],
    *,
    first_index: int = 1,
    stats: GenerationStats | None = None,
) -> list[dict[str, Any]]:
    stats = stats or GenerationStats()
    out: list[dict[str, Any]] = []
    lookup = chunk_lookup(corpus, [k for s in samples for k in s.chunk_keys()])
    for sample in samples:
        version, system = PROMPTS[sample.type]
        chunks = [lookup[k] for k in sample.chunk_keys() if k in lookup]
        if len(chunks) != len(sample.chunks):
            stats.drop("chunk_missing")
            continue
        try:
            result = llm.chat(
                system, _user_prompt(sample, chunks), num_predict=GEN_NUM_PREDICT, seed=SEED
            )
            data = result.json()
        except (LLMError, ValueError):
            stats.drop("llm_error_or_bad_json")
            continue
        question, answer = (
            str(data.get("question", "")).strip(),
            str(data.get("answer", "")).strip(),
        )
        if not question or not answer:
            stats.drop("empty_field")
            continue
        if not is_self_contained(question):
            stats.drop("not_self_contained")
            continue
        if sample.type == "filter" and not phrase_in_question(sample.phrase["text"], question):
            stats.drop("filter_phrase_missing")
            continue
        cid = f"cand-{sample.type}-{first_index + len(out):04d}"
        out.append(_candidate(cid, sample, chunks, question, answer, generator_name(llm), version))
        stats.generated += 1
    return out


def aggregate_candidates(
    corpus: Corpus, *, first_index: int = 1, total: int = 30, seed: int = SEED
) -> list[dict[str, Any]]:
    out = []
    for i, agg in enumerate(generate_aggregates(corpus, total=total, seed=seed), start=first_index):
        d = agg.as_dict()
        out.append(
            {
                "candidate_id": f"cand-aggregate-{i:04d}",
                "sample_id": None,
                "type": "aggregate",
                "question": d["question"],
                "reference_answer": d["expected_sql_answer"],
                "gold_chunks": [],
                "expected_route": "sql",
                "expected_filters": None,
                "expected_sql_answer": d["expected_sql_answer"],
                "source": {
                    "generator": f"sql-template:{d['template']}",
                    "seed": seed,
                    "sampled_chunks": [],
                },
                "filter_phrase": None,
                "sql": d["sql"],
                "prompt_version": f"agg-{d['template'].lower()}-v1",
            }
        )
    return out


def next_index(existing: list[dict[str, Any]], qtype: str) -> int:
    nums = [int(c["candidate_id"].rsplit("-", 1)[1]) for c in existing if c["type"] == qtype]
    return max(nums, default=0) + 1


def generate_type(
    llm: LLM | None,
    corpus: Corpus,
    qtype: str,
    n: int,
    *,
    existing_candidates: list[dict[str, Any]],
    existing_samples: list[SampleRow],
    seed: int = SEED,
    max_rounds: int = 5,
) -> tuple[list[dict[str, Any]], list[SampleRow], GenerationStats]:
    """Up to `n` NEW candidates of one type, ids continuing after `existing_candidates`.

    Unused samples are consumed first; when more are needed (or generation drops some) the type's
    sample stream is extended, so `generate --type T` can be re-run to top up a short type."""
    stats = GenerationStats()
    next_i = next_index(existing_candidates, qtype)
    if qtype == "aggregate":
        # The template draw is deterministic: skip questions that already exist.
        known = {c["question"] for c in existing_candidates if c["type"] == "aggregate"}
        pool = aggregate_candidates(corpus, total=n + len(known), seed=seed)
        fresh = [c for c in pool if c["question"] not in known][:n]
        for i, c in enumerate(fresh, start=next_i):
            c["candidate_id"] = f"cand-aggregate-{i:04d}"
        stats.generated = len(fresh)
        return fresh, existing_samples, stats
    if llm is None:
        raise ValueError("an LLM is required for non-aggregate generation")

    samples = list(existing_samples)
    used = {c.get("sample_id") for c in existing_candidates}
    out: list[dict[str, Any]] = []
    for _ in range(max_rounds):
        unused = [s for s in samples if s.type == qtype and s.sample_id not in used]
        need = n - len(out)
        if need <= 0:
            break
        if len(unused) < need:
            extra = draw_samples(corpus, qtype, need - len(unused), seed=seed, existing=samples)
            if not extra and not unused:
                break  # the corpus has nothing left to sample
            samples += extra
            unused += extra
        batch = unused[:need]
        used |= {s.sample_id for s in batch}
        out += generate_from_samples(llm, corpus, batch, first_index=next_i + len(out), stats=stats)
    return out, samples, stats
