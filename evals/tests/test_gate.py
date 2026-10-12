from __future__ import annotations

import json

import pytest
import respx
import yaml
from fakes import Env, default_handler, ok_response
from typer.testing import CliRunner

from evals import cli, gate, thresholds
from evals.grid import run_grid
from evals.runner import RunOptions

NAMES = list(thresholds.GRID_CONFIGS)


def record(
    name, *, ndcg_at_10=0.8, r20=0.9, route=1.0, sql=1.0, degraded=0, errors=0, run_id="RUN1"
):
    return {
        "run_id": run_id,
        "config": {"name": name},
        "metrics": {
            "retrieval": {
                "ndcg_at_10": {"mean": ndcg_at_10, "n": 10},
                "recall_at_20": {"mean": r20, "n": 10},
            },
            "routing": {
                "route_accuracy": {"value": route, "n": 10},
                "sql_accuracy": {"value": sql, "n": 5},
            },
        },
        "degraded": {"count": degraded},
        "errors": {"count": errors},
    }


def baseline(**overrides):
    return {n: record(n, **overrides.get(n, {})) for n in NAMES}


def write_run_dir(tmp_path, records, run_id="RUN1"):
    d = tmp_path / "runs" / "ci" / run_id
    d.mkdir(parents=True)
    for name, rec in records.items():
        (d / f"{name}.json").write_text(json.dumps(rec))
    (d / "grid.json").write_text("{}")  # summary file must be ignored
    return d


# --- thresholds ------------------------------------------------------------------------------


def test_thresholds_are_baseline_minus_tolerance(tmp_path):
    recs = baseline(**{"06-routing": {"ndcg_at_10": 0.7, "r20": 0.85, "route": 0.9, "sql": 0.8}})
    data = thresholds.write_thresholds(write_run_dir(tmp_path, recs), tmp_path / "ci.yaml")
    assert data["baseline_run"] == "RUN1"
    assert data["max_degraded"] == 0
    assert data["tolerance"] == {
        "ndcg_at_10": 0.03,
        "recall_at_20": 0.03,
        "route_accuracy": 0.05,
        "sql_accuracy": 0.10,
    }
    assert data["configs"]["01-vector"] == {"ndcg_at_10": 0.77, "recall_at_20": 0.87}
    assert data["configs"]["06-routing"] == {
        "ndcg_at_10": 0.67, "recall_at_20": 0.82, "route_accuracy": 0.85, "sql_accuracy": 0.7,
    }  # fmt: skip
    assert set(data["configs"]) == set(NAMES)
    assert set(data["configs"]["05-rerank"]) == {
        "ndcg_at_10",
        "recall_at_20",
    }  # routing metrics: row 6 only
    assert yaml.safe_load((tmp_path / "ci.yaml").read_text()) == data


def test_thresholds_are_floored_at_zero_and_rounded(tmp_path):
    recs = baseline(
        **{
            "01-vector": {"ndcg_at_10": 0.01, "r20": 0.0},
            "06-routing": {"sql": 0.05, "route": 0.123456},
        }
    )
    data = thresholds.write_thresholds(write_run_dir(tmp_path, recs), tmp_path / "ci.yaml")
    assert data["configs"]["01-vector"] == {"ndcg_at_10": 0.0, "recall_at_20": 0.0}
    assert data["configs"]["06-routing"]["sql_accuracy"] == 0.0
    assert data["configs"]["06-routing"]["route_accuracy"] == 0.073


def test_thresholds_need_a_complete_single_run(tmp_path):
    recs = baseline()
    del recs["03-filters"]
    with pytest.raises(thresholds.ThresholdError, match="03-filters"):
        thresholds.write_thresholds(write_run_dir(tmp_path, recs), tmp_path / "ci.yaml")
    mixed = baseline()
    mixed["02-hybrid"]["run_id"] = "OTHER"
    with pytest.raises(thresholds.ThresholdError, match="several runs"):
        thresholds.write_thresholds(write_run_dir(tmp_path, mixed, "RUN2"), tmp_path / "ci.yaml")
    with pytest.raises(thresholds.ThresholdError, match="not found"):
        thresholds.write_thresholds(tmp_path / "missing", tmp_path / "ci.yaml")


def make_thresholds(tmp_path, **kw):
    path = tmp_path / "ci.yaml"
    thresholds.write_thresholds(write_run_dir(tmp_path, baseline(**kw)), path)
    return thresholds.load_thresholds(path)


# --- gate exit codes ---------------------------------------------------------------------------


def test_gate_passes_when_metrics_meet_thresholds(tmp_path):
    th = make_thresholds(tmp_path)
    result = gate.evaluate(th, baseline())
    assert result.exit_code == 0
    assert len(result.rows) == 5 * 2 + 4 and all(r.passed for r in result.rows)


