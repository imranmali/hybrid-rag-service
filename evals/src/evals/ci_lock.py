"""`evals/ci/snapshot.lock.json`: describes the DB snapshot the cassettes were recorded against."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Protocol

from evals.dataset.schema import sha256_file

_ASSET = re.compile(r"^rag-index-([0-9a-f]{12})\.dump$")


class LockError(Exception):
    """Cannot build the lock file (CLI exit code 3)."""


class CorpusStats(Protocol):
    def pipeline_fingerprint(self) -> str: ...
    def document_count(self) -> int: ...
    def chunk_count(self) -> int: ...


class PostgresCorpusStats:
    def __init__(self, dsn: str):
        self.dsn = dsn

    def _scalar(self, sql: str) -> object:
        import psycopg

        with psycopg.connect(self.dsn) as conn, conn.cursor() as cur:
            cur.execute(sql)
            row = cur.fetchone()
            return row[0] if row else None

    def pipeline_fingerprint(self) -> str:
        value = self._scalar("select value from index_metadata where key = 'pipeline_fingerprint'")
        if not value:
            raise LockError("index_metadata has no 'pipeline_fingerprint'; run `make index` first")
        return str(value)

    def document_count(self) -> int:
        return int(self._scalar("select count(*) from documents"))  # type: ignore[arg-type]

    def chunk_count(self) -> int:
        return int(self._scalar("select count(*) from chunks"))  # type: ignore[arg-type]


def build_lock(snapshot: Path, stats: CorpusStats, release_tag: str | None = None) -> dict:
    if not snapshot.is_file():
        raise LockError(f"snapshot not found: {snapshot}")
    m = _ASSET.match(snapshot.name)
    if not m:
        raise LockError(f"snapshot name must look like rag-index-<12hex>.dump, got {snapshot.name}")
    fingerprint = stats.pipeline_fingerprint()
    if not fingerprint.startswith(m.group(1)):
        raise LockError(
            f"snapshot name says fingerprint {m.group(1)} but the database has {fingerprint[:12]}; "
            "the snapshot does not match the indexed corpus"
        )
    return {
        "release_tag": release_tag or f"index-snapshot-{m.group(1)}",
        "asset_name": snapshot.name,
        "sha256": sha256_file(snapshot),
        "pipeline_fingerprint": fingerprint,
        "document_count": stats.document_count(),
        "chunk_count": stats.chunk_count(),
    }


def write_lock(
    snapshot: Path, out_path: Path, stats: CorpusStats, release_tag: str | None = None
) -> dict:
    lock = build_lock(snapshot, stats, release_tag)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8")
    return lock
