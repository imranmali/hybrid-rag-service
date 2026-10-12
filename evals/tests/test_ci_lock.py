from __future__ import annotations

import hashlib
import json

import pytest

from evals import ci_lock

FP = "0123456789ab" + "c" * 52


class FakeStats:
    def __init__(self, fingerprint=FP):
        self._fp = fingerprint

    def pipeline_fingerprint(self):
        return self._fp

    def document_count(self):
        return 212

    def chunk_count(self):
        return 5480


@pytest.fixture
def dump(tmp_path):
    path = tmp_path / "rag-index-0123456789ab.dump"
    path.write_bytes(b"PGDMP fake snapshot bytes")
    return path


def test_lock_sha256_equals_the_snapshot_files_sha256(dump, tmp_path):
    out = tmp_path / "ci" / "snapshot.lock.json"
    lock = ci_lock.write_lock(dump, out, FakeStats())
    assert json.loads(out.read_text()) == lock
    assert lock["sha256"] == hashlib.sha256(dump.read_bytes()).hexdigest()


def test_lock_matches_contract_c9_shape(dump, tmp_path):
    lock = ci_lock.write_lock(dump, tmp_path / "l.json", FakeStats())
    assert lock == {
        "release_tag": "index-snapshot-0123456789ab",
        "asset_name": "rag-index-0123456789ab.dump",
        "sha256": hashlib.sha256(dump.read_bytes()).hexdigest(),
        "pipeline_fingerprint": FP,
        "document_count": 212,
        "chunk_count": 5480,
    }


def test_explicit_release_tag(dump, tmp_path):
    assert ci_lock.write_lock(dump, tmp_path / "l.json", FakeStats(), "index-snapshot-custom")[
        "release_tag"
    ] == ("index-snapshot-custom")


def test_missing_snapshot_and_bad_name(tmp_path):
    with pytest.raises(ci_lock.LockError, match="not found"):
        ci_lock.build_lock(tmp_path / "rag-index-0123456789ab.dump", FakeStats())
    odd = tmp_path / "dump.bin"
    odd.write_bytes(b"x")
    with pytest.raises(ci_lock.LockError, match="rag-index"):
        ci_lock.build_lock(odd, FakeStats())


def test_snapshot_from_a_different_index_is_rejected(dump):
    with pytest.raises(ci_lock.LockError, match="does not match"):
        ci_lock.build_lock(dump, FakeStats(fingerprint="f" * 64))
