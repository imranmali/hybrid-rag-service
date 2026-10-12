from __future__ import annotations

import json

import pytest
import respx
from fakes import Env

from evals import filter_bench, judge_agreement, report
from evals.grid import run_grid
from evals.runner import RunOptions, run_config


@pytest.fixture
def env(tmp_path):
    with respx.mock(assert_all_called=False) as mock:
        yield Env(tmp_path, mock)


def rows(env):
    return [
        ln for ln in env.results_md.read_text().splitlines() if ln.startswith("| ") and "`" in ln
    ]


def test_results_md_has_header_dataset_line_and_all_columns(env):
    run_config(env.config("05-rerank"), RunOptions(), env.ctx())
    text = env.results_md.read_text()
    assert "Do not edit by hand" in text
    assert "human review is pending" in text
    assert "sha256" in text
    header = next(ln for ln in text.splitlines() if ln.startswith("| # | Config"))
    assert header.split(" | ")[1:-1] == [h for h in report.COLUMNS[1:-1]]
    row = next(ln for ln in text.splitlines() if ln.startswith("| 5 |"))
    cells = [c.strip() for c in row.strip("|").split("|")]
    assert len(cells) == len(report.COLUMNS)
    assert all(c and c != "-" for i, c in enumerate(cells) if report.COLUMNS[i] != "p95 rerank ms")
    assert "(est.)" in row  # cost carries the estimate marker
    assert cells[-1].startswith("[2026") and "runs/" in cells[-1]


def test_rows_are_ordered_by_grid_row_with_one_row_per_config(env):
    for name in ("06-routing", "02-hybrid", "04-contextual"):
        run_config(env.config(name), RunOptions(), env.ctx())
    run_config(env.config("02-hybrid"), RunOptions(), env.ctx())  # rerun replaces the row
    names = [ln.split("|")[2].strip() for ln in rows(env) if ln.split("|")[1].strip().isdigit()]
    assert names == ["`02-hybrid`", "`04-contextual`", "`06-routing`"]


def test_provenance_table_lists_seed_digests_and_git_sha(env):
    run_config(env.config("06-routing"), RunOptions(), env.ctx())
    text = env.results_md.read_text()
    prov = text[text.index("## Provenance") :]
    assert "`abc1234`" in prov and "| 42 |" in prov
    assert "aaaaaaaaaaaa" in prov and "bbbbbbbbbbbb" in prov  # both digests (12 hex)
    assert "yes" in prov  # digests match lock


def test_routing_config_gets_a_confusion_matrix_and_stage_table(env):
    run_config(env.config("06-routing"), RunOptions(), env.ctx())
    text = env.results_md.read_text()
    assert "## Routing confusion matrix: `06-routing`" in text
    assert "| **sql** | 2 | 0 |" in text
    assert "## Per-stage latency" in text and "| rerank |" in text


def test_degraded_rows_are_flagged(env):
    from fakes import default_handler, ok_response

    env.handler = lambda b: (
        ok_response(degraded=["rerank_unavailable"])
        if b["question"] == "Question q0001?"
        else default_handler(b)
    )
    run_config(env.config("05-rerank"), RunOptions(), env.ctx())
    assert "⚠" in next(
        ln for ln in env.results_md.read_text().splitlines() if ln.startswith("| 5 |")
    )


def test_report_is_rebuilt_from_run_files_only(env):
    run_config(env.config("05-rerank"), RunOptions(), env.ctx())
    first = env.results_md.read_text()
    env.results_md.unlink()
    report.write_results(env.evals_dir)
    assert env.results_md.read_text() == first  # nothing hand-written, nothing lost


def test_every_number_comes_from_a_run_file(env):
    outcome = run_config(env.config("05-rerank"), RunOptions(), env.ctx())
    rec = json.loads(outcome.path.read_text())
    row = next(ln for ln in env.results_md.read_text().splitlines() if ln.startswith("| 5 |"))
    assert f"{rec['metrics']['retrieval']['ndcg_at_10']['mean']:.3f}" in row
    assert f"{rec['metrics']['latency']['e2e']['p50']:,.0f}" in row


def test_non_eligible_runs_are_ignored_by_report(env):
    run_config(env.config("05-rerank"), RunOptions(limit=2), env.ctx())
    run_config(env.config("05-rerank"), RunOptions(subset="ci", no_generation=True), env.ctx())
    run_config(env.config("05-rerank"), RunOptions(no_judge=True), env.ctx())
    assert report.load_eligible_runs(env.evals_dir) == []
    report.write_results(env.evals_dir)
    assert "No results runs yet" in env.results_md.read_text()


def test_latest_full_run_wins(env):
    first = run_config(env.config("05-rerank"), RunOptions(), env.ctx())
    second = run_config(env.config("05-rerank"), RunOptions(), env.ctx())
    ((path, _),) = report.load_eligible_runs(env.evals_dir)
    assert path == second.path != first.path


# --- grid ------------------------------------------------------------------------------------


def test_grid_runs_six_configs_in_order_under_one_run_id(env):
    grid = run_grid(
        env.evals_dir / "configs",
        RunOptions(subset="ci", no_generation=True, no_judge=True),
        env.ctx(),
    )
    names = [o.record["config"]["name"] for o in grid.outcomes]
    assert names == [
        "01-vector",
        "02-hybrid",
        "03-filters",
        "04-contextual",
        "05-rerank",
        "06-routing",
    ]
    assert {o.record["run_id"] for o in grid.outcomes} == {grid.run_id}
    assert all(o.path.parent == env.evals_dir / "runs" / "ci" / grid.run_id for o in grid.outcomes)
    summary = json.loads(grid.summary_path.read_text())
    assert [c["name"] for c in summary["configs"]] == names
    assert not env.results_md.exists()  # CI results isolated