def test_gate_passes_at_exactly_the_threshold_and_within_tolerance(tmp_path):
    th = make_thresholds(tmp_path)
    recs = baseline(**{"05-rerank": {"ndcg_at_10": 0.77}})  # baseline 0.8 - 0.03
    assert gate.evaluate(th, recs).exit_code == 0


def test_regression_fails_with_exit_1_and_marks_the_row(tmp_path):
    th = make_thresholds(tmp_path)
    recs = baseline(**{"05-rerank": {"ndcg_at_10": 0.5}})
    result = gate.evaluate(th, recs)
    assert result.exit_code == 1
    failed = [r for r in result.rows if not r.passed]
    assert [(r.config, r.metric) for r in failed] == [("05-rerank", "ndcg_at_10")]
    table = gate.format_table(result)
    marked = [ln for ln in table.splitlines() if "FAIL" in ln]
    assert len(marked) == 1 and marked[0].startswith("05-rerank") and "ndcg_at_10" in marked[0]
    assert "0.500" in marked[0] and "0.770" in marked[0]  # value and threshold printed
    assert table.splitlines()[0].split() == ["config", "metric", "value", "threshold", "status"]


def test_routing_metric_regression_fails(tmp_path):
    th = make_thresholds(tmp_path)
    result = gate.evaluate(th, baseline(**{"06-routing": {"sql": 0.5}}))
    assert result.exit_code == 1


def test_missing_metric_value_counts_as_failure(tmp_path):
    th = make_thresholds(tmp_path)
    recs = baseline()
    recs["01-vector"]["metrics"]["retrieval"]["ndcg_at_10"]["mean"] = None
    assert gate.evaluate(th, recs).exit_code == 1


def test_degraded_response_exits_2_with_rerecord_hint_in_replay(tmp_path):
    th = make_thresholds(tmp_path)
    result = gate.evaluate(th, baseline(**{"03-filters": {"degraded": 2}}), replay=True)
    assert result.exit_code == 2
    out = gate.format_table(result)
    assert "make eval-record-ci" in out and "03-filters: 2 degraded" in out
    assert "make eval-record-ci" not in gate.format_table(
        gate.evaluate(th, baseline(**{"03-filters": {"degraded": 2}}))
    )


def test_failed_requests_exit_2_and_beat_a_metric_breach(tmp_path):
    th = make_thresholds(tmp_path)
    # a cassette miss degrades the response AND tanks the metric: still exit 2, not 1
    recs = baseline(**{"05-rerank": {"degraded": 1, "ndcg_at_10": 0.0}})
    assert gate.evaluate(th, recs).exit_code == 2
    assert gate.evaluate(th, baseline(**{"01-vector": {"errors": 1}})).exit_code == 2


def test_config_missing_from_the_grid_is_a_setup_error(tmp_path):
    th = make_thresholds(tmp_path)
    recs = baseline()
    del recs["02-hybrid"]
    assert gate.evaluate(th, recs).exit_code == 3


def test_load_thresholds_errors(tmp_path):
    with pytest.raises(thresholds.ThresholdError):
        thresholds.load_thresholds(tmp_path / "nope.yaml")
    bad = tmp_path / "bad.yaml"
    bad.write_text("foo: 1\n")
    with pytest.raises(thresholds.ThresholdError):
        thresholds.load_thresholds(bad)


# --- end to end through the CLI ------------------------------------------------------------------


@pytest.fixture
def env(tmp_path, monkeypatch):
    with respx.mock(assert_all_called=False) as mock:
        e = Env(tmp_path, mock)
        monkeypatch.setattr(cli, "EVALS_DIR", e.evals_dir)
        monkeypatch.setattr(cli, "default_context", lambda: e.ctx())
        yield e


def record_baseline(env):
    grid = run_grid(
        env.evals_dir / "configs",
        RunOptions(subset="ci", no_generation=True, no_judge=True),
        env.ctx(),
    )
    out = env.evals_dir / "thresholds" / "ci.yaml"
    thresholds.write_thresholds(grid.outcomes[0].path.parent, out)
    return out


def grid_cmd(*extra):
    return CliRunner().invoke(
        cli.app, ["grid", "--subset", "ci", "--no-generation", "--no-judge", *extra]
    )


