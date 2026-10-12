"""Deterministic, section-bounded 384/64-token chunking."""

import hashlib
from dataclasses import dataclass
from typing import Protocol

from indexer.chunk.sentences import split_sentences
from indexer.models import Chunk, Section


class Tokenizer(Protocol):
    def count(self, text: str) -> int: ...

    def encode(self, text: str) -> list[int]: ...

    def decode(self, token_ids: list[int]) -> str: ...


@dataclass(frozen=True)
class ChunkingConfig:
    chunk_size_tokens: int = 384
    overlap_tokens: int = 64
    min_chunk_tokens: int = 64


DEFAULT_CONFIG = ChunkingConfig()


def chunk_sections(
    sections: tuple[Section, ...] | list[Section],
    tokenizer: Tokenizer,
    config: ChunkingConfig = DEFAULT_CONFIG,
) -> tuple[Chunk, ...]:
    """Chunk every eligible section without ever crossing a section boundary."""
    chunks: list[Chunk] = []
    for section in sections:
        if tokenizer.count(section.text) < 20:
            continue
        for content in _chunk_section(section.text, tokenizer, config):
            chunks.append(
                Chunk(
                    ordinal=len(chunks),
                    section_path=section.path,
                    content=content,
                    token_count=tokenizer.count(content),
                    content_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
                )
            )
    return tuple(chunks)


def _chunk_section(text: str, tokenizer: Tokenizer, config: ChunkingConfig) -> list[str]:
    sentences = _expanded_sentences(text, tokenizer, config)
    if not sentences:
        return []
    token_counts = [tokenizer.count(sentence) for sentence in sentences]
    output: list[str] = []
    start = 0
    while start < len(sentences):
        end = start
        total = 0
        while end < len(sentences) and total + token_counts[end] <= config.chunk_size_tokens:
            total += token_counts[end]
            end += 1
        if end == start:  # Defensive: `_expanded_sentences` guarantees this cannot happen.
            end += 1
            total = token_counts[start]
        remaining = sum(token_counts[end:])
        if end < len(sentences) and remaining < config.min_chunk_tokens:
            tail = " ".join(sentences[end:])
            merged = " ".join([*sentences[start:end], tail])
            if tokenizer.count(merged) <= config.chunk_size_tokens + config.min_chunk_tokens:
                end = len(sentences)
        output.append(" ".join(sentences[start:end]).strip())
        if end == len(sentences):
            break
        overlap_start = _overlap_start(token_counts, start, end, config.overlap_tokens)
        start = overlap_start if overlap_start > start else end
    return output


def _expanded_sentences(text: str, tokenizer: Tokenizer, config: ChunkingConfig) -> list[str]:
    expanded: list[str] = []
    for sentence in split_sentences(text):
        ids = tokenizer.encode(sentence)
        if len(ids) <= config.chunk_size_tokens:
            expanded.append(sentence)
            continue
        step = config.chunk_size_tokens - config.overlap_tokens
        for start in range(0, len(ids), step):
            window = ids[start : start + config.chunk_size_tokens]
            if not window:
                break
            expanded.append(tokenizer.decode(window))
            if start + config.chunk_size_tokens >= len(ids):
                break
    return expanded


def _overlap_start(counts: list[int], start: int, end: int, maximum: int) -> int:
    """Find the first trailing whole sentence whose total is within the cap."""
    total = 0
    overlap_start = end
    for index in range(end - 1, start - 1, -1):
        if total + counts[index] > maximum:
            break
        total += counts[index]
        overlap_start = index
    # Preserve progress for a tiny one-sentence chunk before a huge next sentence.
    return overlap_start if overlap_start > start else end
