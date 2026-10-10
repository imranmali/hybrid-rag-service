from __future__ import annotations

import json
from copy import deepcopy

import pytest
from conftest import full_dataset_records, make_record, write_jsonl
from typer.testing import CliRunner

from evals.cli import app
from evals.dataset import schema
from evals.dataset.schema import sha256_file, validate_dataset


def _manifest(tmp_path, data_path, counts=None):
    m = tmp_path / "MANIFEST.json"
    m.write_text(json.dumps({"sha256": sha256_file(data_path), "counts": counts or schema.QUOTA}))
    return m


def test_full_valid_dataset_passes(tmp_path, tmp_dataset):
    path = tmp_dataset(full_dataset_records())
    report = validate_dataset(path, _manifest(tmp_path, path))
    assert report.ok, report.errors
    assert len(report.records) == 150
    assert report.counts == schema.QUOTA
    assert report.ci_counts == schema.CI_QUOTA


def test_multi_chunk_with_one_gold_fails_naming_the_id(tmp_dataset):
    rows = [make_record("q0001", "multi_chunk", gold=[("2401.00001", 0)])]
    report = validate_dataset(tmp_dataset(rows), quota=None, ci_quota=None)
    assert not report.ok
    assert any(e.startswith("q0001") and "multi_chunk needs 2-4" in e for e in report.errors)


def test_multi_chunk_with_five_gold_fails(tmp_dataset):
    rows = [make_record("q0001", "multi_chunk", gold=[("2401.00001", i) for i in range(5)])]
    report = validate_dataset(tmp_dataset(rows), quota=None, ci_quota=None)
    assert any("q0001" in e and "2-4" in e for e in report.errors)


def test_aggregate_without_sql_answer_fails(tmp_dataset):
    rows = [make_record("q0007", "aggregate", expected_sql_answer=None)]
    report = validate_dataset(tmp_dataset(rows), quota=None, ci_quota=None)
    assert any(e.startswith("q0007") and "expected_sql_answer" in e for e in report.errors)


@pytest.mark.parametrize(
    ("qtype", "overrides", "fragment"),
    [
        ("factoid", {"gold_chunks": []}, "at least 1 gold"),
        ("filter", {"gold_chunks": []}, "at least 1 gold"),
        (
            "unanswerable",
            {"gold_chunks": [{"external_id": "x", "ordinal": 0, "content_sha256": "a" * 64}]},
            "no gold",
        ),
        ("factoid", {"expected_route": "sql"}, "expected_route"),
        ("aggregate", {"expected_route": "vector"}, "expected_route"),
        ("factoid", {"expected_sql_answer": "3"}, "only allowed for aggregate"),
        ("filter", {"expected_filters": None}, "filter needs expected_filters"),
        ("factoid", {"expected_filters": {"authors": ["x"]}}, "only allowed for filter"),
    ],
)
def test_gold_and_route_rules(tmp_dataset, qtype, overrides, fragment):
    rows = [make_record("q0001", qtype, **overrides)]
    report = validate_dataset(tmp_dataset(rows), quota=None, ci_quota=None)
    assert any("q0001" in e and fragment in e for e in report.errors), report.errors


def test_bad_hash_and_unknown_field_fail_schema(tmp_dataset):
    bad_hash = make_record("q0001")
    bad_hash["gold_chunks"][0]["content_sha256"] = "nothex"
    extra = make_record("q0002", surprise=1)
    report = validate_dataset(tmp_dataset([bad_hash, extra]), quota=None, ci_quota=None)
    assert any("q0001" in e and "content_sha256" in e for e in report.errors)
    assert any("q0002" in e and "surprise" in e for e in report.errors)


def test_duplicate_ids_and_bad_id_format(tmp_dataset):
    rows = [make_record("q0001"), make_record("q0001"), make_record("bogus")]
    report = validate_dataset(tmp_dataset(rows), quota=None, ci_quota=None)
    assert any("q0001: duplicate id" in e for e in report.errors)
    assert any("bogus" in e and "id" in e for e in report.errors)


def test_invalid_json_line_is_reported(tmp_path):
    path = tmp_path / "q.jsonl"
    path.write_text('{"id": \n')
    report = validate_dataset(path, quota=None, ci_quota=None)
    assert any("line 1" in e and "invalid JSON" in e for e in report.errors)


def test_wrong_counts_fail(tmp_dataset):
    rows = full_dataset_records()[:-1]  # 149 records, one unanswerable short
    report = validate_dataset(tmp_dataset(rows))
    assert any("type counts" in e for e in report.errors)


def test_wrong_ci_split_fails(tmp_dataset):
    rows = full_dataset_records()
    rows[0]["ci_subset"] = False
    report = validate_dataset(tmp_dataset(rows))
    assert any("ci_subset counts" in e for e in report.errors)


def test_manifest_hash_mismatch_fails(tmp_path, tmp_dataset):
    path = tmp_dataset(full_dataset_records())
    manifest = _manifest(tmp_path, path)
    path.write_text(path.read_text() + "\n")  # content hash changes after the manifest was written
    report = validate_dataset(path, manifest)
    assert any("manifest sha256" in e for e in report.errors)


def test_manifest_counts_mismatch_fails(tmp_path, tmp_dataset):
    path = tmp_dataset(full_dataset_records())
    manifest = _manifest(tmp_path, path, counts={**schema.QUOTA, "factoid": 59})
    assert any("manifest counts" in e for e in validate_dataset(path, manifest).errors)


def test_missing_manifest_fails(tmp_path, tmp_dataset):
    path = tmp_dataset(full_dataset_records())
    report = validate_dataset(path, tmp_path / "nope.json")
    assert any("manifest not found" in e for e in report.errors)


def test_load_dataset_raises_with_all_problems(tmp_dataset):
    rows = [make_record("q0001", "multi_chunk", gold=[("2401.00001", 0)])]
    with pytest.raises(ValueError, match="q0001"):
        schema.load_dataset(tmp_dataset(rows))


# --- CLI: `evals dataset validate` ---------------------------------------------------------


def test_cli_validate_ok_and_exit_codes(tmp_path):
    path = write_jsonl(tmp_path / "q.jsonl", full_dataset_records())
    manifest = _manifest(tmp_path, path)
    runner = CliRunner()
    ok = runner.invoke(
        app, ["dataset", "validate", "--path", str(path), "--manifest", str(manifest)]
    )
    assert ok.exit_code == 0, ok.output
    assert "150 valid records" in ok.output

    rows = full_dataset_records()
    rows[3] = deepcopy(rows[3])
    rows[3]["gold_chunks"] = []
    bad = write_jsonl(tmp_path / "bad.jsonl", rows)
    res = runner.invoke(
        app,
        ["dataset", "validate", "--path", str(bad), "--manifest", str(_manifest(tmp_path, bad))],
    )
    assert res.exit_code == 3
    assert "q0004" in res.output
