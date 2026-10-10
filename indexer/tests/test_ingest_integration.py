import hashlib
from datetime import date
from pathlib import Path

import psycopg
import pytest
from testcontainers.postgres import PostgresContainer

from indexer import cli
from indexer.arxiv.cache import ArxivCache
from indexer.bench import run_benchmark
from indexer.chunk.tokenizer import BgeM3Tokenizer
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
            with psycopg.connect(dsn) as connection, connection.cursor() as cursor:
                ingested_at = cursor.execute(
                    "select ingested_at from documents where external_id = '2401.00001'"
                ).fetchone()[0]
                old_chunk_id = cursor.execute(
                    """
                    select c.id from chunks c join documents d on d.id = c.document_id
                    where d.external_id = '2401.00001'
                    """
                ).fetchone()[0]
            assert writer.write(first) == "unchanged"
            with psycopg.connect(dsn) as connection, connection.cursor() as cursor:
                assert (
                    cursor.execute(
                        "select ingested_at from documents where external_id = '2401.00001'"
                    ).fetchone()[0]
                    == ingested_at
                )
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
            assert cursor.execute("select count(*) from chunks").fetchone()[0] == 2
            assert cursor.execute("select count(*) from authors").fetchone()[0] == 1
            assert cursor.execute("select count(*) from paper_authors").fetchone()[0] == 2
            assert cursor.execute("select count(*) from paper_categories").fetchone()[0] == 4
            assert (
                cursor.execute(
                    "select c.content from chunks c join documents d on d.id = c.document_id "
                    "where d.external_id = '2401.00001'"
                ).fetchone()[0]
                == "changed text"
            )
            assert (
                cursor.execute(
                    """
                select c.id from chunks c join documents d on d.id = c.document_id
                where d.external_id = '2401.00001'
                """
                ).fetchone()[0]
                != old_chunk_id
            )
            assert cursor.execute("select count(*) from index_metadata").fetchone()[0] == len(
                INDEX_METADATA_KEYS
            )


class _FakeLlm:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, system: str, user: str, *, num_predict: int) -> str:
        self.calls += 1
        return "A fake one-sentence summary of the paper."


class _FakeEmbedder:
    def __init__(self) -> None:
        self.calls = 0
        self.fail = False

    def embed(self, texts):
        self.calls += 1
        if self.fail:
            raise RuntimeError("embedder unavailable")
        vectors = []
        for text in texts:
            digest = hashlib.sha256(text.encode()).digest()
            vectors.append([digest[index % 32] / 255 + 0.001 for index in range(1024)])
        return vectors


def _html(title: str, body: str) -> str:
    return f"""
    <article class="ltx_document"><h1 class="ltx_title_document">{title}</h1>
      <div class="ltx_abstract"><div class="ltx_para">{title} abstract.</div></div>
      <section class="ltx_section">
        <h2 class="ltx_title"><span class="ltx_tag">1</span> Method</h2>
        <div class="ltx_para">{body}</div>
      </section>
    </article>
    """


def _record(external_id: str, author: str) -> dict[str, object]:
    return {
        "external_id": external_id,
        "version": 1,
        "title": f"Title {external_id}",
        "abstract": "Abstract",
        "authors": [author],
        "categories": ["cs.IR", "cs.CL"],
        "primary_category": "cs.IR",
        "published_on": "2024-01-01",
        "updated_on": "2024-01-01",
    }


