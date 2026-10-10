"""Temporary-table ANN benchmark used by the HNSW/filter-pushdown ADRs."""

import json
import math
import random
import statistics
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
from pgvector.psycopg import register_vector

FILTERED_EF_SEARCH = 40  # pgvector's default, i.e. what an unconfigured service session runs


def recall_at_k(exact_ids: list[int], approximate_ids: list[int], k: int = 10) -> float:
    """Recall of the exact top-k ids retained by an approximate result."""
    expected = set(exact_ids[:k])
    return len(expected.intersection(approximate_ids[:k])) / len(expected) if expected else 1.0


def run_benchmark(database_url: str, *, queries: int = 200, seed: int = 42) -> dict[str, Any]:
    """Build only temporary indexes and return the D11 measurements."""
    with psycopg.connect(database_url) as connection:
        register_vector(connection)
        with connection.cursor() as cursor:
            cursor.execute(
                """
                create temp table bench_vec as
                select c.id, c.embedding, d.primary_category,
                       extract(year from d.published_on)::int as year
                from chunks c join documents d on d.id = c.document_id
                """
            )
            cursor.execute("analyze bench_vec")
            row_count = cursor.execute("select count(*) from bench_vec").fetchone()[0]
            if row_count < 2:
                raise ValueError("benchmark requires at least two chunks")
            samples = cursor.execute(
                "select id, embedding, primary_category, year from bench_vec order by id"
            ).fetchall()
            rng = random.Random(seed)
            sample = rng.sample(samples, min(queries, len(samples)))
            hnsw_seconds = _time_index(
                cursor,
                "create index bench_hnsw on bench_vec using hnsw "
                "(embedding vector_cosine_ops) with (m = 16, ef_construction = 64)",
            )
            hnsw = {
                str(setting): _measure_ann(cursor, sample, "hnsw.ef_search", setting)
                for setting in (40, 100, 250)
            }
            filtered = _measure_filtered(cursor, sample, row_count)
            cursor.execute("drop index bench_hnsw")
            lists = max(1, round(math.sqrt(row_count)))
            ivf_seconds = _time_index(
                cursor,
                "create index bench_ivfflat on bench_vec using ivfflat "
                f"(embedding vector_cosine_ops) with (lists = {lists})",
            )
            ivfflat = {
                str(setting): _measure_ann(cursor, sample, "ivfflat.probes", setting)
                for setting in (1, 5, 10)
            }
    return {
        "timestamp": datetime.now(UTC).isoformat(),
        "row_count": row_count,
        "build_seconds": {"hnsw": hnsw_seconds, "ivfflat": ivf_seconds},
        "hnsw": hnsw,
        "ivfflat": ivfflat,
        "filtered_hnsw": filtered,
    }


