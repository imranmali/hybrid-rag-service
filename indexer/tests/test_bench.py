from indexer.bench import recall_at_k


def test_recall_at_10_uses_exact_result_set() -> None:
    assert recall_at_k([1, 2, 3], [3, 4, 1], 3) == 2 / 3
    assert recall_at_k([], [1], 10) == 1.0
