from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from conftest import sha
from fakes import FakeGoldStore
from typer.testing import CliRunner

from evals import cli
from evals.dataset import freeze
from evals.dataset.schema import validate_dataset

QUOTA = {"factoid": 4, "multi_chunk": 3, "filter": 2, "aggregate": 2, "unanswerable": 2}
CI = {"factoid": 2, "multi_chunk": 1, "filter": 1, "aggregate": 1, "unanswerable": 1}


def candidate(ctype, i):
    gold = {
        "factoid": [("2401.00001", i)],
        "multi_chunk": [("2401.00001", i), ("2401.00002", i)],
        "filter": [("2401.00003", i)],
        "aggregate": [],
        "unanswerable": [],
    }[ctype]
    return {
        "candidate_id": f"cand-{ctype}-{i:04d}",
        "sample_id": None,
        "type": ctype,
        "question": f"{ctype} question {i}?",
        "reference_answer": f"answer {i}",
        "gold_chunks": [{"external_id": e, "ordinal": o, "content_sha256": sha(f"{e}#{o}")} for e, o in gold],
        "expected_route": "sql" if ctype == "aggregate" else "vector",
        "expected_filters": {"authors": ["a"]} if ctype == "filter" else None,
        "expected_sql_answer": "3" if ctype == "aggregate" else None,
        "source": {"generator": "ollama:llama3.1:8b" if ctype != "aggregate" else "sql-template:T1", "seed": 42,
                   "sampled_chunks": []},
    }  # fmt: skip


def inputs(extra=0, accept=True):
    cands, pre, ver = [], [], []
    for ctype, n in QUOTA.items():
        for i in range(n + extra):
            c = candidate(ctype, i)
            cands.append(c)
            pre.append(
                {"candidate_id": c["candidate_id"], "passed": True, "model": "ollama:llama3.1:8b"}
            )
            if accept:
                ver.append(
                    {
                        "candidate_id": c["candidate_id"],
                        "status": "accept",
                        "by": "agent:claude-test",
                        "notes": "ok",
                    }
                )
    return freeze.FreezeInputs(cands, pre, ver, "d" * 64, "f" * 64)


def do_freeze(tmp_path, inp, **kw):
    return freeze.freeze(
        inp,
        FakeGoldStore(),
        dataset_dir=tmp_path,
        quota=QUOTA,
        ci_quota=CI,
        now=lambda: datetime(2026, 10, 10, tzinfo=UTC),
        **kw,
    )


def test_freeze_writes_dataset_manifest_card_and_labels(tmp_path):
    manifest = do_freeze(tmp_path, inputs(extra=2))
    report = validate_dataset(
        tmp_path / "questions.v1.jsonl", tmp_path / "MANIFEST.json", quota=QUOTA, ci_quota=CI
    )
    assert report.ok, report.errors
    assert report.counts == QUOTA and report.ci_counts == CI
    assert manifest["counts"] == QUOTA and manifest["ci_counts"] == CI
    assert manifest["sha256"] == report.sha256 and manifest["seed"] == 42
    assert (
        manifest["generator"] == "ollama:llama3.1:8b" and manifest["generator_digest"] == "d" * 64
    )
    assert (
        manifest["verifier"] == "agent:claude-test"
        and manifest["index_pipeline_fingerprint"] == "f" * 64
    )
    assert json.loads((tmp_path / "MANIFEST.json").read_text()) == manifest


def test_ids_follow_type_order_and_every_record_is_agent_verified_not_human(tmp_path):
    do_freeze(tmp_path, inputs(extra=1))
    recs = [json.loads(ln) for ln in (tmp_path / "questions.v1.jsonl").read_text().splitlines()]
    assert [r["id"] for r in recs] == [f"q{i:04d}" for i in range(1, 14)]
    assert [r["type"] for r in recs] == ["factoid"] * 4 + ["multi_chunk"] * 3 + ["filter"] * 2 + [
        "aggregate"
    ] * 2 + ["unanswerable"] * 2
    assert all(r["verification"]["human_reviewed"] is False for r in recs)
    assert all(r["verification"]["by"] == "agent:claude-test" for r in recs)
    assert all(r["expected_filters"] is None for r in recs if r["type"] != "filter")


def test_freeze_is_deterministic_for_a_seed_and_seed_sensitive(tmp_path):
    a, b, c = tmp_path / "a", tmp_path / "b", tmp_path / "c"
    do_freeze(a, inputs(extra=3))
    do_freeze(b, inputs(extra=3))
    assert (a / "questions.v1.jsonl").read_text() == (b / "questions.v1.jsonl").read_text()
    do_freeze(c, inputs(extra=3), seed=7)
    assert (a / "questions.v1.jsonl").read_text() != (c / "questions.v1.jsonl").read_text()


def test_unverified_rejected_and_failed_precheck_candidates_are_excluded(tmp_path):
    inp = inputs(extra=3)
    by_id = {v["candidate_id"]: v for v in inp.verdicts}
    del by_id["cand-factoid-0000"]  # no verdict
    inp.verdicts = [
        *by_id.values(),
        {"candidate_id": "cand-factoid-0001", "status": "reject", "by": "agent:x", "notes": "bad"},
    ]
    next(p for p in inp.prechecks if p["candidate_id"] == "cand-factoid-0002")["passed"] = False
    do_freeze(tmp_path, inp)
    ids = {
        json.loads(ln)["question"]
        for ln in (tmp_path / "questions.v1.jsonl").read_text().splitlines()
    }
    factoids = {i for i in ids if i.startswith("factoid")}
    assert factoids == {
        f"factoid question {i}?" for i in (3, 4, 5, 6)
    }  # 0 unverified, 1 rejected, 2 precheck


