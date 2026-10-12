from __future__ import annotations

import json
import re
from typing import ClassVar

import pytest
import respx
from fakes import EMB_DIGEST, SERVICE, Env, default_handler, ok_response

from evals.client import ServiceClient
from evals.config_schema import load_config
from evals.dataset.schema import load_dataset
from evals.ollama import ChatResult
from evals.runner import RunOptions, SetupError, compact_response, run_config, select_questions


@pytest.fixture
def env(tmp_path):
    with respx.mock(assert_all_called=False) as mock:
        yield Env(tmp_path, mock)


def run(env, name="05-rerank", **opts):
    return run_config(env.config(name), RunOptions(**opts), env.ctx())


def test_full_run_writes_run_file_with_provenance(env):
    outcome = run(env)
    rec = json.loads(outcome.path.read_text())
    assert outcome.path == env.evals_dir / "runs" / rec["run_id"] / "05-rerank.json"
    assert re.fullmatch(r"20261010T\d{6}Z-abc1234", rec["run_id"])
    for key in (
        "config", "pipeline_sent", "dataset", "seed", "git", "flags", "replayed", "model_digests",
        "gold_drift", "service_info", "started_at", "finished_at", "metrics", "degraded", "errors",
        "questions", "judge",
    ):  # fmt: skip
        assert key in rec, key
    assert rec["config"]["name"] == "05-rerank" and len(rec["config"]["sha256"]) == 64
    assert rec["config"]["content"]["pipeline"]["rerank"]["candidates"] == 20
    assert rec["dataset"]["sha256"] and rec["dataset"]["questions_run"] == 6
    assert rec["seed"] == 42
    assert rec["git"] == {"sha": "abc1234", "dirty": False}
    assert rec["service_info"]["service"]["git_sha"] == "abc1234"
    md = rec["model_digests"]
    assert md["source"] == "ollama" and md["matches_lock"] is True
    assert md["digests"]["bge-m3"] == EMB_DIGEST
    assert rec["replayed"] is False
    assert rec["results_eligible"] is True


def test_pipeline_is_sent_verbatim_and_warmup_is_untimed(env):
    outcome = run(env)
    pipeline = outcome.record["pipeline_sent"]
    config, _ = load_config(env.config("05-rerank"))
    assert pipeline == config.pipeline_payload()
    assert all(r["pipeline"] == pipeline for r in env.requests)
    # 3 warmup + 6 measured; warmup questions never reach the run record
    assert len(env.requests) == 9
    assert [r["question"] for r in env.requests[:3]] == [
        "What is BM25?", "What is dense retrieval?", "What is a cross-encoder?",
    ]  # fmt: skip
    assert len(outcome.record["questions"]) == 6
    assert all(q["request"]["pipeline"] == pipeline for q in outcome.record["questions"])


def test_compact_responses_have_ids_and_scores_but_no_chunk_text(env):
    outcome = run(env)
    text = outcome.path.read_text()
    assert "passage text" not in text  # chunk content never stored
    q1 = outcome.record["questions"][0]
    chunk = q1["response"]["chunks"][0]
    assert set(chunk) == {"rank", "chunk_id", "external_id", "ordinal", "scores"}
    assert "content" not in chunk and "context_header" not in chunk
    assert q1["response"]["answer"]["text"] == "An answer [1]"
    assert compact_response({"chunks": []})["chunks"] == []


def test_metrics_are_computed_from_the_responses(env):
    m = run(env).record["metrics"]
    # factoid q1/q5 and filter q4 have gold at rank 2 -> recall@5 = 1, rr = 0.5
    assert m["retrieval"]["recall_at_5"] == {"mean": 1.0, "n": 3}
    assert m["retrieval"]["mrr"]["mean"] == 0.5
    assert m["routing"]["sql_accuracy"] == {"value": 1.0, "n": 2}
    assert m["routing"]["confusion"]["sql"] == {"sql": 2, "vector": 0}
    # route accuracy: 2 aggregates right, 4 vector-route right
    assert m["routing"]["route_accuracy"]["value"] == 1.0
    assert m["answers"]["abstention_accuracy"] == {"value": 1.0, "n": 1}
    assert m["answers"]["faithfulness"]["n"] == 6
    assert m["answers"]["answer_relevance"]["n"] == 5  # unanswerable excluded
    assert m["latency"]["e2e"]["n"] == 6
    assert m["latency"]["stages"]["rerank"]["p50"] == 100.0
    assert m["cost"]["cost_per_query"] == 0.0 and m["cost"]["tokens_per_query"] == 124
    assert m["filters"]["strategies"] == {"pre": 1}


