"""Automated LLM answerability precheck (`precheck-v1`, design D3)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from evals.dataset.corpus import Corpus, chunk_lookup
from evals.ollama import LLM, LLMError

PROMPT_VERSION = "precheck-v1"
NUM_PREDICT = 64

ANSWERABLE_SYSTEM = (
    "You check evaluation data. Given PASSAGES, a QUESTION and a proposed ANSWER, decide whether "
    "the passages fully support that answer to that question. "
    'Reply with JSON: {"verdict": "yes" | "no"}.'
)
UNANSWERABLE_SYSTEM = (
    "You check evaluation data. Given PASSAGES retrieved for a QUESTION, decide whether the "
    "passages actually answer the question. "
    'Reply with JSON: {"verdict": "yes" | "no"}.'
)

Retriever = Callable[[str], list[dict[str, Any]]]  # question -> top-5 chunk dicts with `content`


def precheck_candidate(llm: LLM, cand: dict[str, Any], passages: list[str]) -> dict[str, Any]:
    """One precheck entry. `passed` is "the passages answer it" for answerable types and "they do
    not" for unanswerable ones."""
    entry: dict[str, Any] = {
        "candidate_id": cand["candidate_id"],
        "prompt_version": PROMPT_VERSION,
        "model": f"{llm.provider}:{llm.model}",
    }
    joined = "\n\n".join(f"[{i}] {p}" for i, p in enumerate(passages, start=1))
    if cand["type"] == "unanswerable":
        system, user = UNANSWERABLE_SYSTEM, f"QUESTION:\n{cand['question']}\n\nPASSAGES:\n{joined}"
    else:
        user = (
            f"QUESTION:\n{cand['question']}\n\nANSWER:\n{cand['reference_answer']}"
            f"\n\nPASSAGES:\n{joined}"
        )
        system = ANSWERABLE_SYSTEM
    try:
        verdict = str(
            llm.chat(system, user, num_predict=NUM_PREDICT).json().get("verdict", "")
        ).lower()
    except (LLMError, ValueError) as exc:
        return {**entry, "verdict": None, "passed": False, "method": "llm", "error": str(exc)}
    if verdict not in ("yes", "no"):
        return {
            **entry,
            "verdict": verdict or None,
            "passed": False,
            "method": "llm",
            "error": "unparseable verdict",
        }
    passed = verdict == "no" if cand["type"] == "unanswerable" else verdict == "yes"
    return {**entry, "verdict": verdict, "passed": passed, "method": "llm"}


def run_precheck(
    llm: LLM,
    corpus: Corpus,
    candidates: list[dict[str, Any]],
    retriever: Retriever | None,
    done: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Precheck every candidate not in `done`. Aggregates are SQL-computed, so they are recorded
    as passed without an LLM call."""
    done = done or set()
    todo = [c for c in candidates if c["candidate_id"] not in done]
    keys = [(g["external_id"], g["ordinal"]) for c in todo for g in c["gold_chunks"]]
    lookup = chunk_lookup(corpus, keys)
    out = []
    for cand in todo:
        if cand["type"] == "aggregate":
            out.append(
                {
                    "candidate_id": cand["candidate_id"],
                    "prompt_version": "sql-computed",
                    "model": None,
                    "verdict": "yes",
                    "passed": True,
                    "method": "sql",
                }
            )
            continue
        if cand["type"] == "unanswerable":
            if retriever is None:
                raise ValueError("unanswerable precheck needs the service retriever")
            passages = [c["content"] for c in retriever(cand["question"])]
        else:
            passages = [
                lookup[(g["external_id"], g["ordinal"])].content for g in cand["gold_chunks"]
            ]
        out.append(precheck_candidate(llm, cand, passages))
    return out
