from __future__ import annotations

import math
from pathlib import Path

import pytest

from evals.metrics import cost, latency, retrieval, routing

A, B, C, D = ("1", 1), ("2", 2), ("3", 3), ("4", 4)


def _filler(n: int, start: int = 100) -> list[tuple[str, int]]:
    return [(f"x{i}", i) for i in range(start, start + n)]


def test_spec_example_recall_and_rr():
    # gold {A, B}; A at rank 3, B at rank 25
    ranked = [*_filler(2), A, *_filler(21, 200), B, *_filler(10, 300)]
    assert ranked.index(A) == 2 and ranked.index(B) == 24
    m = retrieval.retrieval_metrics([A, B], ranked)
    assert m["recall_at_5"] == 0.5
    assert m["recall_at_20"] == 0.5
    assert m["recall_at_50"] == 1.0
    assert m["mrr"] == pytest.approx(1 / 3)


def test_ndcg_hand_computed():
    # gold {A, B}; A at rank 1, B at rank 3: DCG = 1 + 1/log2(4) = 1.5; IDCG = 1 + 1/log2(3)
    m = retrieval.retrieval_metrics([A, B], [A, C, B, D])
    expected = (1 + 1 / math.log2(4)) / (1 + 1 / math.log2(3))
    assert m["ndcg_at_10"] == pytest.approx(expected)
    assert m["mrr"] == 1.0


def test_ndcg_perfect_and_hit_beyond_ten_ignored():
    assert retrieval.retrieval_metrics([A], [A])["ndcg_at_10"] == 1.0
    ranked = [*_filler(10), A]  # first hit at rank 11
    m = retrieval.retrieval_metrics([A], ranked)
    assert m["ndcg_at_10"] == 0.0
    assert m["recall_at_20"] == 1.0
    assert m["mrr"] == pytest.approx(1 / 11)


def test_ndcg_idcg_capped_at_ten_gold():
    gold = [(f"g{i}", i) for i in range(12)]
    m = retrieval.retrieval_metrics(gold, gold[:10])
    assert m["ndcg_at_10"] == pytest.approx(1.0)


def test_empty_list_scores_zero():
    m = retrieval.retrieval_metrics([A, B], [])
    assert m == {k: 0.0 for k in retrieval.METRIC_KEYS}


def test_no_gold_is_a_programming_error():
    with pytest.raises(ValueError):
        retrieval.retrieval_metrics([], [A])


def test_duplicate_gold_collapses_and_mean_reports_n():
    assert retrieval.retrieval_metrics([A, A], [A])["recall_at_5"] == 1.0
    agg = retrieval.aggregate_retrieval(
        [retrieval.retrieval_metrics([A], [A]), retrieval.retrieval_metrics([A], [])]
    )
    assert agg["recall_at_5"] == {"mean": 0.5, "n": 2}
    assert retrieval.mean_with_n([]) == {"mean": None, "n": 0}
    assert retrieval.mean_with_n([1.0, None, 0.0]) == {"mean": 0.5, "n": 2}


# --- routing ---------------------------------------------------------------------------------


def test_confusion_matrix_example_13_of_15():
    pairs = [("sql", "sql")] * 13 + [("sql", "vector")] * 2 + [("vector", "vector")] * 20
    cm = routing.confusion_matrix(pairs)
    assert cm["sql"] == {"sql": 13, "vector": 2}
    assert cm["vector"] == {"sql": 0, "vector": 20}
    assert routing.route_accuracy(pairs) == pytest.approx(33 / 35)
    assert routing.route_accuracy([]) is None


def _sql_response(rows, route="sql", error=None):
    return {"route": route, "sql": {"columns": ["n"], "rows": rows, "error": error}}


@pytest.mark.parametrize(
    ("rows", "expected", "ok"),
    [
        ([[3]], "3", True),
        ([[3.0]], "3", True),
        ([["3"]], "3", True),
        ([[4]], "3", False),
        ([["Jimmy  Lin"]], "jimmy lin", True),  # case + whitespace normalised
        ([["Jimmy Lin", 7]], "Jimmy Lin", True),  # any cell
        ([["a"], ["b"]], '["B", "a"]', True),  # list: set of first column, case-insensitive
        ([["a"], ["b"]], '["a"]', False),
        ([["a"]], '["a", "b"]', False),
        ([], "3", False),
    ],
)
def test_sql_answer_normalisation(rows, expected, ok):
    assert routing.sql_answer_correct(_sql_response(rows), expected) is ok


def test_sql_answer_requires_sql_route_and_no_error():
    assert not routing.sql_answer_correct(_sql_response([[3]], route="vector"), "3")
    assert not routing.sql_answer_correct(_sql_response([[3]], error="guard_rejected: x"), "3")
    assert not routing.sql_answer_correct({"route": "sql", "sql": None}, "3")
    assert routing.sql_accuracy([True, False, True, True]) == 0.75
    assert routing.sql_accuracy([]) is None


# --- latency ---------------------------------------------------------------------------------


def test_percentile_of_known_list():
    values = [10, 20, 30, 40, 50]
    assert latency.percentile(values, 50) == 30
    assert latency.percentile(values, 95) == pytest.approx(48.0)  # linear interpolation
    assert latency.percentile([], 50) is None
    assert latency.percentile([7], 95) == 7


def test_latency_summary_covers_every_stage_key_present():
    timings = [
        {"embed": 10, "retrieve.vector": 5, "total": 100},
        {"embed": 20, "rerank": 400, "total": 500},
    ]
    s = latency.latency_summary([110, 520], timings)
    assert s["e2e"]["p50"] == pytest.approx(315)
    assert s["server_total"]["n"] == 2
    assert set(s["stages"]) == {"embed", "retrieve.vector", "rerank"}
    assert s["stages"]["embed"]["p50"] == 15
    assert s["stages"]["rerank"]["n"] == 1  # only responses that contain the key


# --- cost ------------------------------------------------------------------------------------

USAGE = {
    "embed_prompt_tokens": 14,
    "rerank_pairs": 20,
    "llm_prompt_tokens": 2310,
    "llm_completion_tokens": 182,
}


def test_zero_price_default_cost_is_zero_but_tokens_are_not():
    pricing = cost.load_pricing(Path(__file__).resolve().parents[1] / "pricing.yaml")
    assert pricing == cost.Pricing()
    s = cost.cost_summary([USAGE, USAGE], pricing)
    assert s["cost_per_query"] == 0.0
    assert s["tokens_per_query"] == 14 + 2310 + 182
    assert s["rerank_pairs_per_query"] == 20


def test_cost_formula():
    p = cost.Pricing(
        embedding_per_1m=0.1, llm_prompt_per_1m=3.0, llm_completion_per_1m=15.0, rerank_per_1k=0.5
    )
    expected = 14 * 0.1 / 1e6 + 2310 * 3.0 / 1e6 + 182 * 15.0 / 1e6 + 20 * 0.5 / 1e3
    assert cost.query_cost(USAGE, p) == pytest.approx(expected)
    assert cost.cost_summary([USAGE, {}], p)["cost_per_query"] == pytest.approx(expected / 2)


def test_pricing_rejects_unknown_keys(tmp_path):
    bad = tmp_path / "p.yaml"
    bad.write_text("llm_per_token: 1\n")
    with pytest.raises(ValueError):
        cost.load_pricing(bad)


def test_empty_usage_list():
    assert cost.cost_summary([], cost.Pricing())["tokens_per_query"] == 0.0
