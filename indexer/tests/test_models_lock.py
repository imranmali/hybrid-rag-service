import json
from pathlib import Path

import httpx
import pytest
import respx

from indexer.config import Settings
from indexer.models_lock import ModelDriftError, verify_model_lock


@respx.mock
def test_digest_drift_requires_explicit_override(tmp_path: Path) -> None:
    lock = tmp_path / "models.lock.json"
    lock.write_text(
        json.dumps(
            {
                "ollama": {
                    "bge-m3": {"digest": "expected-embed"},
                    "llama3.1:8b": {"digest": "expected-llm"},
                }
            }
        )
    )
    respx.get("http://localhost:11434/api/tags").mock(
        return_value=httpx.Response(
            200,
            json={
                "models": [
                    {"name": "bge-m3:latest", "digest": "actual-embed"},
                    {"name": "llama3.1:8b", "digest": "expected-llm"},
                ]
            },
        )
    )
    settings = Settings(indexer_database_url="postgresql://example", _env_file=None)

    with pytest.raises(ModelDriftError) as error:
        verify_model_lock(settings, client=httpx.Client(), lock_path=lock)
    assert error.value.exit_code == 3
    assert verify_model_lock(
        settings, allow_drift=True, client=httpx.Client(), lock_path=lock
    ).embedding == ("actual-embed")
