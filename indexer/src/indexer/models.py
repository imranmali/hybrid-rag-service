"""Small immutable value types shared by acquisition, parsing, and storage."""

from dataclasses import dataclass, field
from datetime import date


@dataclass(frozen=True)
class Paper:
    external_id: str
    version: int
    title: str
    abstract: str
    authors: tuple[str, ...]
    categories: tuple[str, ...]
    primary_category: str
    published_on: date
    updated_on: date

    @property
    def source_url(self) -> str:
        return f"https://arxiv.org/html/{self.external_id}v{self.version}"


@dataclass(frozen=True)
class Section:
    path: str
    text: str


@dataclass(frozen=True)
class ParsedDocument:
    title: str
    sections: tuple[Section, ...]

    @property
    def raw_text(self) -> str:
        return "\n\n".join(section.text for section in self.sections)


@dataclass(frozen=True)
class Chunk:
    ordinal: int
    section_path: str
    content: str
    token_count: int
    content_sha256: str


@dataclass(frozen=True)
class IndexedPaper:
    paper: Paper
    parsed: ParsedDocument
    chunks: tuple[Chunk, ...]
    context_summary: str
    context_headers: tuple[str, ...]
    embeddings: tuple[tuple[float, ...], ...]
    contextual_embeddings: tuple[tuple[float, ...], ...]
    content_hash: str
    pipeline_fingerprint: str
    metadata: dict[str, str] = field(default_factory=dict)
