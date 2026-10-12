"""WireMock cassette recording (design D9 / contract C9)."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from evals.dataset.schema import sha256_file

SNAPSHOT_REQUEST = {
    "persist": False,
    "repeatsAsScenarios": False,
    "requestBodyPattern": {
        "matcher": "equalToJson",
        "ignoreArrayOrder": False,
        "ignoreExtraElements": False,
    },
}
RERECORD_HINT = "re-record with `make eval-record-ci`"


class CassetteError(Exception):
    """Recording failed (CLI exit code 3)."""


def _is_proxy(mapping: Mapping[str, Any]) -> bool:
    return "proxyBaseUrl" in (mapping.get("response") or {})


def snapshot_cassettes(
    *,
    wiremock_url: str,
    cassette_dir: Path,
    git_sha: str,
    model_digests: Mapping[str, str | None],
    reranker: Mapping[str, Any],
    snapshot_sha256: str,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Capture the recorded stubs from wiremock into `<cassette_dir>/mappings/` (replacing the
    previous ones) and write `<cassette_dir>/meta.json`. Returns the meta dict."""
    http = client or httpx.Client(timeout=60.0)
    try:
        resp = http.post(
            f"{wiremock_url.rstrip('/')}/__admin/recordings/snapshot", json=SNAPSHOT_REQUEST
        )
        resp.raise_for_status()
        mappings = [m for m in resp.json().get("mappings", []) if not _is_proxy(m)]
    except (httpx.HTTPError, ValueError) as exc:
        raise CassetteError(f"wiremock snapshot failed: {exc}") from exc
    if not mappings:
        raise CassetteError(
            "wiremock recorded no traffic; was the grid run against the recording proxy?"
        )

    mappings_dir = cassette_dir / "mappings"
    mappings_dir.mkdir(parents=True, exist_ok=True)
    for old in mappings_dir.glob("*.json"):
        old.unlink()
    for i, mapping in enumerate(mappings):
        stub_id = mapping.get("id") or mapping.get("uuid") or f"stub-{i:05d}"
        (mappings_dir / f"{stub_id}.json").write_text(
            json.dumps(mapping, indent=2) + "\n", encoding="utf-8"
        )

    meta = {
        "recorded_at": now().isoformat(),
        "git_sha": git_sha,
        "model_digests": dict(model_digests),
        "reranker_model": reranker.get("model"),
        "reranker_revision": reranker.get("revision"),
        "snapshot_sha256": snapshot_sha256,
        "mapping_count": len(mappings),
    }
    (cassette_dir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    return meta


def snapshot_sha_from_lock(lock_path: Path) -> str:
    try:
        return json.loads(lock_path.read_text(encoding="utf-8"))["sha256"]
    except (OSError, ValueError, KeyError) as exc:
        raise CassetteError(
            f"cannot read the snapshot sha256 from {lock_path} ({exc}); run `evals ci-lock` first "
            "or pass --snapshot <path>"
        ) from exc


def snapshot_sha_from_file(path: Path) -> str:
    try:
        return sha256_file(path)
    except OSError as exc:
        raise CassetteError(f"cannot read snapshot {path}: {exc}") from exc
