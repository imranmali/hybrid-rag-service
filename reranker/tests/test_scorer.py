import pytest

from reranker import scorer as scorer_module
from reranker.scorer import CrossEncoderScorer, FakeScorer, resolve_device


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


def _availability(monkeypatch: pytest.MonkeyPatch, *, mps: bool, cuda: bool) -> None:
    monkeypatch.setattr(scorer_module.torch.backends.mps, "is_available", lambda: mps)
    monkeypatch.setattr(scorer_module.torch.cuda, "is_available", lambda: cuda)


@pytest.mark.parametrize(
    ("mps", "cuda", "expected"),
    [(True, True, "mps"), (True, False, "mps"), (False, True, "cuda"), (False, False, "cpu")],
)
def test_auto_resolves_mps_then_cuda_then_cpu(
    monkeypatch: pytest.MonkeyPatch, mps: bool, cuda: bool, expected: str
) -> None:
    _availability(monkeypatch, mps=mps, cuda=cuda)

    assert resolve_device("auto") == expected


@pytest.mark.parametrize("requested", ["mps", "cuda", "cpu"])
def test_explicit_device_is_not_probed(monkeypatch: pytest.MonkeyPatch, requested: str) -> None:
    _availability(monkeypatch, mps=False, cuda=False)

    assert resolve_device(requested) == requested


def test_cross_encoder_scorer_reports_resolved_device(monkeypatch: pytest.MonkeyPatch) -> None:
    _availability(monkeypatch, mps=True, cuda=False)

    scorer = CrossEncoderScorer("m", "main", 512, 16, device="auto")

    assert scorer.device == "mps"
