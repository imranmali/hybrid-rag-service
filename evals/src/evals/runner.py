"""Runs one eval config against the live service and writes a reproducible run record (D6/D8)."""

from __future__ import annotations

import copy
import json
import subprocess
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evals import drift
from evals.client import ServiceClient, ServiceError
from evals.config_schema import QUESTION_TYPES, ConfigError, EvalConfig, Judge, load_config
from evals.dataset.schema import Record, load_dataset, sha256_file
from evals.metrics import cost, judge, latency, retrieval, routing
from evals.ollama import LLM, LLMError, fetch_digests, make_llm
from evals.settings import EVALS_DIR, REPO_ROOT, Settings, get_settings

WARMUP_QUESTIONS = ("What is BM25?", "What is dense retrieval?", "What is a cross-encoder?")
# Paths that change during a run and must not make the working tree look "dirty".
_DIRTY_EXCLUDES = ("evals/runs", "evals/results.md", "evals/results_filter_pushdown.md")


class SetupError(Exception):
    """Setup, validation or drift problem; the CLI maps it to exit code 3."""


@dataclass
class RunOptions:
    subset: str | None = None  # "ci" or None
    limit: int | None = None
    no_generation: bool = False
    no_judge: bool = False
    replay: bool = False
    allow_model_drift: bool = False
    allow_drift: bool = False


@dataclass
class RunContext:
    """Everything the runner touches outside the process, so tests can fake it."""

    service: ServiceClient
    gold_store: drift.GoldStore
    digest_fetcher: Callable[[], dict[str, str]]
    judge_factory: Callable[[Judge], LLM]
    git_info: Callable[[], dict[str, Any]]
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    evals_dir: Path = EVALS_DIR
    repo_root: Path = REPO_ROOT
    dataset_path: Path = EVALS_DIR / "dataset" / "questions.v1.jsonl"
    pricing_path: Path = EVALS_DIR / "pricing.yaml"
    models_lock_path: Path = REPO_ROOT / "infra" / "models.lock.json"
    cassette_meta_path: Path = EVALS_DIR / "cassettes" / "ci" / "meta.json"
    warmup_questions: tuple[str, ...] = WARMUP_QUESTIONS


@dataclass
class RunOutcome:
    path: Path
    record: dict[str, Any]

    @property
    def degraded_count(self) -> int:
        return self.record["degraded"]["count"]

    @property
    def error_count(self) -> int:
        return self.record["errors"]["count"]


def git_info(repo_root: Path = REPO_ROOT) -> dict[str, Any]:
    def run(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=repo_root, capture_output=True, text=True, check=True
        ).stdout.strip()

    try:
        sha = run("rev-parse", "--short=7", "HEAD")
        pathspec = [".", *(f":(exclude){p}" for p in _DIRTY_EXCLUDES)]
        dirty = bool(run("status", "--porcelain", "--", *pathspec))
    except (subprocess.CalledProcessError, FileNotFoundError):
        return {"sha": "unknown", "dirty": True}
    return {"sha": sha, "dirty": dirty}


def default_context(settings: Settings | None = None) -> RunContext:
    settings = settings or get_settings()
    return RunContext(
        service=ServiceClient(settings.rag_service_url),
        gold_store=drift.PostgresGoldStore(settings.evals_database_url),
        digest_fetcher=lambda: fetch_digests(settings.ollama_host_url),
        judge_factory=lambda j: make_llm(j.provider, j.model, settings),
        git_info=git_info,
    )


def make_run_id(ctx: RunContext) -> str:
    return f"{ctx.now().strftime('%Y%m%dT%H%M%SZ')}-{ctx.git_info()['sha'][:7]}"


def select_questions(records: list[Record], config: EvalConfig, opts: RunOptions) -> list[Record]:
    selected = [r for r in records if r.type in config.question_types]
    if opts.subset == "ci":
        selected = [r for r in selected if r.ci_subset]
    if opts.limit is not None:
        selected = _round_robin(selected, opts.limit)
    return selected


