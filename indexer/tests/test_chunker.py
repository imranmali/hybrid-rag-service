from indexer.chunk.chunker import ChunkingConfig, chunk_sections
from indexer.models import Section


class WhitespaceTokenizer:
    def encode(self, text: str) -> list[int]:
        return list(range(len(text.split())))

    def count(self, text: str) -> int:
        return len(text.split())

    def decode(self, token_ids: list[int]) -> str:
        return " ".join(f"token{token}" for token in token_ids)


def test_chunker_is_bounded_section_scoped_and_stable() -> None:
    tokenizer = WhitespaceTokenizer()
    sentence = " ".join(f"word{number}" for number in range(40)) + "."
    sections = (
        Section("One", f"{sentence} {sentence} {sentence}"),
        Section("Two", f"{sentence} {sentence}"),
    )
    config = ChunkingConfig(chunk_size_tokens=80, overlap_tokens=40, min_chunk_tokens=20)

    first = chunk_sections(sections, tokenizer, config)
    second = chunk_sections(sections, tokenizer, config)

    assert [chunk.ordinal for chunk in first] == list(range(len(first)))
    assert all(chunk.token_count <= 100 for chunk in first)
    assert {chunk.section_path for chunk in first} == {"One", "Two"}
    assert first == second


def test_long_sentence_is_windowed() -> None:
    tokenizer = WhitespaceTokenizer()
    section = Section("Long", " ".join(f"word{number}" for number in range(240)))

    chunks = chunk_sections(
        (section,),
        tokenizer,
        ChunkingConfig(chunk_size_tokens=80, overlap_tokens=20, min_chunk_tokens=20),
    )

    assert len(chunks) == 4
    assert all(chunk.token_count <= 80 for chunk in chunks)