def test_ingest_pipeline_end_to_end_with_fake_llm_and_embedder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "CACHE_ROOT", tmp_path)
    cache = ArxivCache(tmp_path)
    sentence = "Dense retrieval encodes passages into vectors for approximate search. "
    cache.write_html("2401.00001", 1, _html("Paper one", sentence * 5))
    cache.write_html("2401.00002", 1, _html("Paper two", sentence * 6))
    records = [
        _record("2401.00001", "José Pérez-Gómez"),
        _record("2401.00002", "Jose Perez-Gomez"),
    ]
    with PostgresContainer("pgvector/pgvector:pg17") as container:
        dsn = _admin_dsn(container)
        _apply_migrations(dsn)
        indexer_dsn = dsn.replace("test:test@", "rag_indexer:indexer@")
        writer = IndexWriter(indexer_dsn)
        llm, embedder, tokenizer = _FakeLlm(), _FakeEmbedder(), BgeM3Tokenizer()

        def run(force: bool = False) -> dict[str, int]:
            return cli.ingest_records(
                records, writer, tokenizer, llm, embedder, "fp-1", force=force
            ).as_dict()

        def query(sql: str):
            with psycopg.connect(dsn) as connection, connection.cursor() as cursor:
                return cursor.execute(sql).fetchall()

        try:
            assert run() == {"inserted": 2, "updated": 0, "unchanged": 0, "skipped": 0, "failed": 0}
            assert (llm.calls, embedder.calls) == (2, 4)
            counts = dict(
                query(
                    "select 'documents', count(*) from documents union all "
                    "select 'chunks', count(*) from chunks union all "
                    "select 'authors', count(*) from authors union all "
                    "select 'paper_authors', count(*) from paper_authors union all "
                    "select 'paper_categories', count(*) from paper_categories"
                )
            )
            assert counts["documents"] == 2
            assert counts["chunks"] >= 2
            assert counts["authors"] == 1  # one normalized author shared across papers
            assert counts["paper_authors"] == 2
            assert counts["paper_categories"] == 4
            assert query("select count(*) from chunks where embedding_ctx is null") == [(0,)]
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
                    "pipeline_fingerprint": "fp-1",
                }
            )
            keys = {key for (key,) in query("select key from index_metadata")}
            assert keys == set(INDEX_METADATA_KEYS) and len(keys) == 13

            before = query("select external_id, ingested_at from documents order by 1")
            llm.calls = embedder.calls = 0
            assert run() == {"inserted": 0, "updated": 0, "unchanged": 2, "skipped": 0, "failed": 0}
            assert (llm.calls, embedder.calls) == (0, 0)
            assert query("select external_id, ingested_at from documents order by 1") == before

            ids_before = {
                row[0]
                for row in query(
                    "select c.id::text from chunks c join documents d on d.id = c.document_id "
                    "where d.external_id = '2401.00001'"
                )
            }
            cache.write_html("2401.00001", 1, _html("Paper one", sentence * 5 + "A new claim."))
            assert run() == {"inserted": 0, "updated": 1, "unchanged": 1, "skipped": 0, "failed": 0}
            ids_after = {
                row[0]
                for row in query(
                    "select c.id::text from chunks c join documents d on d.id = c.document_id "
                    "where d.external_id = '2401.00001'"
                )
            }
            assert ids_after and ids_after.isdisjoint(ids_before)

            # A failure while preparing a changed document leaves the stored version intact.
            cache.write_html("2401.00001", 1, _html("Paper one", sentence * 5 + "Another claim."))
            embedder.fail = True
            assert run() == {"inserted": 0, "updated": 0, "unchanged": 1, "skipped": 0, "failed": 1}
            embedder.fail = False
            assert {
                row[0]
                for row in query(
                    "select c.id::text from chunks c join documents d on d.id = c.document_id "
                    "where d.external_id = '2401.00001'"
                )
            } == ids_after

            # The benchmark runs against the same schema and leaves corpus indexes untouched.
            before_indexes = query("select indexname from pg_indexes where tablename = 'chunks'")
            results = run_benchmark(indexer_dsn, queries=5, seed=42)
            assert {item["filter"] for item in results["filtered_hnsw"]} >= {
                "category=cs.IR",
                "year=2024",
            }
            assert query("select indexname from pg_indexes where tablename = 'chunks'") == (
                before_indexes
            )

            # --prune removes documents (and their chunks) that are outside the manifest slice.
            assert writer.prune(["2401.00002"]) == 1
            assert query("select external_id from documents") == [("2401.00002",)]
            assert (
                query("select count(*) from chunks c join documents d on d.id = c.document_id")[0][
                    0
                ]
                == query("select count(*) from chunks")[0][0]
            )
        finally:
            writer.close()
