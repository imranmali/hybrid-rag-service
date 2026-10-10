import pytest
from pydantic import ValidationError

from reranker.models import RerankRequest


def payload(**overrides):
    body = {
        "query": "capital of France",
        "passages": [{"id": "a", "text": "Paris is the capital."}],
    }
    body.update(overrides)
    return body


@pytest.mark.parametrize("query", ["", " " * 3, "a" * 2001])
def test_query_must_be_non_blank_and_bounded(query: str) -> None:
    with pytest.raises(ValidationError):
        RerankRequest.model_validate(payload(query=query))


@pytest.mark.parametrize("passages", [[], [{"id": str(i), "text": "text"} for i in range(101)]])
def test_passage_count_is_bounded(passages: list[dict[str, str]]) -> None:
    with pytest.raises(ValidationError):
        RerankRequest.model_validate(payload(passages=passages))


def test_duplicate_ids_are_rejected() -> None:
    with pytest.raises(ValidationError, match="unique"):
        RerankRequest.model_validate(
            payload(passages=[{"id": "a", "text": "one"}, {"id": "a", "text": "two"}])
        )


@pytest.mark.parametrize("field", ["id", "text"])
def test_passage_fields_must_be_non_blank(field: str) -> None:
    passage = {"id": "a", "text": "value"}
    passage[field] = " "
    with pytest.raises(ValidationError):
        RerankRequest.model_validate(payload(passages=[passage]))


def test_top_k_defaults_and_clamps() -> None:
    passages = [{"id": "a", "text": "one"}, {"id": "b", "text": "two"}]
    assert RerankRequest.model_validate(payload(passages=passages)).resolved_top_k() == 2
    assert RerankRequest.model_validate(payload(passages=passages, top_k=20)).resolved_top_k() == 2
