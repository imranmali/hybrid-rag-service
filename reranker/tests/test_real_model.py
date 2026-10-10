import pytest

from reranker.scorer import CrossEncoderScorer


@pytest.mark.model
def test_tiny_real_cross_encoder_ranks_paris_above_banana() -> None:
    scorer = CrossEncoderScorer(
        model="cross-encoder/ms-marco-TinyBERT-L-2-v2",
        revision="main",
        max_length=512,
        batch_size=2,
    )
    scorer.load()

    paris, banana = scorer.score(
        "what is the capital of France",
        ["Paris is the capital of France.", "Bananas are rich in potassium."],
    )

    assert 0 <= paris <= 1
    assert 0 <= banana <= 1
    assert paris > banana
