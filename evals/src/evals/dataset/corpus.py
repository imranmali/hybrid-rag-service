"""Read-only view of the indexed corpus used by dataset generation.

The `Corpus` protocol is the seam: production reads Postgres as `rag_reader`, tests use an in-memory
fixture. Aggregate questions need only `fetch` (parameterised SQL with `:name` placeholders)."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class Paper:
    external_id: str
    title: str
    primary_category: str
    categories: tuple[str, ...]
    published_on: str  # ISO date
    authors: tuple[tuple[str, str], ...]  # (display name, normalized name)

    @property
    def year(self) -> int:
        return int(self.published_on[:4])


@dataclass(frozen=True)
class Chunk:
    external_id: str
    ordinal: int
    section_path: str
    content: str
    content_sha256: str
    token_count: int

    @property
    def top_section(self) -> str:
        return self.section_path.split(" > ")[0].strip()

    @property
    def key(self) -> tuple[str, int]:
        return (self.external_id, self.ordinal)


class Corpus(Protocol):
    def papers(self) -> list[Paper]:
        """All papers ordered by external_id."""

    def chunks(self, external_id: str) -> list[Chunk]:
        """All chunks of a paper ordered by ordinal."""

    def fetch(self, sql: str, params: dict[str, Any]) -> list[tuple]:
        """Run a read-only query; `:name` placeholders."""


_PARAM = re.compile(r"(?<!:):([a-z_][a-z0-9_]*)")


def to_psycopg(sql: str) -> str:
    """`:name` -> `%(name)s` (and escape literal percent signs)."""
    return _PARAM.sub(r"%(\1)s", sql.replace("%", "%%"))


@dataclass
class PostgresCorpus:
    dsn: str
    _conn: Any = field(default=None, repr=False)

    def _connect(self):
        import psycopg

        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(self.dsn, autocommit=True)
        return self._conn

    def fetch(self, sql: str, params: dict[str, Any]) -> list[tuple]:
        with self._connect().cursor() as cur:
            cur.execute(to_psycopg(sql), params)
            return list(cur.fetchall())

    def papers(self) -> list[Paper]:
        rows = self.fetch(
            "select d.external_id, d.title, d.primary_category, d.published_on::text,"
            "  (select array_agg(pc.category order by pc.category) from paper_categories pc"
            "     where pc.document_id = d.id),"
            "  (select array_agg(a.name || E'\\x1f' || a.name_normalized order by pa.position)"
            "     from paper_authors pa join authors a on a.id = pa.author_id"
            "     where pa.document_id = d.id)"
            " from documents d order by d.external_id",
            {},
        )
        papers = []
        for ext, title, primary, published, cats, authors in rows:
            pairs = tuple(tuple(a.split("\x1f", 1)) for a in (authors or []))
            papers.append(Paper(ext, title, primary, tuple(cats or ()), published, pairs))
        return papers

    def chunks(self, external_id: str) -> list[Chunk]:
        rows = self.fetch(
            "select d.external_id, c.ordinal, c.section_path, c.content, c.content_sha256,"
            " c.token_count from chunks c join documents d on d.id = c.document_id"
            " where d.external_id = :ext order by c.ordinal",
            {"ext": external_id},
        )
        return [Chunk(*r) for r in rows]


def chunk_lookup(corpus: Corpus, keys: Sequence[tuple[str, int]]) -> dict[tuple[str, int], Chunk]:
    out: dict[tuple[str, int], Chunk] = {}
    for ext in sorted({k[0] for k in keys}):
        for c in corpus.chunks(ext):
            if c.key in keys:
                out[c.key] = c
    return out
