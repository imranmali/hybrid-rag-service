import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from reranker.app import LOGGER, create_app
from reranker.config import Settings
from reranker.scorer import FakeScorer


class CountingScorer(FakeScorer):
    def __init__(self) -> None:
        self.calls = 0

    def score(self, query: str, passages: list[str]) -> list[float]:
        self.calls += 1
        return super().score(query, passages)


class BlockingLoadScorer(CountingScorer):
    def __init__(self) -> None:
        super().__init__()
        self.release = threading.Event()

    def load(self) -> None:
        self.release.wait(timeout=2)


class SlowScorer(CountingScorer):
    def __init__(self) -> None:
        super().__init__()
        self.intervals: list[tuple[float, float]] = []
        self.lock = threading.Lock()

    def score(self, query: str, passages: list[str]) -> list[float]:
        started = time.monotonic()
        time.sleep(0.1)
        result = super().score(query, passages)
        with self.lock:
            self.intervals.append((started, time.monotonic()))
        return result


def settings(**overrides: object) -> Settings:
    return Settings(model="fake", **overrides)


def wait_until_ready(client: TestClient) -> None:
    for _ in range(100):
        if client.get("/healthz").status_code == 200:
            return
        time.sleep(0.01)
    raise AssertionError("model did not become ready")


def request_payload(top_k: int = 2) -> dict[str, object]:
    return {
        "query": "capital of France",
        "passages": [
            {"id": "a", "text": "Paris is the capital of France."},
            {"id": "b", "text": "Bananas are yellow."},
        ],
        "top_k": top_k,
    }


def test_ordering_truncation_and_latency_header() -> None:
    with TestClient(create_app(settings(), CountingScorer())) as client:
        wait_until_ready(client)
        response = client.post("/rerank", json=request_payload(top_k=1))

    body = response.json()
    assert response.status_code == 200
    assert [result["id"] for result in body["results"]] == ["a"]
    assert float(response.headers["X-Rerank-Latency-Ms"]) == body["latency_ms"]


def test_top_k_is_clamped() -> None:
    with TestClient(create_app(settings(), FakeScorer())) as client:
        wait_until_ready(client)
        response = client.post("/rerank", json=request_payload(top_k=20))

    assert len(response.json()["results"]) == 2


def test_validation_returns_422_without_inference() -> None:
    scorer = CountingScorer()
    with TestClient(create_app(settings(), scorer)) as client:
        wait_until_ready(client)
        response = client.post("/rerank", json={"query": "", "passages": []})

    assert response.status_code == 422
    assert scorer.calls == 0


def test_readiness_endpoints_are_unavailable_while_loading() -> None:
    scorer = BlockingLoadScorer()
    with TestClient(create_app(settings(), scorer)) as client:
        assert client.get("/healthz").status_code == 503
        assert client.post("/rerank", json=request_payload()).status_code == 503
        scorer.release.set()
        wait_until_ready(client)


def test_health_body_contains_contract_fields() -> None:
    with TestClient(create_app(settings(), FakeScorer())) as client:
        wait_until_ready(client)
        body = client.get("/healthz").json()

    assert body == {
        "status": "ok",
        "model": "fake",
        "revision": "main",
        "device": "cpu",
        "max_length": 512,
        "ready": True,
    }


def test_concurrent_requests_are_serialized() -> None:
    scorer = SlowScorer()
    with TestClient(create_app(settings(), scorer)) as client:
        wait_until_ready(client)
        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(
                executor.map(lambda _: client.post("/rerank", json=request_payload()), range(2))
            )

    assert all(response.status_code == 200 for response in responses)
    first, second = sorted(scorer.intervals)
    assert first[1] <= second[0]
    assert max(response.json()["latency_ms"] for response in responses) > 100


def test_request_log_has_required_fields() -> None:
    records: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(self.format(record))

    capture = Capture()
    capture.setFormatter(LOGGER.handlers[0].formatter)
    LOGGER.addHandler(capture)
    try:
        with TestClient(create_app(settings(), FakeScorer())) as client:
            wait_until_ready(client)
            client.post("/rerank", json=request_payload())
    finally:
        LOGGER.removeHandler(capture)

    payload = next(
        json.loads(record) for record in records if json.loads(record)["event"] == "rerank"
    )
    assert {"event", "n_passages", "queue_ms", "inference_ms", "model"} <= payload.keys()


class BlockingWarmUpScorer(CountingScorer):
    def __init__(self) -> None:
        super().__init__()
        self.release = threading.Event()
        self.batches: list[tuple[str, int, int]] = []

    def score(self, query: str, passages: list[str]) -> list[float]:
        self.batches.append((query, len(passages), max(len(p.split()) for p in passages)))
        self.release.wait(timeout=2)
        return super().score(query, passages)


def test_ready_flips_only_after_warm_up() -> None:
    records: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    scorer = BlockingWarmUpScorer()
    app_settings = Settings(model="stub/real-model", batch_size=4, max_length=64)
    capture = Capture()
    LOGGER.addHandler(capture)
    try:
        with TestClient(create_app(app_settings, scorer)) as client:
            for _ in range(100):
                if scorer.batches:
                    break
                time.sleep(0.01)
            assert len(scorer.batches) == 1
            assert client.get("/healthz").status_code == 503
            assert client.post("/rerank", json=request_payload()).status_code == 503

            scorer.release.set()
            wait_until_ready(client)
    finally:
        LOGGER.removeHandler(capture)

    assert scorer.batches == [("warm up query", 4, 64)]
    warm_up = next(record for record in records if getattr(record, "event", None) == "warm_up")
    assert warm_up.warm_up_seconds >= 0


def test_fake_model_skips_warm_up() -> None:
    scorer = CountingScorer()
    with TestClient(create_app(settings(), scorer)) as client:
        wait_until_ready(client)

    assert scorer.calls == 0
