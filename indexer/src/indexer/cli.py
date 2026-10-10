"""Typer command line interface for reproducible corpus indexing."""

import json
from datetime import date
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from indexer.arxiv.api import parse_atom, query_url
from indexer.arxiv.cache import ArxivCache
from indexer.arxiv.html_fetch import HtmlFetcher
from indexer.arxiv.ratelimit import RateLimiter
from indexer.arxiv.select import CATEGORIES, read_manifest, select_papers, write_manifest
from indexer.bench import run_benchmark, write_results
from indexer.chunk.chunker import chunk_sections
from indexer.chunk.tokenizer import BgeM3Tokenizer
from indexer.config import REPOSITORY_ROOT, Settings
from indexer.embed.ollama import OllamaEmbedder
from indexer.enrich.context import PROMPT_VERSION, context_header, summarize_document
from indexer.enrich.llm import LlmClient
from indexer.fingerprint import content_hash, pipeline_fingerprint
from indexer.models import IndexedPaper, Paper
from indexer.models_lock import ModelDriftError, verify_model_lock
from indexer.parse.html_parser import parse_html
from indexer.store.writer import INDEX_METADATA_KEYS, IndexWriter, IngestSummary

app = typer.Typer(no_args_is_help=True, help="Acquire and index the reproducible arXiv corpus.")
INDEXER_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = INDEXER_ROOT / "corpus" / "manifest.jsonl"
CACHE_ROOT = REPOSITORY_ROOT / "data" / "cache" / "arxiv"


def _settings() -> Settings:
    try:
        return Settings()
    except ValidationError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(2) from error


@app.command()
def select(
    target: Annotated[int, typer.Option(min=1)] = 220,
    seed: int = 42,
) -> None:
    """Query 2023-2026 category quarters and write a deterministic manifest."""
    settings = _settings()
    fetcher = HtmlFetcher(ArxivCache(CACHE_ROOT), RateLimiter(), contact=settings.arxiv_contact)
    try:
        candidates: list[Paper] = []
        for category in CATEGORIES:
            for year in range(2023, min(date.today().year, 2026) + 1):
                for quarter in range(1, 5):
                    start_month = (quarter - 1) * 3 + 1
                    start = date(year, start_month, 1)
                    if quarter == 4:
                        end = date(year, 12, 31)
                    else:
                        end = date(year, start_month + 3, 1).fromordinal(
                            date(year, start_month + 3, 1).toordinal() - 1
                        )
                    if start > date.today():
                        continue
                    url = query_url(category, start, min(end, date.today()))
                    candidates.extend(
                        paper
                        for paper in parse_atom(fetcher.get_api(url))
                        if paper.primary_category == category
                    )
        # Test only selected candidates, then reselect after any rejected page. Checking every
        # entry in the 48 x 50 API pool would turn the 3 s politeness policy into a multi-hour run.
        accepted: dict[str, bool] = {}
        tokenizer = BgeM3Tokenizer()

        def usable(paper: Paper) -> bool:
            if paper.external_id in accepted:
                return accepted[paper.external_id]
            html = fetcher.get_html(paper.external_id, paper.version)
            parsed = parse_html(html) if html else None
            accepted[paper.external_id] = bool(parsed and tokenizer.count(parsed.raw_text) >= 2000)
            return accepted[paper.external_id]

        remaining = list(candidates)
        while True:
            selected = select_papers(remaining, target=target, seed=seed)
            rejected = [
                selected_paper.paper.external_id
                for selected_paper in selected
                if not usable(selected_paper.paper)
            ]
            if not rejected:
                break
            rejected_ids = set(rejected)
            remaining = [paper for paper in remaining if paper.external_id not in rejected_ids]
        write_manifest(MANIFEST_PATH, selected)
        typer.echo(f"wrote {len(selected)} papers to {MANIFEST_PATH}")
    finally:
        fetcher.close()


