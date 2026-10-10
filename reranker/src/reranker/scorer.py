"""Scoring implementations for production and deterministic test runs."""

import re
from collections.abc import Sequence
from math import exp
from typing import Protocol

import torch
from sentence_transformers import CrossEncoder

_TOKEN_RE = re.compile(r"[a-z0-9]+")


class Scorer(Protocol):
    """Synchronous scorer called from a worker thread by the HTTP layer."""

    @property
    def model(self) -> str: ...

    def load(self) -> None: ...

    def score(self, query: str, passages: Sequence[str]) -> list[float]: ...


class FakeScorer:
    """A dependency-free Jaccard scorer used for tests and image smoke checks."""

    model = "fake"

    def load(self) -> None:
        return None

    def score(self, query: str, passages: Sequence[str]) -> list[float]:
        query_tokens = set(_TOKEN_RE.findall(query.lower()))
        return [
            self._jaccard(query_tokens, set(_TOKEN_RE.findall(passage.lower())))
            for passage in passages
        ]

    @staticmethod
    def _jaccard(left: set[str], right: set[str]) -> float:
        union = left | right
        return len(left & right) / len(union) if union else 0.0


class CrossEncoderScorer:
    """CPU-backed sentence-transformers cross encoder with explicit sigmoid."""

    def __init__(
        self,
        model: str,
        revision: str,
        max_length: int,
        batch_size: int,
        torch_threads: int = 0,
    ) -> None:
        self._model_name = model
        self._revision = revision
        self._max_length = max_length
        self._batch_size = batch_size
        self._torch_threads = torch_threads
        self._encoder: CrossEncoder | None = None

    @property
    def model(self) -> str:
        return self._model_name

    def load(self) -> None:
        if self._torch_threads > 0:
            torch.set_num_threads(self._torch_threads)
        self._encoder = CrossEncoder(
            self._model_name,
            revision=self._revision,
            max_length=self._max_length,
            device="cpu",
            default_activation_function=torch.nn.Identity(),
        )

    def score(self, query: str, passages: Sequence[str]) -> list[float]:
        if self._encoder is None:
            raise RuntimeError("cross-encoder has not loaded")
        pairs = [(query, passage) for passage in passages]
        with torch.inference_mode():
            logits = self._encoder.predict(
                pairs,
                batch_size=self._batch_size,
                show_progress_bar=False,
                convert_to_numpy=True,
                activation_fct=torch.nn.Identity(),
            )
        return [self._sigmoid(float(logit)) for logit in logits]

    @staticmethod
    def _sigmoid(logit: float) -> float:
        if logit >= 0:
            return 1.0 / (1.0 + exp(-logit))
        positive = exp(logit)
        return positive / (1.0 + positive)