def _round_robin(records: list[Record], n: int) -> list[Record]:
    """First `n` records interleaved across types, so a smoke run covers every question type."""
    by_type: dict[str, list[Record]] = {}
    for r in records:
        by_type.setdefault(r.type, []).append(r)
    lists = [by_type[t] for t in QUESTION_TYPES if t in by_type]
    out: list[Record] = []
    depth = 0
    while len(out) < n and any(depth < len(lst) for lst in lists):
        out.extend(lst[depth] for lst in lists if depth < len(lst))
        depth += 1
    return out[:n]


def compact_response(body: dict[str, Any]) -> dict[str, Any]:
    """Chunk identifiers and scores only (no chunk text); everything else verbatim."""
    return {
        "request_id": body.get("request_id"),
        "trace_id": body.get("trace_id"),
        "route": body.get("route"),
        "route_source": body.get("route_source"),
        "filters": body.get("filters"),
        "chunks": [
            {
                "rank": c.get("rank"),
                "chunk_id": c.get("chunk_id"),
                "external_id": c.get("external_id"),
                "ordinal": c.get("ordinal"),
                "scores": c.get("scores"),
            }
            for c in body.get("chunks", [])
        ],
        "sql": body.get("sql"),
        "answer": body.get("answer"),
        "degraded": body.get("degraded", []),
        "timings_ms": body.get("timings_ms", {}),
        "usage": body.get("usage", {}),
    }