def test_shortfall_names_the_type_and_the_generate_command(tmp_path):
    inp = inputs()
    inp.verdicts = [v for v in inp.verdicts if v["candidate_id"] != "cand-filter-0000"]
    with pytest.raises(freeze.FreezeError) as exc:
        do_freeze(tmp_path, inp)
    assert "filter: 1 accepted of 2" in str(exc.value)
    assert "evals dataset generate --type filter" in str(exc.value)
    assert not (tmp_path / "questions.v1.jsonl").exists()


def test_edited_candidates_use_the_edited_text_and_note_the_edit(tmp_path):
    inp = inputs()
    for v in inp.verdicts:
        if v["candidate_id"] == "cand-factoid-0000":
            v.update(
                status="edit",
                question="Edited question?",
                reference_answer="Edited answer.",
                notes="fixed",
            )
    inp.verdicts += []
    do_freeze(tmp_path, inp)
    recs = [json.loads(ln) for ln in (tmp_path / "questions.v1.jsonl").read_text().splitlines()]
    edited = next(r for r in recs if r["question"] == "Edited question?")
    assert (
        edited["reference_answer"] == "Edited answer."
        and "[edit]" in edited["verification"]["notes"]
    )


def test_gold_hashes_come_from_the_database_and_drift_blocks_freezing(tmp_path):
    inp = inputs()
    store = FakeGoldStore({("2401.00001", 0): "e" * 64})
    with pytest.raises(freeze.FreezeError, match="changed since generation"):
        freeze.freeze(inp, store, dataset_dir=tmp_path, quota=QUOTA, ci_quota=CI)
    gone = FakeGoldStore({("2401.00001", 0): None})
    with pytest.raises(freeze.FreezeError, match="not in the database"):
        freeze.freeze(inp, gone, dataset_dir=tmp_path, quota=QUOTA, ci_quota=CI)


def test_a_frozen_version_is_never_overwritten(tmp_path):
    do_freeze(tmp_path, inputs())
    with pytest.raises(freeze.FreezeError, match="already exists"):
        do_freeze(tmp_path, inputs())
    do_freeze(tmp_path, inputs(), version="v2")
    assert (tmp_path / "questions.v2.jsonl").exists()


def test_dataset_card_discloses_method_and_pending_human_review(tmp_path):
    do_freeze(tmp_path, inputs())
    card = (tmp_path / "DATASET_CARD.md").read_text()
    for needle in (
        "LLM-generated", "agent-verified", "human review is pending", "ollama:llama3.1:8b",
        "agent:claude-test", "precheck-v1", "human_reviewed: false", "d" * 64, "f" * 64,
    ):  # fmt: skip
        assert needle in card, needle
    assert "self-preference" in card  # known bias disclosed


def test_human_label_template_lists_seeded_answerable_ids(tmp_path):
    do_freeze(tmp_path, inputs())
    rows = [
        json.loads(ln) for ln in (tmp_path / "human_labels.template.jsonl").read_text().splitlines()
    ]
    recs = {
        json.loads(ln)["id"]: json.loads(ln)
        for ln in (tmp_path / "questions.v1.jsonl").read_text().splitlines()
    }
    assert len(rows) == 9  # all answerable ids when fewer than 30 exist
    assert all(recs[r["id"]]["type"] in ("factoid", "multi_chunk", "filter") for r in rows)
    assert all(r["faithfulness"] is None and r["answer_relevance"] is None for r in rows)


def test_human_label_template_has_30_ids_for_the_real_quota(tmp_path):
    from evals.dataset.schema import CI_QUOTA
    from evals.dataset.schema import QUOTA as REAL

    cands, pre, ver = [], [], []
    for ctype, n in REAL.items():
        for i in range(n):
            c = candidate(ctype, i)
            cands.append(c)
            pre.append({"candidate_id": c["candidate_id"], "passed": True})
            ver.append(
                {
                    "candidate_id": c["candidate_id"],
                    "status": "accept",
                    "by": "agent:t",
                    "notes": "",
                }
            )
    # distinct gold chunks per candidate are not needed by the fake store
    freeze.freeze(
        freeze.FreezeInputs(cands, pre, ver, None, "f" * 64), FakeGoldStore(), dataset_dir=tmp_path
    )
    assert len((tmp_path / "human_labels.template.jsonl").read_text().splitlines()) == 30
    rep = validate_dataset(tmp_path / "questions.v1.jsonl", tmp_path / "MANIFEST.json")
    assert rep.ok, rep.errors
    assert rep.counts == REAL and rep.ci_counts == CI_QUOTA


def test_cli_lists_every_documented_command():
    out = CliRunner().invoke(cli.app, ["--help"]).output
    for command in (
        "dataset",
        "run",
        "grid",
        "report",
        "filter-bench",
        "thresholds",
        "cassettes",
        "ci-lock",
        "judge-agreement",
    ):
        assert command in out
    sub = CliRunner().invoke(cli.app, ["dataset", "--help"]).output
    for command in (
        "sample",
        "generate",
        "precheck",
        "check-filters",
        "review",
        "verdict",
        "freeze",
        "validate",
    ):
        assert command in sub
    assert "snapshot" in CliRunner().invoke(cli.app, ["cassettes", "--help"]).output
