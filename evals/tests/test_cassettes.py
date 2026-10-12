from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest
import respx

from evals import cassettes

WM = "http://wiremock.test:8089"
NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)


def stub(stub_id, url="/api/embed", body=None):
    return {
        "id": stub_id,
        "request": {
            "url": url,
            "method": "POST",
            "bodyPatterns": [
                {"equalToJson": json.dumps(body or {"input": [stub_id]}), "ignoreArrayOrder": False}
            ],
        },
        "response": {"status": 200, "body": json.dumps({"embeddings": [[0.1, 0.2]]})},
    }


def proxy(stub_id):
    return {
        "id": stub_id,
        "request": {"urlPattern": "/api/.*"},
        "response": {"proxyBaseUrl": "http://host"},
    }


def snapshot(tmp_path, mappings, **kw):
    return cassettes.snapshot_cassettes(
        wiremock_url=WM,
        cassette_dir=tmp_path / "cassettes" / "ci",
        git_sha="abc1234",
        model_digests={"bge-m3": "a" * 64, "llama3.1:8b": "b" * 64},
        reranker={"model": "BAAI/bge-reranker-v2-m3", "revision": "r" * 40},
        snapshot_sha256="s" * 64,
        now=lambda: NOW,
        **kw,
    )


@respx.mock
def test_snapshot_writes_mappings_and_meta(tmp_path):
    route = respx.post(f"{WM}/__admin/recordings/snapshot").mock(
        return_value=httpx.Response(
            200, json={"mappings": [stub("id-1"), stub("id-2", "/rerank")], "meta": {"total": 2}}
        )
    )
    meta = snapshot(tmp_path, None)
    d = tmp_path / "cassettes" / "ci"
    assert sorted(p.name for p in (d / "mappings").glob("*.json")) == ["id-1.json", "id-2.json"]
    assert json.loads((d / "mappings" / "id-2.json").read_text())["request"]["url"] == "/rerank"
    on_disk = json.loads((d / "meta.json").read_text())
    assert on_disk == meta
    assert set(on_disk) >= {
        "recorded_at", "git_sha", "model_digests", "reranker_model", "reranker_revision", "snapshot_sha256",
    }  # fmt: skip
    assert on_disk["git_sha"] == "abc1234" and on_disk["snapshot_sha256"] == "s" * 64
    assert on_disk["model_digests"] == {"bge-m3": "a" * 64, "llama3.1:8b": "b" * 64}
    assert on_disk["reranker_revision"] == "r" * 40 and on_disk["recorded_at"] == NOW.isoformat()
    # the exact snapshot request of contract C9: exact JSON body matching, nothing persisted by wiremock
    sent = json.loads(route.calls.last.request.content)
    assert sent == {
        "persist": False,
        "repeatsAsScenarios": False,
        "requestBodyPattern": {
            "matcher": "equalToJson",
            "ignoreArrayOrder": False,
            "ignoreExtraElements": False,
        },
    }


@respx.mock
def test_old_mappings_are_removed_and_other_files_kept(tmp_path):
    d = tmp_path / "cassettes" / "ci" / "mappings"
    d.mkdir(parents=True)
    (d / "stale.json").write_text("{}")
    (d.parent / "README.md").write_text("keep")
    respx.post(f"{WM}/__admin/recordings/snapshot").mock(
        return_value=httpx.Response(200, json={"mappings": [stub("fresh")]})
    )
    snapshot(tmp_path, None)
    assert [p.name for p in d.glob("*.json")] == ["fresh.json"]
    assert (d.parent / "README.md").read_text() == "keep"


@respx.mock
def test_proxy_mappings_are_excluded(tmp_path):
    respx.post(f"{WM}/__admin/recordings/snapshot").mock(
        return_value=httpx.Response(200, json={"mappings": [proxy("p1"), stub("s1")]})
    )
    meta = snapshot(tmp_path, None)
    assert [p.name for p in (tmp_path / "cassettes" / "ci" / "mappings").glob("*.json")] == [
        "s1.json"
    ]
    assert meta["mapping_count"] == 1


@respx.mock
def test_empty_recording_fails_loudly_without_touching_existing_cassettes(tmp_path):
    d = tmp_path / "cassettes" / "ci" / "mappings"
    d.mkdir(parents=True)
    (d / "old.json").write_text("{}")
    respx.post(f"{WM}/__admin/recordings/snapshot").mock(
        return_value=httpx.Response(200, json={"mappings": []})
    )
    with pytest.raises(cassettes.CassetteError, match="no traffic"):
        snapshot(tmp_path, None)
    assert (d / "old.json").exists()


@respx.mock
def test_wiremock_failure_is_a_cassette_error(tmp_path):
    respx.post(f"{WM}/__admin/recordings/snapshot").mock(return_value=httpx.Response(500))
    with pytest.raises(cassettes.CassetteError):
        snapshot(tmp_path, None)


def test_snapshot_sha_sources(tmp_path):
    lock = tmp_path / "snapshot.lock.json"
    lock.write_text(json.dumps({"sha256": "c" * 64}))
    assert cassettes.snapshot_sha_from_lock(lock) == "c" * 64
    with pytest.raises(cassettes.CassetteError, match="ci-lock"):
        cassettes.snapshot_sha_from_lock(tmp_path / "missing.json")
    dump = tmp_path / "rag-index-aaaaaaaaaaaa.dump"
    dump.write_bytes(b"data")
    assert len(cassettes.snapshot_sha_from_file(dump)) == 64
