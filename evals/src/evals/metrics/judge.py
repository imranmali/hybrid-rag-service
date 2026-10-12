"""LLM judge (design D6.5): faithfulness (`faith-v1`) and answer relevance (`rel-v1`).

Temperature 0 and seed 42 come from the LLM client; verdicts map supported/partially/unsupported
and relevant/partially/irrelevant to 1/0.5/0. A malformed judge reply is recorded as
`judge_error` and excluded from the mean (the count of exclusions is reported)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any

from evals.ollama import LLM, LLMError

FAITH_PROMPT_VERSION = "faith-v1"
REL_PROMPT_VERSION = "rel-v1"
JUDGE_NUM_PREDICT = 128

FAITH_SYSTEM = (
    "You are a strict evaluator. Decide whether the ANSWER is fully supported by the CONTEXT. "
    "Use only the context, never your own knowledge. "
    'Reply with JSON: {"verdict": "supported" | "partially_supported" | "unsupported"}. '
    "supported = every claim in the answer is backed by the context; "
    "partially_supported = some claims are backed and some are not; "
    "unsupported = the answer is not backed by the context."
)
REL_SYSTEM = (
    "You are a strict evaluator. Decide whether the ANSWER addresses the QUESTION, "
    "regardless of whether it is correct. "
    'Reply with JSON: {"verdict": "relevant" | "partially_relevant" | "irrelevant"}.'
)

FAITH_SCORES = {"supported": 1.0, "partially_supported": 0.5, "unsupported": 0.0}
REL_SCORES = {"relevant": 1.0, "partially_relevant": 0.5, "irrelevant": 0.0}


@dataclass(frozen=True)
class Verdict:
    label: str | None
    score: float | None
    error: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _judge(llm: LLM, system: str, user: str, scores: dict[str, float]) -> Verdict:
    try:
        result = llm.chat(system, user, num_predict=JUDGE_NUM_PREDICT, json_format=True)
    except LLMError as exc:
        return Verdict(None, None, f"judge_error: {exc}")
    try:
        label = str(result.json().get("verdict", "")).strip().lower()
    except ValueError as exc:
        return Verdict(
            None,
            None,
            f"judge_error: malformed JSON ({exc})",
            result.prompt_tokens,
            result.completion_tokens,
        )
    if label not in scores:
        return Verdict(
            None,
            None,
            f"judge_error: unknown verdict {label!r}",
            result.prompt_tokens,
            result.completion_tokens,
        )
    return Verdict(label, scores[label], None, result.prompt_tokens, result.completion_tokens)


def judge_faithfulness(llm: LLM, question: str, context: str, answer: str) -> Verdict:
    user = f"QUESTION:\n{question}\n\nCONTEXT:\n{context}\n\nANSWER:\n{answer}"
    return _judge(llm, FAITH_SYSTEM, user, FAITH_SCORES)


def judge_relevance(llm: LLM, question: str, answer: str) -> Verdict:
    user = f"QUESTION:\n{question}\n\nANSWER:\n{answer}"
    return _judge(llm, REL_SYSTEM, user, REL_SCORES)


def build_context(response: dict[str, Any], context_k: int) -> str:
    """The exact material the generator saw: SQL rows on the SQL route, else the top `context_k`
    passages (chunk `content`; the generation prompt is identical with and without headers)."""
    sql = response.get("sql")
    if response.get("route") == "sql" and sql and not sql.get("error"):
        header = " | ".join(sql.get("columns", []))
        rows = "\n".join(" | ".join(str(c) for c in row) for row in sql.get("rows", []))
        return f"SQL: {sql.get('query', '')}\n{header}\n{rows}"
    chunks = response.get("chunks", [])[:context_k]
    return "\n\n".join(f"[{i}] {c.get('content', '')}" for i, c in enumerate(chunks, start=1))


def score_answer(
    llm: LLM,
    *,
    qtype: str,
    question: str,
    response: dict[str, Any],
    context_k: int,
) -> dict[str, Any]:
    """Judge one response. Only `answer.status == "ok"` is judged. Abstentions are scored without
    an LLM call: faithfulness 1, and relevance 0 on answerable questions (a false abstention);
    relevance is only ever reported for answerable questions."""
    answer = response.get("answer") or {}
    out: dict[str, Any] = {"faithfulness": None, "relevance": None, "abstained": None}
    if answer.get("status") != "ok":
        return out
    out["abstained"] = bool(answer.get("abstained"))
    answerable = qtype != "unanswerable"
    if out["abstained"]:
        out["faithfulness"] = Verdict("abstained", 1.0).as_dict()
        if answerable:
            out["relevance"] = Verdict("abstained", 0.0).as_dict()
        return out
    text = answer.get("text") or ""
    out["faithfulness"] = judge_faithfulness(
        llm, question, build_context(response, context_k), text
    ).as_dict()
    if answerable:
        out["relevance"] = judge_relevance(llm, question, text).as_dict()
    return out


def aggregate_judgements(
    judgements: Sequence[tuple[str, dict[str, Any]]],
) -> dict[str, Any]:
    """`judgements` = (question type, score_answer output) for every judged question."""

    def stats(key: str, only_answerable: bool) -> dict[str, Any]:
        scores, errors = [], 0
        for qtype, j in judgements:
            if only_answerable and qtype == "unanswerable":
                continue
            v = j.get(key)
            if v is None:
                continue
            if v.get("error"):
                errors += 1
            elif v["score"] is not None:
                scores.append(v["score"])
        return {
            "mean": sum(scores) / len(scores) if scores else None,
            "n": len(scores),
            "judge_errors": errors,
        }

    return {
        "faithfulness": stats("faithfulness", False),
        "answer_relevance": stats("relevance", True),
    }


def abstention_metrics(rows: Sequence[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    """`rows` = (question type, response). Abstention accuracy over unanswerable questions;
    false-abstention rate over answerable ones whose answer status is ok."""
    unans = [r for t, r in rows if t == "unanswerable"]
    ans_ok = [
        r for t, r in rows if t != "unanswerable" and (r.get("answer") or {}).get("status") == "ok"
    ]
    correct = sum(1 for r in unans if (r.get("answer") or {}).get("abstained") is True)
    false_abs = sum(1 for r in ans_ok if (r.get("answer") or {}).get("abstained") is True)
    return {
        "abstention_accuracy": {"value": correct / len(unans) if unans else None, "n": len(unans)},
        "false_abstention_rate": {
            "value": false_abs / len(ans_ok) if ans_ok else None,
            "n": len(ans_ok),
        },
    }
