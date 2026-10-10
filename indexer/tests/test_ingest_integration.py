from datetime import date
from pathlib import Path

import psycopg
import pytest
from testcontainers.postgres import PostgresContainer

from indexer.models import Chunk, IndexedPaper, Paper, ParsedDocument, Section
from indexer.store.writer import INDEX_METADATA_KEYS, IndexWriter

pytestmark = pytest.mark.integration


def _indexed(external_id: str, content: str, author: str = "José Pérez-Gómez") -> IndexedPaper:
    paper = Paper(
        external_id=external_id,
        version=1,
        title=f"Title {external_id}",
        abstract="Abstract",
        authors=(author,),
        categories=("cs.IR", "cs.CL"),
        primary_category="cs.IR",
        published_on=date(2024, 1, 1),
        updated_on=date(2024, 1, 1),
    )
    chunk = Chunk(0, "Abstract", content, 2, f"hash-{content}")
    return IndexedPaper(
        paper=paper,
        parsed=ParsedDocument(paper.title, (Section("Abstract", content),)),
        chunks=(chunk,),
        context_summary="A short summary.",
        context_headers=(
            f"Paper: {paper.title} (cs.IR, 2024). Section: Abstract. A short summary.",
        ),
        embeddings=(tuple([0.0] * 1024),),
        contextual_embeddings=(tuple([0.1] * 1024),),
        content_hash=f"content-{content}",
        pipeline_fingerprint="pipeline",
    )


def _admin_dsn(container: PostgresContainer) -> str:
    return (
        f"postgresql://test:test@{container.get_container_host_ip()}:"
        f"{container.get_exposed_port(5432)}/test"
    )


def _apply_migrations(dsn: str) -> None:
    root = Path(__file__).resolve().parents[2]
    v1 = (root / "infra" / "migrations" / "V1__schema.sql").read_text()
    v2 = (root / "infra" / "migrations" / "V2__roles.sql").read_text()
    v2 = (
        v2.replace("${indexer_password}", "indexer")
        .replace("${reader_password}", "reader")
        .replace("${router_password}", "router")
    )
    with psycopg.connect(dsn, autocommit=True) as connection, connection.cursor() as cursor:
        cursor.execute(v1)
        cursor.execute(v2)


def test_writer_is_idempotent_atomic_and_populates_metadata() -> None:
    with PostgresContainer("pgvector/pgvector:pg17") as container:
        dsn = _admin_dsn(container)
        _apply_migrations(dsn)
        indexer_dsn = dsn.replace("test:test@", "rag_indexer:indexer@")
        writer = IndexWriter(indexer_dsn)
        first = _indexed("2401.00001", "first text")
        second = _indexed("2401.00002", "second text", "Jose Perez-Gomez")
        try:
            assert writer.write(first) == "inserted"
            assert writer.write(second) == "inserted"
            assert writer.write(first) == "unchanged"
            writer.finalize(
                {
                    "embedding_model": "bge-m3",
                    "embedding_model_digest": "embed",
                    "llm_model": "llama3.1:8b",
                    "llm_model_digest": "llm",
                    "chunk_size_tokens": "384",
                    "chunk_overlap_tokens": "64",
                    "min_chunk_tokens": "64",
                    "header_prompt_version": "ctx-v1",
                    "parser_version": "html-v1",
                    "pipeline_fingerprint": "pipeline",
                }
            )
            changed = _indexed("2401.00001", "changed text")
            assert writer.write(changed) == "updated"
            broken = _indexed("2401.00001", "broken")
            broken = IndexedPaper(
                **{**broken.__dict__, "context_headers": ()}
            )  # zip(strict=True) fails inside the transaction
            with pytest.raises(ValueError):
                writer.write(broken)
        finally:
            writer.close()
        with psycopg.connect(dsn) as connection, connection.cursor() as cursor:
            assert cursor.execute("select count(*) from documents").fetchone()[0] == 2
            assert cursor.execute("select count(*) from authors").fetchone()[0] == 1
            assert (
                cursor.execute(
                    "select c.content from chunks c join documents d on d.id = c.document_id "
                    "where d.external_id = '2401.00001'"
                ).fetchone()[0]
                == "changed text"
            )
            assert cursor.execute("select count(*) from index_metadata").fetchone()[0] == len(
                INDEX_METADATA_KEYS
            )