@app.command()
def fetch() -> None:
    """Fetch every version-pinned manifest page, using only the on-disk cache when warm."""
    settings = _settings()
    manifest = _require_manifest()
    fetcher = HtmlFetcher(ArxivCache(CACHE_ROOT), RateLimiter(), contact=settings.arxiv_contact)
    try:
        missing = 0
        for record in manifest:
            if fetcher.get_html(str(record["external_id"]), int(record["version"])) is None:
                missing += 1
        typer.echo(
            json.dumps(
                {
                    "papers": len(manifest),
                    "missing": missing,
                    "network_requests": fetcher.network_requests,
                }
            )
        )
    finally:
        fetcher.close()


@app.command()
def ingest(
    limit: Annotated[int | None, typer.Option(min=1)] = None,
    force: bool = False,
    prune: bool = False,
    allow_model_drift: bool = False,
) -> None:
    """Parse, enrich, embed, and atomically upsert manifest papers."""
    settings = _settings()
    manifest = _require_manifest()
    records = manifest[:limit] if limit else manifest
    try:
        digests = verify_model_lock(settings, allow_drift=allow_model_drift)
    except ModelDriftError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(ModelDriftError.exit_code) from error
    fingerprint = pipeline_fingerprint(
        embedding_model=settings.ollama_embed_model,
        embedding_model_digest=digests.embedding,
        llm_model=(
            f"anthropic:{settings.anthropic_model}"
            if settings.llm_provider == "anthropic"
            else settings.ollama_llm_model
        ),
        llm_model_digest=_llm_digest(settings, digests.llm),
    )
    writer = IndexWriter(settings.indexer_database_url)
    tokenizer = BgeM3Tokenizer()
    llm = LlmClient(settings)
    embedder = OllamaEmbedder(settings)
    summary = IngestSummary()
    try:
        for record in records:
            paper = _paper_from_record(record)
            try:
                indexed = _index_paper(
                    paper, writer, tokenizer, llm, embedder, fingerprint, force=force
                )
                if indexed is None:
                    summary.unchanged += 1
                    continue
                result = writer.write(indexed, force=force)
                setattr(summary, result, getattr(summary, result) + 1)
            except Exception as error:
                # Keep other papers runnable; final error budget decides exit.
                summary.failed += 1
                typer.echo(f"{paper.external_id}: {error}", err=True)
        if prune:
            writer.prune(str(record["external_id"]) for record in records)
        writer.finalize(
            _metadata(
                settings,
                digests.embedding,
                _llm_digest(settings, digests.llm),
                fingerprint,
            )
        )
    finally:
        embedder.close()
        llm.close()
        writer.close()
    typer.echo(json.dumps(summary.as_dict(), sort_keys=True))
    if summary.failed > len(records) * 0.05:
        raise typer.Exit(1)


@app.command()
def status() -> None:
    """Show index cardinalities and warm the bge-m3 tokenizer cache."""
    settings = _settings()
    tokenizer = BgeM3Tokenizer()
    tokenizer.count("warm tokenizer")
    writer = IndexWriter(settings.indexer_database_url)
    try:
        typer.echo(json.dumps(writer.status_counts(), sort_keys=True))
    finally:
        writer.close()


@app.command("bench-index")
def bench_index(
    queries: Annotated[int, typer.Option(min=1)] = 200,
    seed: int = 42,
) -> None:
    """Measure temporary HNSW/IVFFlat indexes without changing corpus indexes."""
    settings = _settings()
    results = run_benchmark(settings.indexer_database_url, queries=queries, seed=seed)
    json_path, markdown_path = write_results(results, INDEXER_ROOT / "bench")
    typer.echo(f"wrote {json_path} and {markdown_path}")


def _require_manifest() -> list[dict[str, object]]:
    if not MANIFEST_PATH.exists():
        typer.echo(f"manifest is missing: run `indexer select` first ({MANIFEST_PATH})", err=True)
        raise typer.Exit(2)
    return read_manifest(MANIFEST_PATH)


