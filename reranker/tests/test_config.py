import pytest
from pydantic import ValidationError

from reranker.config import Settings


def test_contract_defaults() -> None:
    settings = Settings()

    assert settings.model == "BAAI/bge-reranker-v2-m3"
    assert settings.model_revision == "main"
    assert settings.max_length == 512
    assert settings.batch_size == 16
    assert settings.max_passages == 100
    assert settings.device == "auto"
    assert settings.torch_threads == 0
    assert settings.max_concurrency == 1
    assert settings.port == 8080


def test_environment_overrides(monkeypatch) -> None:
    monkeypatch.setenv("RERANKER_MODEL", "fake")
    monkeypatch.setenv("RERANKER_MAX_CONCURRENCY", "2")
    monkeypatch.setenv("PORT", "9090")

    settings = Settings()

    assert settings.model == "fake"
    assert settings.max_concurrency == 2
    assert settings.port == 9090


def test_device_override_and_validation(monkeypatch) -> None:
    monkeypatch.setenv("RERANKER_DEVICE", "mps")
    assert Settings().device == "mps"

    monkeypatch.setenv("RERANKER_DEVICE", "tpu")
    with pytest.raises(ValidationError):
        Settings()
