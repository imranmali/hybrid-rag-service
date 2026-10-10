from reranker.scorer import FakeScorer


def test_fake_scorer_is_deterministic_and_bounded() -> None:
    scorer = FakeScorer()
    passages = ["Paris is France's capital", "Bananas are yellow"]

    first = scorer.score("capital of France", passages)
    second = scorer.score("capital of France", passages)

    assert first == second
    assert all(0 <= score <= 1 for score in first)


def test_fake_scorer_orders_token_overlap() -> None:
    scorer = FakeScorer()
    scores = scorer.score(
        "what is the capital of France",
        ["Paris is the capital of France.", "Bananas are rich in potassium."],
    )

    assert scores[0] > scores[1]
