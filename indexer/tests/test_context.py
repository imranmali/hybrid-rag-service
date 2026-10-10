from datetime import date

import httpx
import respx

from indexer.config import Settings
from indexer.enrich.context import context_header, post_process_summary, summarize_document
from indexer.enrich.llm import LlmClient


@respx.mock
def test_context_request_and_header_contract() -> None:
    settings = Settings(indexer_database_url="postgresql://example", _env_file=None)
    route = respx.post("http://localhost:11434/api/chat").mock(
        return_value=httpx.Response(200, json={"message": {"content": " ".join(["word"] * 60)}})
    )
    client = LlmClient(settings, client=httpx.Client())

    summary = summarize_document(
        client,
        title="A paper",
        categories=("cs.IR",),
        abstract="Abstract",
        section_headings=("1 Intro",),
    )

    assert len(summary.split()) == 50
    request = route.calls[0].request
    assert request.content
    assert b'"temperature":0' in request.content
    assert b'"seed":42' in request.content
    assert b'"num_predict":96' in request.content
    assert b'"format"' not in request.content
    assert context_header("A paper", "cs.IR", date(2024, 1, 2), "1 Intro", summary).startswith(
        "Paper: A paper (cs.IR, 2024). Section: 1 Intro."
    )


def test_summary_keeps_one_sentence() -> None:
    assert post_process_summary('"First sentence. Second sentence."') == "First sentence."
