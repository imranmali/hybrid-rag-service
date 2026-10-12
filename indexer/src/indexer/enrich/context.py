"""Doc-level contextual summary prompt and deterministic chunk header formatting."""

import re
from collections.abc import Iterable
from datetime import date

from indexer.enrich.llm import LlmClient

PROMPT_VERSION = "ctx-v1"
SYSTEM_PROMPT = "You write one sentence that situates a research paper for a search index."


def summarize_document(
    client: LlmClient,
    *,
    title: str,
    categories: Iterable[str],
    abstract: str,
    section_headings: Iterable[str],
) -> str:
    """Create the one summary call allowed for a document."""
    headings = ", ".join(section_headings)
    # The production tokenizer is used later for chunking; a word cap keeps this bounded
    # before any model call without adding another heavyweight tokenizer instance.
    headings = " ".join(headings.split()[:1500])
    user = (
        f"Title: {title}\n"
        f"Categories: {', '.join(categories)}\n"
        f"Abstract: {abstract}\n"
        f"Section headings: {headings}\n\n"
        "Write ONE sentence of at most 50 words stating the paper's problem, approach and "
        "domain. Output only the sentence."
    )
    return post_process_summary(client.complete(SYSTEM_PROMPT, user, num_predict=96))


def post_process_summary(text: str) -> str:
    """Normalize quotes, retain one sentence, and enforce the 50-word storage contract."""
    cleaned = text.strip().strip("\"'")
    cleaned = cleaned.strip(chr(0x201C) + chr(0x201D) + chr(0x2018) + chr(0x2019))
    sentence = re.split(r"(?<=[.!?])\s+", cleaned, maxsplit=1)[0]
    words = sentence.split()
    return " ".join(words[:50]).strip()


def context_header(
    title: str,
    primary_category: str,
    published_on: date,
    section_path: str,
    summary: str,
) -> str:
    """Format the D3 context header stored separately from chunk content."""
    return (
        f"Paper: {title} ({primary_category}, {published_on.year}). "
        f"Section: {section_path}. {summary}"
    )
