"""Retrieval metrics (design D7): recall@k, nDCG@10 (binary relevance), reciprocal rank."""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence

Key = tuple[str, int]  # (external_id, ordinal)

METRIC_KEYS = ("recall_at_5", "recall_at_20", "recall_at_50", "ndcg_at_10", "mrr")


def retrieval_metrics(gold: Iterable[Key], ranked: Sequence[Key]) -> dict[str, float]:
    """Metrics for one question. `ranked` is the returned list (best first); an empty list
    (e.g. a mis-routed SQL response) scores zero everywhere."""
    gold_set = set(gold)
    if not gold_set:
        raise ValueError("retrieval_metrics needs at least one gold chunk")

    def recall(k: int) -> float:
        return len(gold_set & set(ranked[:k])) / len(gold_set)

    dcg = sum(1.0 / math.log2(i + 2) for i, key in enumerate(ranked[:10]) if key in gold_set)
    idcg = sum(1.0 / math.log2(i + 2) for i in range(min(len(gold_set), 10)))
    rr = 0.0
    for i, key in enumerate(ranked):
        if key in gold_set:
            rr = 1.0 / (i + 1)
            break
    return {
        "recall_at_5": recall(5),
        "recall_at_20": recall(20),
        "recall_at_50": recall(50),
        "ndcg_at_10": dcg / idcg,
        "mrr": rr,
    }


def mean_with_n(values: Sequence[float | None]) -> dict[str, float | int | None]:
    """Mean over the non-None values plus how many questions the metric covers."""
    present = [v for v in values if v is not None]
    return {"mean": (sum(present) / len(present)) if present else None, "n": len(present)}


def aggregate_retrieval(per_question: Sequence[dict[str, float]]) -> dict[str, dict]:
    return {k: mean_with_n([q[k] for q in per_question]) for k in METRIC_KEYS}
