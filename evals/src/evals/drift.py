"""Drift guards (eval-runner spec): model digests vs the lock file, gold chunk hashes vs the DB."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Protocol

from evals.dataset.schema import Record


class GoldStore(Protocol):
    """Read access to `(external_id, ordinal) -> content_sha256` for the indexed corpus."""

    def content_hashes(self, keys: Sequence[tuple[str, int]]) -> dict[tuple[str, int], str]: ...


class PostgresGoldStore:
    """The real store: one query over all gold pairs as `rag_reader`."""

    def __init__(self, dsn: str):
        self.dsn = dsn

    def content_hashes(self, keys: Sequence[tuple[str, int]]) -> dict[tuple[str, int], str]:
        import psycopg

        if not keys:
            return {}
        ext = [k[0] for k in keys]
        ords = [k[1] for k in keys]
        sql = (
            "select d.external_id, c.ordinal, c.content_sha256 "
            "from unnest(%s::text[], %s::int[]) as g(external_id, ordinal) "
            "join documents d on d.external_id = g.external_id "
            "join chunks c on c.document_id = d.id and c.ordinal = g.ordinal"
        )
        with psycopg.connect(self.dsn) as conn, conn.cursor() as cur:
            cur.execute(sql, (ext, ords))
            return {(r[0], r[1]): r[2] for r in cur.fetchall()}


def load_models_lock(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def lock_digests(lock: Mapping) -> dict[str, str]:
    return {name: entry["digest"] for name, entry in lock.get("ollama", {}).items()}


def digest_mismatches(live: Mapping[str, str | None], lock: Mapping[str, str]) -> list[str]:
    """Human-readable mismatches between live (or recorded) digests and the lock file."""
    out = []
    for name, expected in lock.items():
        actual = live.get(name)
        if actual is None:
            out.append(f"{name}: not found (lock expects {expected[:12]}…)")
        elif actual != expected:
            out.append(f"{name}: digest {actual[:12]}… != lock {expected[:12]}…")
    return out


def gold_drift(records: Iterable[Record], store: GoldStore) -> dict[str, list[str]]:
    """Map question id -> reasons for every question whose gold chunk is missing from the DB or
    whose `content_sha256` differs from the dataset."""
    records = [r for r in records if r.gold_chunks]
    keys = sorted({g.key for r in records for g in r.gold_chunks})
    actual = store.content_hashes(keys)
    drift: dict[str, list[str]] = {}
    for r in records:
        reasons = []
        for g in r.gold_chunks:
            sha = actual.get(g.key)
            if sha is None:
                reasons.append(f"{g.external_id}#{g.ordinal} missing from the database")
            elif sha != g.content_sha256:
                reasons.append(f"{g.external_id}#{g.ordinal} content hash differs")
        if reasons:
            drift[r.id] = reasons
    return drift