def _paper_from_record(record: dict[str, object]) -> Paper:
    required = ("title", "abstract", "authors", "categories", "updated_on")
    missing = [key for key in required if key not in record]
    if missing:
        raise ValueError(f"manifest entry lacks acquisition metadata: {', '.join(missing)}")
    return Paper(
        external_id=str(record["external_id"]),
        version=int(record["version"]),
        title=str(record["title"]),
        abstract=str(record["abstract"]),
        authors=tuple(str(value) for value in record["authors"]),  # type: ignore[index]
        categories=tuple(str(value) for value in record["categories"]),  # type: ignore[index]
        primary_category=str(record["primary_category"]),
        published_on=date.fromisoformat(str(record["published_on"])),
        updated_on=date.fromisoformat(str(record["updated_on"])),
    )


def _index_paper(
    paper: Paper,
    writer: IndexWriter,
    tokenizer: BgeM3Tokenizer,
    llm: LlmClient,
    embedder: OllamaEmbedder,
    fingerprint: str,
    *,
    force: bool,
) -> IndexedPaper | None:
    cache = ArxivCache(CACHE_ROOT)
    html = cache.read_html(paper.external_id, paper.version)
    if html is None:
        raise ValueError("HTML is not cached; run `indexer fetch` first")
    parsed = parse_html(html)
    if parsed is None:
        raise ValueError("cached page is not LaTeXML article HTML")
    document_hash = content_hash(
        arxiv_version=paper.version,
        title=paper.title,
        abstract=paper.abstract,
        raw_text=parsed.raw_text,
    )
    if not force and writer.is_unchanged(paper.external_id, document_hash, fingerprint):
        return None
    chunks = chunk_sections(parsed.sections, tokenizer)
    if not chunks:
        raise ValueError("document produced no chunks")
    context_summary = summarize_document(
        llm,
        title=paper.title,
        categories=paper.categories,
        abstract=paper.abstract,
        section_headings=(section.path for section in parsed.sections),
    )
    headers = tuple(
        context_header(
            paper.title,
            paper.primary_category,
            paper.published_on,
            chunk.section_path,
            context_summary,
        )
        for chunk in chunks
    )
    content_embeddings = embedder.embed([chunk.content for chunk in chunks])
    embeddings = tuple(tuple(vector) for vector in content_embeddings)
    contextual_embeddings = tuple(
        tuple(vector)
        for vector in embedder.embed(
            [f"{header}\n\n{chunk.content}" for header, chunk in zip(headers, chunks, strict=True)]
        )
    )
    return IndexedPaper(
        paper=paper,
        parsed=parsed,
        chunks=chunks,
        context_summary=context_summary,
        context_headers=headers,
        embeddings=embeddings,
        contextual_embeddings=contextual_embeddings,
        content_hash=document_hash,
        pipeline_fingerprint=fingerprint,
    )


def _llm_digest(settings: Settings, ollama_digest: str) -> str:
    return (
        (settings.anthropic_model or "") if settings.llm_provider == "anthropic" else ollama_digest
    )


def _metadata(
    settings: Settings, embedding_digest: str, llm_digest: str, fingerprint: str
) -> dict[str, str]:
    values = {
        "embedding_model": settings.ollama_embed_model,
        "embedding_model_digest": embedding_digest,
        "llm_model": (
            f"anthropic:{settings.anthropic_model}"
            if settings.llm_provider == "anthropic"
            else settings.ollama_llm_model
        ),
        "llm_model_digest": llm_digest,
        "chunk_size_tokens": "384",
        "chunk_overlap_tokens": "64",
        "min_chunk_tokens": "64",
        "header_prompt_version": PROMPT_VERSION,
        "parser_version": "html-v1",
        "pipeline_fingerprint": fingerprint,
    }
    if len(values) != len(INDEX_METADATA_KEYS) - 3:
        raise AssertionError("index metadata contract changed")
    return values


if __name__ == "__main__":
    app()