def test_full_grid_fills_six_ordered_rows(env):
    run_grid(env.evals_dir / "configs", RunOptions(), env.ctx())
    nums = [ln.split("|")[1].strip() for ln in rows(env) if ln.split("|")[1].strip().isdigit()]
    assert nums == ["1", "2", "3", "4", "5", "6"]


# --- filter bench + judge agreement -------------------------------------------------------------


def test_filter_bench_writes_one_row_per_strategy(env):
    path, outcomes = filter_bench.run_filter_bench(
        env.evals_dir / "configs", RunOptions(), env.ctx()
    )
    text = path.read_text()
    assert path == env.evals_dir / "results_filter_pushdown.md"
    assert list(outcomes) == ["pre", "post", "auto"]
    for strategy in ("pre", "post", "auto"):
        assert f"| `{strategy}` ([run]" in text
    header = next(ln for ln in text.splitlines() if ln.startswith("| Strategy"))
    assert "R@20" in header and "nDCG@10" in header and "retrieve.vector p50" in header
    assert "underfilled" in header and "strategies chosen" in header
    # only filter questions, retrieval only, and results.md untouched
    assert all(r["pipeline"]["generation"]["enabled"] is False for r in env.requests)
    assert not env.results_md.exists()
    assert outcomes["pre"].record["dataset"]["questions_run"] == 1


def test_judge_agreement_is_pending_without_labels(tmp_path):
    assert judge_agreement.report(tmp_path / "dataset" / "human_labels.jsonl") == (
        "pending: no dataset/human_labels.jsonl"
    )


def test_judge_agreement_computes_agreement_and_kappa(env):
    run_config(env.config("06-routing"), RunOptions(), env.ctx())  # judge says supported / relevant
    labels = [
        {
            "id": "q0001",
            "config": "06-routing",
            "faithfulness": "supported",
            "answer_relevance": "relevant",
        },
        {
            "id": "q0004",
            "config": "06-routing",
            "faithfulness": "unsupported",
            "answer_relevance": "relevant",
        },
        {"id": "q0005", "config": "06-routing", "faithfulness": None, "answer_relevance": None},
    ]
    path = env.evals_dir / "dataset" / "human_labels.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in labels))
    result = judge_agreement.agreement(labels, env.evals_dir)
    assert result["faithfulness"]["n"] == 2 and result["faithfulness"]["agreement"] == 0.5
    assert result["answer_relevance"]["agreement"] == 1.0
    text = judge_agreement.report(path, env.evals_dir)
    assert "faithfulness: n=2" in text


def test_cohens_kappa():
    assert judge_agreement.cohens_kappa([]) is None
    assert judge_agreement.cohens_kappa([("a", "a"), ("b", "b")]) == 1.0
    assert judge_agreement.cohens_kappa([("a", "b"), ("b", "a")]) == -1.0


# --- retrieval-only + gen/judge subset composition ---------------------------------------------------


def cells(env, number):
    row = next(
        ln for ln in env.results_md.read_text().splitlines() if ln.startswith(f"| {number} |")
    )
    return dict(zip(report.COLUMNS, [c.strip() for c in row.strip("|").split("|")], strict=True))


def test_retrieval_only_run_fills_retrieval_columns_and_dashes_the_rest(env):
    run_config(env.config("06-routing"), RunOptions(no_generation=True, no_judge=True), env.ctx())
    c = cells(env, 6)
    assert c["Mode"] == "retrieval-only"
    assert (
        c["nDCG@10"] != "—"
        and c["R@20"] != "—"
        and c["Route acc."] != "—"
        and c["p50 e2e ms"] != "—"
    )
    for col in (
        "Faithfulness",
        "Answer rel.",
        "Abstain acc.",
        "Cost/query (est.)",
        "Tokens/query",
        "Gen/judge run",
    ):
        assert c[col] == "—", col
    assert "retrieval-only" in env.results_md.read_text()


def test_gen_subset_run_fills_generation_columns_marked_ci50(env):
    run_config(env.config("06-routing"), RunOptions(no_generation=True, no_judge=True), env.ctx())
    gen = run_config(env.config("06-routing"), RunOptions(subset="ci"), env.ctx())
    c = cells(env, 6)
    assert c["Mode"] == "retrieval-only"  # retrieval columns still come from the full-dataset run
    assert c["Faithfulness"].endswith("(ci50)") and c["Abstain acc."].endswith("(ci50)")
    assert c["Tokens/query"].endswith("(ci50)")
    assert "(est.)" in c["Cost/query (est.)"]
    assert gen.record["run_id"] in c["Gen/judge run"] and "runs/" in c["Gen/judge run"]
    assert gen.record["run_id"] not in c["Run"]
    # numbers come from the right run files
    main_rec = next(
        r for _, r in report.load_eligible_runs(env.evals_dir) if r["results_role"] == "retrieval"
    )
    assert f"{main_rec['metrics']['retrieval']['ndcg_at_10']['mean']:.3f}" in c["nDCG@10"]
    assert f"{gen.record['metrics']['answers']['faithfulness']['mean']:.3f}" in c["Faithfulness"]


def test_gen_subset_alone_leaves_retrieval_columns_blank(env):
    run_config(env.config("06-routing"), RunOptions(subset="ci"), env.ctx())
    c = cells(env, 6)
    assert c["nDCG@10"] == "—" and c["Mode"] == "—" and c["Run"] == "—"
    assert c["Faithfulness"].endswith("(ci50)")


def test_full_run_supersedes_nothing_but_provides_every_column(env):
    run_config(env.config("06-routing"), RunOptions(), env.ctx())
    c = cells(env, 6)
    assert c["Mode"] == "full" and "(ci50)" not in c["Faithfulness"] and c["Faithfulness"] != "—"