def _preflight(
    config: EvalConfig,
    records: list[Record],
    opts: RunOptions,
    ctx: RunContext,
) -> dict[str, Any]:
    try:
        service_info = ctx.service.info()
    except ServiceError as exc:
        raise SetupError(str(exc)) from exc

    lock = drift.load_models_lock(ctx.models_lock_path)
    lock_ollama = drift.lock_digests(lock)
    if opts.replay:
        try:
            meta = json.loads(ctx.cassette_meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SetupError(f"--replay needs {ctx.cassette_meta_path}: {exc}") from exc
        live = dict(meta.get("model_digests", {}))
        source = "cassette_meta"
    else:
        try:
            live = ctx.digest_fetcher()
        except LLMError as exc:
            raise SetupError(str(exc)) from exc
        source = "ollama"
    mismatches = drift.digest_mismatches(live, lock_ollama)
    if mismatches and not opts.allow_model_drift:
        raise SetupError(
            "model digest drift (pass --allow-model-drift to override):\n  "
            + "\n  ".join(mismatches)
        )

    warnings: list[str] = []
    live_rev = ((service_info.get("models") or {}).get("reranker") or {}).get("revision")
    lock_rev = (lock.get("reranker") or {}).get("revision")
    if live_rev and lock_rev and live_rev != lock_rev:
        warnings.append(f"reranker revision {live_rev} != lock {lock_rev}")

    try:
        gold = drift.gold_drift(records, ctx.gold_store)
    except Exception as exc:  # database unreachable etc.
        raise SetupError(f"gold chunk check failed: {exc}") from exc
    if gold and not opts.allow_drift:
        lines = [f"{qid}: {'; '.join(reasons)}" for qid, reasons in sorted(gold.items())]
        raise SetupError(
            f"gold chunk drift in {len(gold)} question(s) (pass --allow-drift to override):\n  "
            + "\n  ".join(lines)
        )
    return {
        "service_info": service_info,
        "model_digests": {
            "source": source,
            "digests": {n: live.get(n) for n in lock_ollama},
            "lock": lock_ollama,
            "matches_lock": not mismatches,
            "drift_allowed": bool(mismatches and opts.allow_model_drift),
        },
        "gold_drift": {
            "checked": len([r for r in records if r.gold_chunks]),
            "drifted_ids": sorted(gold),
            "drift_allowed": bool(gold and opts.allow_drift),
        },
        "warnings": warnings,
        "live_digests": live,
    }


def compute_metrics(
    items: list[dict[str, Any]], config: EvalConfig, opts: RunOptions, pricing: cost.Pricing
) -> dict[str, Any]:
    """Aggregate metrics (design D7). Errored requests count as zero retrieval, a wrong route and a
    wrong SQL answer; they are excluded from latency and the confusion matrix and reported as
    errors."""
    ok = [i for i in items if i["response"] is not None]

    per_q = []
    for i in items:
        if not i["record"].gold_chunks:
            continue
        ranked = [(c["external_id"], c["ordinal"]) for c in (i["response"] or {}).get("chunks", [])]
        per_q.append(retrieval.retrieval_metrics([g.key for g in i["record"].gold_chunks], ranked))

    route_pairs_all = [
        (i["record"].expected_route, (i["response"] or {}).get("route")) for i in items
    ]
    route_correct = [e == a for e, a in route_pairs_all]
    confusion_pairs = [(e, a) for e, a in route_pairs_all if a in routing.ROUTES]
    sql_results = [
        routing.sql_answer_correct(i["response"] or {}, i["record"].expected_sql_answer)
        for i in items
        if i["record"].type == "aggregate"
    ]

    metrics: dict[str, Any] = {
        "retrieval": retrieval.aggregate_retrieval(per_q),
        "routing": {
            "route_accuracy": {
                "value": sum(route_correct) / len(route_correct) if route_correct else None,
                "n": len(route_correct),
            },
            "confusion": routing.confusion_matrix(confusion_pairs),
            "sql_accuracy": {"value": routing.sql_accuracy(sql_results), "n": len(sql_results)},
        },
        "latency": latency.latency_summary(
            [i["e2e_ms"] for i in ok], [i["response"].get("timings_ms", {}) for i in ok]
        ),
        "cost": cost.cost_summary([i["response"].get("usage", {}) for i in ok], pricing),
    }

    filt = [i["response"].get("filters") for i in ok if i["response"].get("filters")]
    metrics["filters"] = {
        "n": len(filt),
        "strategies": dict(Counter(f.get("strategy") for f in filt)),
        "underfilled_rate": (sum(1 for f in filt if f.get("underfilled")) / len(filt))
        if filt
        else None,
    }

    if not opts.no_generation:
        metrics["answers"] = judge.abstention_metrics(
            [(i["record"].type, i["response"]) for i in ok]
        )
        judged = [(i["record"].type, i["judge"]) for i in items if i.get("judge")]
        if judged:
            metrics["answers"].update(judge.aggregate_judgements(judged))
    return metrics


def _question_entry(i: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": i["record"].id,
        "type": i["record"].type,
        "request": i["request"],
        "http_status": i["status"],
        "error": i["error"],
        "e2e_ms": round(i["e2e_ms"], 2),
        "response": compact_response(i["response"]) if i["response"] is not None else None,
        "judge": i.get("judge"),
    }


def run_config(
    config_path: str | Path,
    opts: RunOptions,
    ctx: RunContext,
    *,
    run_id: str | None = None,
) -> RunOutcome:
    try:
        config, config_sha = load_config(config_path)
    except ConfigError as exc:
        raise SetupError(str(exc)) from exc

    try:
        dataset = load_dataset(ctx.dataset_path)
    except (OSError, ValueError) as exc:
        raise SetupError(str(exc)) from exc
    records = select_questions(dataset, config, opts)
    if not records:
        raise SetupError("no questions selected (check --subset / --limit / question_types)")

    pipeline = config.pipeline_payload()
    if opts.no_generation:
        pipeline = copy.deepcopy(pipeline)
        pipeline["generation"]["enabled"] = False

    pre = _preflight(config, records, opts, ctx)
    run_id = run_id or make_run_id(ctx)
    started = ctx.now()

    for q in ctx.warmup_questions[: config.run.warmup_queries]:
        ctx.service.query(q, pipeline, timeout_s=config.run.timeout_s)  # untimed

    items: list[dict[str, Any]] = []
    for rec in records:
        res = ctx.service.query(rec.question, pipeline, timeout_s=config.run.timeout_s)
        items.append(
            {
                "record": rec,
                "request": {"question": rec.question, "pipeline": pipeline},
                "e2e_ms": res.e2e_ms,
                "status": res.status,
                "error": res.error,
                "response": res.body if res.ok else None,
                "judge": None,
            }
        )

    judging = config.judge.enabled and not opts.no_judge and not opts.no_generation
    if judging:
        llm = ctx.judge_factory(config.judge)
        for i in items:
            if i["response"] is not None:
                i["judge"] = judge.score_answer(
                    llm,
                    qtype=i["record"].type,
                    question=i["record"].question,
                    response=i["response"],
                    context_k=pipeline["generation"]["context_k"],
                )

    metrics = compute_metrics(items, config, opts, cost.load_pricing(ctx.pricing_path))

    degraded_codes: Counter[str] = Counter()
    degraded_n = 0
    for i in items:
        codes = (i["response"] or {}).get("degraded") or []
        if codes:
            degraded_n += 1
            degraded_codes.update(codes)
    errors_n = sum(1 for i in items if i["response"] is None)
    n = len(items)

    full_dataset = (
        opts.subset is None
        and opts.limit is None
        and set(config.question_types) == set(QUESTION_TYPES)
        and not opts.replay
        and not opts.no_generation
        and not opts.no_judge
    )
    finished = ctx.now()
    record: dict[str, Any] = {
        "run_id": run_id,
        "config": {
            "name": config.name,
            "grid_row": config.grid_row,
            "description": config.description,
            "sha256": config_sha,
            "content": config.model_dump(mode="json"),
        },
        "pipeline_sent": pipeline,
        "dataset": {
            "path": str(ctx.dataset_path.relative_to(ctx.evals_dir))
            if ctx.dataset_path.is_relative_to(ctx.evals_dir)
            else str(ctx.dataset_path),
            "sha256": sha256_file(ctx.dataset_path),
            "total_records": len(dataset),
            "questions_run": n,
        },
        "seed": config.run.seed,
        "git": ctx.git_info(),
        "flags": {
            "subset": opts.subset,
            "limit": opts.limit,
            "no_generation": opts.no_generation,
            "no_judge": opts.no_judge,
            "replay": opts.replay,
            "allow_model_drift": opts.allow_model_drift,
            "allow_drift": opts.allow_drift,
        },
        "replayed": opts.replay,
        "results_eligible": full_dataset,
        "model_digests": pre["model_digests"],
        "gold_drift": pre["gold_drift"],
        "warnings": pre["warnings"],
        "service_info": pre["service_info"],
        "judge": {
            "enabled": judging,
            "provider": config.judge.provider,
            "model": config.judge.model,
            "digest": pre["live_digests"].get(config.judge.model) if judging else None,
        },
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "metrics": metrics,
        "degraded": {
            "count": degraded_n,
            "rate": degraded_n / n,
            "by_code": dict(degraded_codes),
        },
        "errors": {
            "count": errors_n,
            "rate": errors_n / n,
            "details": [
                {"id": i["record"].id, "error": i["error"]} for i in items if i["response"] is None
            ],
        },
        "questions": [_question_entry(i) for i in items],
    }

    out_dir = ctx.evals_dir / "runs" / ("ci" if opts.subset == "ci" else "") / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{config.name}.json"
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")

    if record["results_eligible"]:
        from evals.report import write_results

        write_results(ctx.evals_dir)
    return RunOutcome(path, record)


@dataclass
class GridOutcome:
    run_id: str
    outcomes: list[RunOutcome] = field(default_factory=list)
    summary_path: Path | None = None