def write_results(results: dict[str, Any], directory: Path) -> tuple[Path, Path]:
    """Write the committed JSON and reviewer-friendly Markdown result artifacts."""
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / "index_bench.json"
    markdown_path = directory / "index_bench.md"
    json_path.write_text(json.dumps(results, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "# Index benchmark",
        "",
        f"Timestamp: {results['timestamp']}",
        f"Chunk rows: {results['row_count']}",
        "",
        "## Build time",
        "",
        "| Index | Seconds |",
        "| --- | ---: |",
        f"| HNSW | {results['build_seconds']['hnsw']:.3f} |",
        f"| IVFFlat | {results['build_seconds']['ivfflat']:.3f} |",
        "",
        "## Unfiltered recall",
        "",
        "| Index | Setting | Recall@10 | p50 ms | p95 ms |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    indexes = (
        ("HNSW ef_search", results["hnsw"]),
        ("IVFFlat probes", results["ivfflat"]),
    )
    for index_name, settings in indexes:
        for setting, measurement in settings.items():
            lines.append(
                f"| {index_name} | {setting} | {measurement['recall_at_10']:.3f} | "
                f"{measurement['p50_ms']:.2f} | {measurement['p95_ms']:.2f} |"
            )
    lines.extend(
        [
            "",
            f"## Filtered HNSW recall (ef_search = {FILTERED_EF_SEARCH}, post-filter)",
            "",
            "| Filter | Selectivity | Recall@10 |",
            "| --- | ---: | ---: |",
        ]
    )
    for item in results["filtered_hnsw"]:
        lines.append(
            f"| {item['filter']} | {item['selectivity']:.3f} | {item['recall_at_10']:.3f} |"
        )
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, markdown_path


def _time_index(cursor: psycopg.Cursor[Any], statement: str) -> float:
    started = time.perf_counter()
    cursor.execute(statement)
    return time.perf_counter() - started


def _measure_ann(
    cursor: psycopg.Cursor[Any], sample: list[tuple[Any, ...]], setting_name: str, setting: int
) -> dict[str, float]:
    recalls: list[float] = []
    latencies: list[float] = []
    for query_id, embedding, *_ in sample:
        exact = _nearest(cursor, query_id, embedding, exact=True)
        cursor.execute("set local enable_indexscan = on")
        cursor.execute("set local enable_bitmapscan = on")
        cursor.execute("set local enable_seqscan = off")
        started = time.perf_counter()
        cursor.execute(f"set local {setting_name} = {setting}")
        approximate = _nearest(cursor, query_id, embedding, exact=False)
        latencies.append((time.perf_counter() - started) * 1000)
        recalls.append(recall_at_k(exact, approximate))
    return {
        "recall_at_10": statistics.fmean(recalls),
        "p50_ms": _percentile(latencies, 0.5),
        "p95_ms": _percentile(latencies, 0.95),
    }


def _measure_filtered(
    cursor: psycopg.Cursor[Any], sample: list[tuple[Any, ...]], row_count: int
) -> list[dict[str, float | str]]:
    category_rows = cursor.execute(
        "select distinct primary_category from bench_vec order by 1"
    ).fetchall()
    year_rows = cursor.execute("select distinct year from bench_vec order by 1").fetchall()
    values = [
        *[("primary_category", value) for (value,) in category_rows],
        *[("year", value) for (value,) in year_rows],
    ]
    measurements: list[dict[str, float | str]] = []
    # Pin the setting: set local otherwise carries over the last unfiltered ef_search (250).
    cursor.execute(f"set local hnsw.ef_search = {FILTERED_EF_SEARCH}")
    for column, value in values:
        count = cursor.execute(
            f"select count(*) from bench_vec where {column} = %s", (value,)
        ).fetchone()[0]
        rows = [row for row in sample if row[2 if column == "primary_category" else 3] == value]
        if not rows:
            rows = cursor.execute(
                f"select id, embedding, primary_category, year "
                f"from bench_vec where {column} = %s limit 20",
                (value,),
            ).fetchall()
        recalls = []
        for query_id, embedding, *_ in rows:
            exact = _nearest(cursor, query_id, embedding, exact=True, column=column, value=value)
            cursor.execute("set local enable_indexscan = on")
            cursor.execute("set local enable_bitmapscan = on")
            cursor.execute("set local enable_seqscan = off")
            cursor.execute("set local hnsw.iterative_scan = off")
            approximate = _nearest(
                cursor, query_id, embedding, exact=False, column=column, value=value
            )
            recalls.append(recall_at_k(exact, approximate))
        measurements.append(
            {
                "filter": f"{'category' if column == 'primary_category' else column}={value}",
                "selectivity": count / row_count,
                "recall_at_10": statistics.fmean(recalls) if recalls else 1.0,
            }
        )
    return measurements


def _nearest(
    cursor: psycopg.Cursor[Any],
    query_id: int,
    embedding: Any,
    *,
    exact: bool,
    column: str | None = None,
    value: Any | None = None,
) -> list[int]:
    if exact:
        cursor.execute("set local enable_seqscan = on")
        cursor.execute("set local enable_indexscan = off")
        cursor.execute("set local enable_bitmapscan = off")
    where = "id <> %s"
    parameters: list[Any] = [query_id]
    if column is not None:
        where += f" and {column} = %s"
        parameters.append(value)
    parameters.append(embedding)
    return [
        row[0]
        for row in cursor.execute(
            f"select id from bench_vec where {where} order by embedding <=> %s, id limit 10",
            parameters,
        ).fetchall()
    ]


def _percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    return ordered[min(len(ordered) - 1, math.ceil(len(ordered) * quantile) - 1)]