def test_full_run_upserts_exactly_one_row(env):
    run(env)
    run(env)  # rerun overwrites the row, it does not append
    rows = [ln for ln in env.results_md.read_text().splitlines() if ln.startswith("| 5 |")]
    assert len(rows) == 1 and "`05-rerank`" in rows[0]
    assert "runs/" in rows[0]


@pytest.mark.parametrize(
    "opts",
    [
        {"limit": 2},
        {"no_judge": True},
        {"subset": "ci", "no_judge": True},
        {"replay": True},
    ],
)
def test_non_full_runs_never_touch_results_md(env, opts):
    if opts.get("replay"):
        (env.evals_dir / "cassettes" / "ci").mkdir(parents=True)
        (env.evals_dir / "cassettes" / "ci" / "meta.json").write_text(
            json.dumps({"model_digests": env.live_digests})
        )
    outcome = run(env, **opts)
    assert outcome.record["results_eligible"] is False
    assert not env.results_md.exists()


def test_filter_only_config_is_not_results_eligible(env):
    outcome = run(env, "filter-auto")
    assert outcome.record["results_eligible"] is False
    assert not env.results_md.exists()


def test_subset_ci_writes_under_runs_ci_and_selects_flagged_questions(env):
    outcome = run(env, subset="ci", no_generation=True)
    assert outcome.path.parent.parent == env.evals_dir / "runs" / "ci"
    assert [q["id"] for q in outcome.record["questions"]] == ["q0005", "q0006"]


def test_no_generation_disables_generation_in_the_sent_pipeline_and_skips_judging(env):
    outcome = run(env, no_generation=True)
    assert all(r["pipeline"]["generation"]["enabled"] is False for r in env.requests)
    assert outcome.record["pipeline_sent"]["generation"]["enabled"] is False
    assert outcome.record["config"]["content"]["pipeline"]["generation"]["enabled"] is True
    assert env.judge.calls == []
    assert "answers" not in outcome.record["metrics"]


def test_no_judge_keeps_generation_but_skips_judging(env):
    outcome = run(env, no_judge=True)
    assert env.judge.calls == []
    assert outcome.record["judge"]["enabled"] is False
    assert outcome.record["metrics"]["answers"]["abstention_accuracy"]["n"] == 1
    assert "faithfulness" not in outcome.record["metrics"]["answers"]


def test_limit_interleaves_types_so_a_smoke_run_covers_all_of_them(env):
    config, _ = load_config(env.config("05-rerank"))
    records = load_dataset(env.ctx().dataset_path)
    picked = select_questions(records, config, RunOptions(limit=4))
    assert [r.id for r in picked] == ["q0001", "q0004", "q0002", "q0003"]  # one of each type first
    assert len(select_questions(records, config, RunOptions(limit=100))) == 6


def test_incomplete_config_exits_before_any_request(env):
    env.edit_config("05-rerank", lambda d: d["pipeline"]["rerank"].pop("candidates"))
    with pytest.raises(SetupError, match=r"pipeline\.rerank\.candidates"):
        run(env)
    assert env.requests == [] and env.info_calls == 0 and env.digest_calls == 0


def test_model_digest_mismatch_exits_unless_allowed(env):
    env.live_digests["llama3.1:8b"] = "c" * 64
    with pytest.raises(SetupError, match=r"llama3\.1:8b: digest"):
        run(env)
    assert env.requests == []
    outcome = run(env, allow_model_drift=True)
    md = outcome.record["model_digests"]
    assert md["matches_lock"] is False and md["drift_allowed"] is True
    assert outcome.record["flags"]["allow_model_drift"] is True


def test_missing_model_is_drift(env):
    del env.live_digests["bge-m3"], env.live_digests["bge-m3:latest"]
    with pytest.raises(SetupError, match="bge-m3: not found"):
        run(env)


def test_gold_drift_exits_listing_ids_unless_allowed(env):
    env.gold.overrides[("2401.00001", 0)] = "f" * 64  # q0001 changed
    env.gold.overrides[("2401.00002", 1)] = None  # q0004 gone
    with pytest.raises(SetupError) as exc:
        run(env)
    msg = str(exc.value)
    assert "q0001" in msg and "q0004" in msg and "q0005" not in msg
    assert "content hash differs" in msg and "missing from the database" in msg
    assert env.requests == []
    outcome = run(env, allow_drift=True)
    assert outcome.record["gold_drift"]["drifted_ids"] == ["q0001", "q0004"]
    assert outcome.record["gold_drift"]["drift_allowed"] is True