def test_cli_gate_exit_codes_end_to_end(env):
    th = record_baseline(env)
    ok = grid_cmd("--gate", str(th))
    assert ok.exit_code == 0, ok.output
    assert "gate exit code: 0" in ok.output

    data = yaml.safe_load(th.read_text())
    data["configs"]["01-vector"]["ndcg_at_10"] = 1.01  # impossible minimum
    th.write_text(yaml.safe_dump(data))
    breach = grid_cmd("--gate", str(th))
    assert breach.exit_code == 1 and "FAIL" in breach.output

    env.handler = lambda b: (
        ok_response(degraded=["rerank_unavailable"])
        if "q0005" in b["question"]
        else default_handler(b)
    )
    degraded = grid_cmd("--gate", str(record_baseline_file(env)))
    assert degraded.exit_code == 2

    env.info_status = 503
    setup = grid_cmd("--gate", str(th))
    assert setup.exit_code == 3

    assert grid_cmd("--gate", str(env.evals_dir / "thresholds" / "missing.yaml")).exit_code == 3


def record_baseline_file(env):
    return env.evals_dir / "thresholds" / "ci.yaml"


def test_cli_replay_cassette_miss_prints_rerecord_hint(env):
    th = record_baseline(env)
    (env.evals_dir / "cassettes" / "ci").mkdir(parents=True)
    (env.evals_dir / "cassettes" / "ci" / "meta.json").write_text(
        json.dumps({"model_digests": env.live_digests})
    )
    env.digest_calls = 0
    env.handler = lambda b: ok_response(degraded=["rerank_unavailable"])  # wiremock 404 -> degraded
    res = grid_cmd("--replay", "--gate", str(th))
    assert res.exit_code == 2
    assert "make eval-record-ci" in res.output
    assert env.digest_calls == 0  # replay never asked Ollama


def test_cli_run_prints_summary_and_exit_0(env):
    res = CliRunner().invoke(
        cli.app, ["run", "--config", str(env.config("05-rerank")), "--limit", "3"]
    )
    assert res.exit_code == 0, res.output
    assert "05-rerank: nDCG@10=" in res.output
    assert not env.results_md.exists()  # limited run


def test_cli_run_incomplete_config_exits_3_before_requests(env):
    env.edit_config("05-rerank", lambda d: d["pipeline"]["rerank"].pop("candidates"))
    res = CliRunner().invoke(cli.app, ["run", "--config", str(env.config("05-rerank"))])
    assert res.exit_code == 3 and "pipeline.rerank.candidates" in res.output
    assert env.requests == [] and env.info_calls == 0


def test_cli_thresholds_command(env, tmp_path):
    grid = run_grid(
        env.evals_dir / "configs", RunOptions(subset="ci", no_generation=True), env.ctx()
    )
    out = tmp_path / "t.yaml"
    res = CliRunner().invoke(
        cli.app, ["thresholds", "--from", str(grid.outcomes[0].path.parent), "--out", str(out)]
    )
    assert res.exit_code == 0, res.output
    assert yaml.safe_load(out.read_text())["baseline_run"] == grid.run_id


# --- tolerated degradation codes -------------------------------------------------------------------


def with_questions(rec, codes_per_question):
    rec = dict(rec)
    rec["degraded"] = {
        "count": sum(1 for c in codes_per_question if c),
        "by_code": {c: 1 for codes in codes_per_question for c in codes},
    }
    rec["questions"] = [{"response": {"degraded": c}} for c in codes_per_question]
    return rec


def test_baseline_records_tolerable_sql_fallback_only(tmp_path):
    recs = baseline()
    recs["06-routing"] = with_questions(
        recs["06-routing"], [["sql_fallback"], [], ["sql_fallback"]]
    )
    data = thresholds.write_thresholds(write_run_dir(tmp_path, recs), tmp_path / "ci.yaml")
    assert data["allowed_degraded_codes"] == ["sql_fallback"]
    assert data["max_degraded"] == 0
    recs["05-rerank"] = with_questions(recs["05-rerank"], [["rerank_unavailable"]])
    data = thresholds.build_thresholds(recs)
    assert data["allowed_degraded_codes"] == [
        "sql_fallback"
    ]  # dependency codes are never tolerated
    assert thresholds.build_thresholds(baseline())["allowed_degraded_codes"] == []


def test_gate_tolerates_sql_fallback_but_not_dependency_degradation(tmp_path):
    recs = baseline()
    recs["06-routing"] = with_questions(recs["06-routing"], [["sql_fallback"], []])
    th = thresholds.build_thresholds(recs)
    assert gate.evaluate(th, recs).exit_code == 0
    bad = baseline()
    bad["06-routing"] = with_questions(
        bad["06-routing"], [["sql_fallback"], ["router_unavailable"]]
    )
    res = gate.evaluate(th, bad, replay=True)
    assert res.exit_code == 2 and "1 degraded" in gate.format_table(res)
    mixed = baseline()
    mixed["06-routing"] = with_questions(
        mixed["06-routing"], [["sql_fallback", "rerank_unavailable"]]
    )
    assert gate.evaluate(th, mixed).exit_code == 2
    strict = dict(th, allowed_degraded_codes=[])
    assert gate.evaluate(strict, recs).exit_code == 2
