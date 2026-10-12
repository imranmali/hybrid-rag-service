"""Transactional, idempotent Postgres persistence for indexed papers."""

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

import psycopg
from pgvector import Vector
from pgvector.psycopg import register_vector

from indexer.models import IndexedPaper
from indexer.names import normalize_author_name

INDEX_METADATA_KEYS = (
    "embedding_model",
    "embedding_model_digest",
    "llm_model",
    "llm_model_digest",
    "chunk_size_tokens",
    "chunk_overlap_tokens",
    "min_chunk_tokens",
    "header_prompt_version",
    "parser_version",
    "pipeline_fingerprint",
    "document_count",
    "chunk_count",
    "last_ingest_at",
)


@dataclass
class IngestSummary:
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped: int = 0
    failed: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "inserted": self.inserted,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "skipped": self.skipped,
            "failed": self.failed,
        }


class IndexWriter:
    """Write documents one at a time so a failed replacement preserves the old version."""

    def __init__(self, database_url: str, connection: psycopg.Connection | None = None) -> None:
        self._connection = connection or psycopg.connect(database_url, autocommit=True)
        self._owns_connection = connection is None
        register_vector(self._connection)

    def close(self) -> None:
        if self._owns_connection:
            self._connection.close()

    def is_unchanged(self, external_id: str, content_hash: str, fingerprint: str) -> bool:
        with self._connection.cursor() as cursor:
            row = cursor.execute(
                """
                select 1 from documents
                where external_id = %s and content_hash = %s and pipeline_fingerprint = %s
                """,
                (external_id, content_hash, fingerprint),
            ).fetchone()
        return row is not None

    def write(self, indexed: IndexedPaper, *, force: bool = False) -> str:
        """Return inserted/updated/unchanged after one fully atomic document operation."""
        paper = indexed.paper
        if not force and self.is_unchanged(
            paper.external_id, indexed.content_hash, indexed.pipeline_fingerprint
        ):
            return "unchanged"
        with self._connection.transaction(), self._connection.cursor() as cursor:
            existed = (
                cursor.execute(
                    "select 1 from documents where external_id = %s", (paper.external_id,)
                ).fetchone()
                is not None
            )
            document_id = uuid.uuid5(uuid.NAMESPACE_URL, f"arxiv:{paper.external_id}")
            cursor.execute(
                """
                    insert into documents (
                      id, external_id, arxiv_version, title, abstract, primary_category,
                      published_on, updated_on, source_url, raw_text, context_summary,
                      content_hash, pipeline_fingerprint
                    ) values (
                      %(id)s, %(external_id)s, %(version)s, %(title)s, %(abstract)s,
                      %(primary_category)s, %(published_on)s, %(updated_on)s, %(source_url)s,
                      %(raw_text)s, %(context_summary)s, %(content_hash)s, %(fingerprint)s
                    )
                    on conflict (external_id) do update set
                      arxiv_version = excluded.arxiv_version,
                      title = excluded.title,
                      abstract = excluded.abstract,
                      primary_category = excluded.primary_category,
                      published_on = excluded.published_on,
                      updated_on = excluded.updated_on,
                      source_url = excluded.source_url,
                      raw_text = excluded.raw_text,
                      context_summary = excluded.context_summary,
                      content_hash = excluded.content_hash,
                      pipeline_fingerprint = excluded.pipeline_fingerprint,
                      ingested_at = now()
                    """,
                {
                    "id": document_id,
                    "external_id": paper.external_id,
                    "version": paper.version,
                    "title": paper.title,
                    "abstract": paper.abstract,
                    "primary_category": paper.primary_category,
                    "published_on": paper.published_on,
                    "updated_on": paper.updated_on,
                    "source_url": paper.source_url,
                    "raw_text": indexed.parsed.raw_text,
                    "context_summary": indexed.context_summary,
                    "content_hash": indexed.content_hash,
                    "fingerprint": indexed.pipeline_fingerprint,
                },
            )
            cursor.execute("delete from chunks where document_id = %s", (document_id,))
            cursor.execute("delete from paper_authors where document_id = %s", (document_id,))
            cursor.execute("delete from paper_categories where document_id = %s", (document_id,))
            self._write_authors(cursor, document_id, paper.authors)
            self._write_categories(cursor, document_id, paper.categories, paper.primary_category)
            self._write_chunks(cursor, document_id, indexed)
        return "updated" if existed else "inserted"

    @staticmethod
    def _write_authors(
        cursor: psycopg.Cursor[object], document_id: uuid.UUID, authors: tuple[str, ...]
    ) -> None:
        for position, name in enumerate(authors, start=1):
            normalized = normalize_author_name(name)
            cursor.execute(
                "insert into authors (name, name_normalized) values (%s, %s) "
                "on conflict (name_normalized) do nothing",
                (name, normalized),
            )
            author_id = cursor.execute(
                "select id from authors where name_normalized = %s", (normalized,)
            ).fetchone()[0]
            cursor.execute(
                "insert into paper_authors (document_id, author_id, position) values (%s, %s, %s)",
                (document_id, author_id, position),
            )

    @staticmethod
    def _write_categories(
        cursor: psycopg.Cursor[object],
        document_id: uuid.UUID,
        categories: tuple[str, ...],
        primary_category: str,
    ) -> None:
        cursor.executemany(
            "insert into paper_categories (document_id, category, is_primary) values (%s, %s, %s)",
            [(document_id, category, category == primary_category) for category in categories],
        )

    @staticmethod
    def _write_chunks(
        cursor: psycopg.Cursor[object], document_id: uuid.UUID, indexed: IndexedPaper
    ) -> None:
        rows = [
            (
                document_id,
                chunk.ordinal,
                chunk.section_path,
                chunk.content,
                header,
                chunk.token_count,
                chunk.content_sha256,
                Vector(list(embedding)),
                Vector(list(contextual_embedding)),
            )
            for chunk, header, embedding, contextual_embedding in zip(
                indexed.chunks,
                indexed.context_headers,
                indexed.embeddings,
                indexed.contextual_embeddings,
                strict=True,
            )
        ]
        cursor.executemany(
            """
            insert into chunks (
              document_id, ordinal, section_path, content, context_header, token_count,
              content_sha256, embedding, embedding_ctx
            ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            rows,
        )

    def prune(self, external_ids: Iterable[str]) -> int:
        ids = list(external_ids)
        with self._connection.transaction(), self._connection.cursor() as cursor:
            cursor.execute("delete from documents where external_id <> all(%s)", (ids,))
            return cursor.rowcount

    def status_counts(self) -> dict[str, int]:
        """Return the minimal operational counts for the CLI status command."""
        with self._connection.cursor() as cursor:
            document_count, chunk_count = cursor.execute(
                "select (select count(*) from documents), (select count(*) from chunks)"
            ).fetchone()
        return {"documents": document_count, "chunks": chunk_count}

    def finalize(self, metadata: dict[str, str]) -> None:
        """Upsert all service-visible metadata, remove orphan names, and refresh planner stats."""
        with self._connection.transaction(), self._connection.cursor() as cursor:
            counts = cursor.execute(
                "select (select count(*) from documents), (select count(*) from chunks)"
            ).fetchone()
            values = {
                **metadata,
                "document_count": str(counts[0]),
                "chunk_count": str(counts[1]),
                "last_ingest_at": datetime.now(UTC).isoformat(),
            }
            missing = set(INDEX_METADATA_KEYS).difference(values)
            if missing:
                raise ValueError(f"missing index metadata keys: {', '.join(sorted(missing))}")
            cursor.executemany(
                """
                insert into index_metadata (key, value, updated_at) values (%s, %s, now())
                on conflict (key) do update set value = excluded.value, updated_at = now()
                """,
                [(key, values[key]) for key in INDEX_METADATA_KEYS],
            )
            cursor.execute(
                "delete from authors a where not exists "
                "(select 1 from paper_authors pa where pa.author_id = a.id)"
            )
            cursor.execute("analyze documents")
            cursor.execute("analyze chunks")
            cursor.execute("analyze authors")
            cursor.execute("analyze paper_authors")
            cursor.execute("analyze paper_categories")