def test_unreachable_service_is_a_setup_error(env):
    env.info_status = 503
    with pytest.raises(SetupError, match="/v1/info"):
        run(env)
    assert env.requests == []


def test_replay_takes_digests_from_cassette_meta_and_never_asks_ollama(env):
    (env.evals_dir / "cassettes" / "ci").mkdir(parents=True)
    (env.evals_dir / "cassettes" / "ci" / "meta.json").write_text(
        json.dumps({"model_digests": env.live_digests})
    )
    env.live_digests.clear()  # live Ollama would now fail the check
    outcome = run(env, replay=True, subset="ci")
    assert env.digest_calls == 0
    assert outcome.record["replayed"] is True
    assert outcome.record["model_digests"]["source"] == "cassette_meta"
    assert outcome.record["model_digests"]["matches_lock"] is True


def test_replay_without_meta_is_a_setup_error(env):
    with pytest.raises(SetupError, match=r"meta\.json"):
        run(env, replay=True)


def test_degraded_and_failed_requests_are_recorded(env):
    def handler(body):
        if body["question"] == "Question q0001?":
            return ok_response(degraded=["rerank_unavailable"], chunks=[])
        return default_handler(body)

    env.handler = handler
    outcome = run(env)
    assert outcome.degraded_count == 1
    assert outcome.record["degraded"]["by_code"] == {"rerank_unavailable": 1}
    assert outcome.record["degraded"]["rate"] == pytest.approx(1 / 6)

    env.query_status = 500
    failed = run(env, subset="ci")
    assert failed.error_count == 2 and failed.record["errors"]["rate"] == 1.0
    assert failed.record["questions"][0]["response"] is None
    assert failed.record["questions"][0]["http_status"] == 500
    # failed requests count as zero retrieval / wrong route, never as silently dropped
    assert failed.record["metrics"]["retrieval"]["recall_at_5"]["mean"] == 0.0
    assert failed.record["metrics"]["routing"]["route_accuracy"]["value"] == 0.0


def test_judge_runs_after_all_queries_so_latency_is_not_interleaved(env):
    order = []

    def judge_chat(*args, **kwargs):
        order.append("judge")
        return ChatResult('{"verdict": "supported"}', 1, 1)

    env.judge.chat = judge_chat
    orig = env.handler
    env.handler = lambda b: order.append("query") or orig(b)
    run(env)
    first_judge = order.index("judge")
    assert "query" not in order[first_judge:]


def test_client_timeout_comes_from_the_config(env):
    class Spy(ServiceClient):
        timeouts: ClassVar[list] = []

        def query(self, question, pipeline, *, timeout_s=None):
            self.timeouts.append(timeout_s)
            return super().query(question, pipeline, timeout_s=timeout_s)

    env.edit_config("05-rerank", lambda d: d["run"].update(timeout_s=7))
    spy = Spy(SERVICE)
    run_config(env.config("05-rerank"), RunOptions(limit=2), env.ctx(service=spy))
    assert spy.timeouts and set(spy.timeouts) == {7}  # warmup and measured queries alike


@respx.mock
def test_service_client_applies_a_per_request_timeout():
    import httpx

    route = respx.post(f"{SERVICE}/v1/query").mock(side_effect=httpx.ReadTimeout("slow"))
    res = ServiceClient(SERVICE).query("q", {}, timeout_s=3)
    assert res.status is None and "ReadTimeout" in res.error
    assert route.calls.last.request.extensions["timeout"]["read"] == 3


def test_results_roles(env):
    from evals.runner import results_role

    cfg, _ = load_config(env.config("05-rerank"))
    filt, _ = load_config(env.config("filter-auto"))
    assert results_role(cfg, RunOptions()) == "full"
    assert results_role(cfg, RunOptions(no_generation=True, no_judge=True)) == "retrieval"
    assert results_role(cfg, RunOptions(no_generation=True)) == "retrieval"
    assert results_role(cfg, RunOptions(subset="ci")) == "gen_subset"
    assert results_role(cfg, RunOptions(subset="ci", no_generation=True)) is None  # CI gate runs
    assert results_role(cfg, RunOptions(no_judge=True)) is None
    assert results_role(cfg, RunOptions(limit=3)) is None
    assert results_role(cfg, RunOptions(replay=True)) is None
    assert results_role(filt, RunOptions()) is None
    assert results_role(filt, RunOptions(no_generation=True)) is None  # filter-only: not a grid row


def test_subset_run_with_generation_is_a_results_run_but_outside_runs_ci(env):
    outcome = run(env, subset="ci")
    assert outcome.record["results_role"] == "gen_subset"
    assert outcome.path.parent.parent == env.evals_dir / "runs"  # not runs/ci
