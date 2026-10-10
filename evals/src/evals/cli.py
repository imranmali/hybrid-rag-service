"""`evals` command line. Exit codes everywhere: 0 ok, 1 threshold breach, 2 degraded/failed
requests under --gate, 3 setup / validation / drift error."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

import typer

from evals import cassettes as cassettes_mod
from evals import ci_lock as ci_lock_mod
from evals import drift, filter_bench, gate, judge_agreement, report, thresholds
from evals.client import ServiceClient
from evals.config_schema import ConfigError
from evals.dataset import check_filters, probe, schema, work
from evals.dataset import corpus as corpus_mod
from evals.dataset import freeze as freeze_mod
from evals.dataset import generate as generate_mod
from evals.dataset import precheck as precheck_mod
from evals.dataset import review as review_mod
from evals.dataset import sample as sample_mod
from evals.dataset.scope import record_scope
from evals.grid import run_grid
from evals.metrics.cost import Pricing  # noqa: F401  (re-exported for convenience)
from evals.ollama import LLMError, default_generator_llm, fetch_digests
from evals.runner import RunOptions, SetupError, default_context, git_info, run_config
from evals.settings import EVALS_DIR, get_settings

app = typer.Typer(help="Eval harness for the hybrid RAG service.", no_args_is_help=True)
dataset_app = typer.Typer(
    help="Build, verify and validate the frozen eval dataset.", no_args_is_help=True
)
cassettes_app = typer.Typer(help="Record the CI model-traffic cassettes.", no_args_is_help=True)
app.add_typer(dataset_app, name="dataset")
app.add_typer(cassettes_app, name="cassettes")

SETUP_ERRORS = (
    SetupError,
    ConfigError,
    thresholds.ThresholdError,
    cassettes_mod.CassetteError,
    ci_lock_mod.LockError,
    freeze_mod.FreezeError,
    review_mod.VerdictError,
    LLMError,
)

SubsetOpt = Annotated[str | None, typer.Option("--subset", help="'ci' = the 50-question CI subset")]
LimitOpt = Annotated[
    int | None, typer.Option("--limit", help="First N questions, interleaved across types")
]
NoGenOpt = Annotated[
    bool, typer.Option("--no-generation", help="Disable generation in the sent pipeline")
]
NoJudgeOpt = Annotated[bool, typer.Option("--no-judge", help="Skip the LLM judge")]
ReplayOpt = Annotated[
    bool, typer.Option("--replay", help="Model digests from cassettes/ci/meta.json")
]
ModelDriftOpt = Annotated[
    bool, typer.Option("--allow-model-drift", help="Do not fail on Ollama digest drift")
]
DriftOpt = Annotated[bool, typer.Option("--allow-drift", help="Do not fail on gold chunk drift")]


def _fail(message: str) -> None:
    typer.secho(f"error: {message}", fg=typer.colors.RED, err=True)
    raise typer.Exit(3)


def guarded(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Map setup/validation errors to exit code 3."""
    import functools

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except SETUP_ERRORS as exc:
            _fail(str(exc))
        except OSError as exc:
            _fail(str(exc))

    return wrapper


def _resolve(path: Path) -> Path:
    """Relative paths are taken from the cwd, falling back to the evals/ directory."""
    if path.is_absolute() or path.exists():
        return path
    alt = EVALS_DIR / path
    return alt if alt.exists() else path


def _options(
    subset: str | None, limit: int | None, no_generation: bool, no_judge: bool, replay: bool,
    allow_model_drift: bool, allow_drift: bool,
) -> RunOptions:  # fmt: skip
    if subset not in (None, "ci"):
        _fail("--subset must be 'ci'")
    return RunOptions(
        subset, limit, no_generation, no_judge, replay, allow_model_drift, allow_drift
    )


def _print_outcome(outcome: Any) -> None:
    rec = outcome.record
    m = rec["metrics"]
    typer.echo(
        f"{rec['config']['name']}: nDCG@10={_fmt(m['retrieval']['ndcg_at_10']['mean'])} "
        f"R@20={_fmt(m['retrieval']['recall_at_20']['mean'])} "
        f"route_acc={_fmt(m['routing']['route_accuracy']['value'])} "
        f"degraded={rec['degraded']['count']} errors={rec['errors']['count']} -> {outcome.path}"
    )


def _fmt(v: float | None) -> str:
    return "n/a" if v is None else f"{v:.3f}"


# --------------------------------------------------------------------------- run / grid / report


@app.command("run")
@guarded
def run_cmd(
    config: Annotated[Path, typer.Option("--config", help="Eval config YAML (contract C7)")],
    subset: SubsetOpt = None,
    limit: LimitOpt = None,
    no_generation: NoGenOpt = False,
    no_judge: NoJudgeOpt = False,
    replay: ReplayOpt = False,
    allow_model_drift: ModelDriftOpt = False,
    allow_drift: DriftOpt = False,
) -> None:
    """Run one config against the live service."""
    opts = _options(subset, limit, no_generation, no_judge, replay, allow_model_drift, allow_drift)
    outcome = run_config(_resolve(config), opts, default_context())
    _print_outcome(outcome)


@app.command("grid")
@guarded
def grid_cmd(
    subset: SubsetOpt = None,
    limit: LimitOpt = None,
    no_generation: NoGenOpt = False,
    no_judge: NoJudgeOpt = False,
    replay: ReplayOpt = False,
    allow_model_drift: ModelDriftOpt = False,
    allow_drift: DriftOpt = False,
    gate_file: Annotated[
        Path | None, typer.Option("--gate", help="Thresholds file; sets the exit code")
    ] = None,
) -> None:
    """Run the six grid configs in order (optionally gating on a thresholds file)."""
    opts = _options(subset, limit, no_generation, no_judge, replay, allow_model_drift, allow_drift)
    thresholds_data = thresholds.load_thresholds(_resolve(gate_file)) if gate_file else None
    grid = run_grid(EVALS_DIR / "configs", opts, default_context())
    for outcome in grid.outcomes:
        _print_outcome(outcome)
    typer.echo(f"grid summary: {grid.summary_path}")
    if thresholds_data is not None:
        result = gate.evaluate(
            thresholds_data,
            {o.record["config"]["name"]: o.record for o in grid.outcomes},
            replay=replay,
        )
        typer.echo(gate.format_table(result))
        raise typer.Exit(result.exit_code)


@app.command("report")
@guarded
def report_cmd() -> None:
    """Rebuild results.md from the latest full run per config."""
    path = report.write_results(EVALS_DIR)
    typer.echo(f"wrote {path}")


@app.command("filter-bench")
@guarded
def filter_bench_cmd(
    limit: LimitOpt = None,
    allow_model_drift: ModelDriftOpt = False,
    allow_drift: DriftOpt = False,
) -> None:
    """Pre- vs post-filter benchmark -> results_filter_pushdown.md."""
    opts = RunOptions(limit=limit, allow_model_drift=allow_model_drift, allow_drift=allow_drift)
    path, outcomes = filter_bench.run_filter_bench(EVALS_DIR / "configs", opts, default_context())
    for o in outcomes.values():
        _print_outcome(o)
    typer.echo(f"wrote {path}")


@app.command("thresholds")
@guarded
def thresholds_cmd(
    from_dir: Annotated[
        Path, typer.Option("--from", help="Baseline run directory, e.g. runs/ci/<run_id>")
    ],
    out: Annotated[Path, typer.Option("--out")] = EVALS_DIR / "thresholds" / "ci.yaml",
) -> None:
    """Write the CI thresholds from a real subset baseline run."""
    data = thresholds.write_thresholds(_resolve(from_dir), out)
    typer.echo(f"wrote {out} (baseline {data['baseline_run']})")


@app.command("ci-lock")
@guarded
def ci_lock_cmd(
    snapshot: Annotated[
        Path, typer.Option("--snapshot", help="data/snapshots/rag-index-<12hex>.dump")
    ],
    release_tag: Annotated[str | None, typer.Option("--release-tag")] = None,
    out: Annotated[Path, typer.Option("--out")] = EVALS_DIR / "ci" / "snapshot.lock.json",
) -> None:
    """Describe the DB snapshot the cassettes were recorded against."""
    stats = ci_lock_mod.PostgresCorpusStats(get_settings().evals_database_url)
    lock = ci_lock_mod.write_lock(snapshot, out, stats, release_tag)
    typer.echo(f"wrote {out}\n{json.dumps(lock, indent=2)}")


@app.command("judge-agreement")
@guarded
def judge_agreement_cmd() -> None:
    """Judge vs human agreement (pending until dataset/human_labels.jsonl exists)."""
    typer.echo(judge_agreement.report())


# --------------------------------------------------------------------------- cassettes


@cassettes_app.command("snapshot")
@guarded
def cassettes_snapshot_cmd(
    wiremock_url: Annotated[str | None, typer.Option("--wiremock-url")] = None,
    snapshot: Annotated[
        Path | None,
        typer.Option("--snapshot", help="DB snapshot file (else ci/snapshot.lock.json)"),
    ] = None,
    cassette_dir: Annotated[Path, typer.Option("--cassette-dir")] = EVALS_DIR / "cassettes" / "ci",
) -> None:
    """Capture the proxied model/reranker traffic from wiremock into cassettes/ci/."""
    settings = get_settings()
    snapshot_sha = (
        cassettes_mod.snapshot_sha_from_file(snapshot)
        if snapshot
        else cassettes_mod.snapshot_sha_from_lock(EVALS_DIR / "ci" / "snapshot.lock.json")
    )
    lock = drift.load_models_lock(drift_lock_path())
    live = fetch_digests(settings.ollama_host_url)
    digests = {name: live.get(name) for name in drift.lock_digests(lock)}
    info = ServiceClient(settings.rag_service_url).info()
    reranker = (info.get("models") or {}).get("reranker") or {}
    meta = cassettes_mod.snapshot_cassettes(
        wiremock_url=wiremock_url or settings.wiremock_admin_url,
        cassette_dir=cassette_dir,
        git_sha=git_info()["sha"],
        model_digests=digests,
        reranker=reranker,
        snapshot_sha256=snapshot_sha,
    )
    typer.echo(f"recorded {meta['mapping_count']} mappings into {cassette_dir}/mappings")


def drift_lock_path() -> Path:
    from evals.settings import REPO_ROOT

    return REPO_ROOT / "infra" / "models.lock.json"


# --------------------------------------------------------------------------- dataset


def _corpus() -> corpus_mod.PostgresCorpus:
    return corpus_mod.PostgresCorpus(get_settings().evals_database_url)


def _service() -> ServiceClient:
    return ServiceClient(get_settings().rag_service_url)


def _extractor() -> check_filters.Extractor:
    client = _service()
    return lambda q: probe.extracted_filters(client, q)


@dataset_app.command("sample")
@guarded
def dataset_sample(
    seed: Annotated[int, typer.Option("--seed")] = 42,
    oversample: Annotated[float, typer.Option("--oversample", help="Samples per quota slot")] = 2.0,
) -> None:
    """Seeded chunk samples per type (default 2x oversampling) -> work/samples.jsonl."""
    corpus = _corpus()
    record_scope(corpus, "sample", seed=seed, oversample=oversample)
    rows = sample_mod.sample_all(corpus, seed=seed, oversample=oversample)
    sample_mod.write_samples(work.SAMPLES, rows)
    counts = {t: sum(1 for r in rows if r.type == t) for t in sample_mod.SAMPLERS}
    typer.echo(f"wrote {len(rows)} samples to {work.SAMPLES}: {counts}")


@dataset_app.command("generate")
@guarded
def dataset_generate(
    qtype: Annotated[str | None, typer.Option("--type", help="Only this question type")] = None,
    n: Annotated[int | None, typer.Option("--n", help="New candidates to add")] = None,
    seed: Annotated[int, typer.Option("--seed")] = 42,
    oversample: Annotated[
        float, typer.Option("--oversample", help="Candidates per quota slot")
    ] = 2.0,
) -> None:
    """LLM candidates (+ SQL-template aggregates) -> work/candidates.jsonl."""
    if qtype is not None and qtype not in schema.QUOTA:
        _fail(f"--type must be one of {list(schema.QUOTA)}")
    corpus = _corpus()
    record_scope(corpus, "generate", type=qtype, oversample=oversample)
    candidates = work.read_jsonl(work.CANDIDATES)
    samples = sample_mod.read_samples(work.SAMPLES)
    llm = default_generator_llm()
    for t in [qtype] if qtype else list(schema.QUOTA):
        have = sum(1 for c in candidates if c["type"] == t)
        want = (
            n
            if n is not None
            else max(
                round(schema.QUOTA[t] * oversample) - have,
                0 if qtype is None else schema.QUOTA[t] // 2,
            )
        )
        if want <= 0:
            typer.echo(f"{t}: already {have} candidates")
            continue
        new, samples, stats = generate_mod.generate_type(
            llm,
            corpus,
            t,
            want,
            existing_candidates=candidates,
            existing_samples=samples,
            seed=seed,
        )
        candidates += new
        work.write_jsonl(work.CANDIDATES, candidates)
        sample_mod.write_samples(work.SAMPLES, samples)
        typer.echo(
            f"{t}: +{len(new)} candidates (total {have + len(new)}), dropped {stats.dropped or 0}"
        )


@dataset_app.command("precheck")
@guarded
def dataset_precheck() -> None:
    """LLM answerability check for every candidate -> work/precheck.jsonl."""
    candidates = work.read_jsonl(work.CANDIDATES)
    # entries that errored (e.g. Ollama restarted mid-run) are retried; the later entry supersedes
    done = {
        c
        for c, p in review_mod.latest_by_candidate(work.read_jsonl(work.PRECHECK)).items()
        if not p.get("error")
    }
    client = _service()
    entries = precheck_mod.run_precheck(
        default_generator_llm(), _corpus(), candidates, lambda q: probe.top_chunks(client, q), done
    )
    for e in entries:
        work.append_jsonl(work.PRECHECK, e)
    passed = sum(1 for e in entries if e["passed"])
    typer.echo(f"prechecked {len(entries)} candidates, {passed} passed")


@dataset_app.command("check-filters")
@guarded
def dataset_check_filters(candidate_id: Annotated[str | None, typer.Option("--id")] = None) -> None:
    """Compare each filter candidate's expected filters with the service's extracted filters."""
    cands = [c for c in work.read_jsonl(work.CANDIDATES) if c["type"] == "filter"]
    if candidate_id:
        cands = [c for c in cands if c["candidate_id"] == candidate_id]
    results = check_filters.run_check_filters(cands, _extractor())
    for r in results:
        work.append_jsonl(work.FILTER_CHECKS, r)
    bad = [r["candidate_id"] for r in results if not r["match"]]
    typer.echo(
        f"checked {len(results)} filter candidates, {len(bad)} flagged: {', '.join(bad[:20])}"
    )


@dataset_app.command("review")
@guarded
def dataset_review(
    next_n: Annotated[
        int | None, typer.Option("--next", help="Show the next N unverified candidates")
    ] = None,
    candidate_id: Annotated[str | None, typer.Option("--id")] = None,
    max_chars: Annotated[int | None, typer.Option("--max-chars", help="Clip each passage")] = None,
) -> None:
    """Print candidates with the evidence a verifier needs."""
    candidates = work.read_jsonl(work.CANDIDATES)
    prechecks = work.read_jsonl(work.PRECHECK)
    verdicts = work.read_jsonl(work.VERIFICATION)
    if candidate_id:
        chosen = [c for c in candidates if c["candidate_id"] == candidate_id]
    else:
        chosen = review_mod.pending_candidates(candidates, prechecks, verdicts)[: next_n or 20]
    pre = review_mod.latest_by_candidate(prechecks)
    fchecks = review_mod.latest_by_candidate(work.read_jsonl(work.FILTER_CHECKS))
    corpus = _corpus()
    client = _service()
    for c in chosen:
        typer.echo(
            review_mod.render_review(
                c,
                pre.get(c["candidate_id"]),
                fchecks.get(c["candidate_id"]),
                corpus,
                lambda q: probe.top_chunks(client, q),
                max_chars,
            )
        )
    remaining = len(review_mod.pending_candidates(candidates, prechecks, verdicts))
    typer.echo(f"\n{remaining} candidate(s) still without a verdict")


@dataset_app.command("verdict")
@guarded
def dataset_verdict(
    candidate_id: Annotated[str, typer.Option("--id")],
    status: Annotated[str, typer.Option("--status", help="accept | reject | edit")],
    notes: Annotated[str, typer.Option("--notes")] = "",
    question: Annotated[str | None, typer.Option("--question")] = None,
    answer: Annotated[str | None, typer.Option("--answer")] = None,
    by: Annotated[
        str | None, typer.Option("--by", help="Verifier id, e.g. agent:<model id>")
    ] = None,
) -> None:
    """Append a verification verdict to work/verification.jsonl."""
    entry = review_mod.record_verdict(
        candidate_id=candidate_id,
        status=status,
        by=by or os.environ.get("EVALS_VERIFIER", ""),
        notes=notes,
        question=question,
        answer=answer,
        extractor=_extractor() if question else None,
    )
    typer.echo(f"{entry['candidate_id']}: {entry['status']} by {entry['by']}")


@dataset_app.command("freeze")
@guarded
def dataset_freeze(
    version: Annotated[str, typer.Option("--version")] = "v1",
    seed: Annotated[int, typer.Option("--seed")] = 42,
) -> None:
    """Freeze accepted candidates into questions.<version>.jsonl + MANIFEST + card."""
    settings = get_settings()
    llm = default_generator_llm(settings)
    try:
        digest = fetch_digests(settings.ollama_host_url).get(llm.model)
    except LLMError:
        digest = None
    inp = freeze_mod.FreezeInputs(
        candidates=work.read_jsonl(work.CANDIDATES),
        prechecks=work.read_jsonl(work.PRECHECK),
        verdicts=work.read_jsonl(work.VERIFICATION),
        generator_digest=digest,
        pipeline_fingerprint=ci_lock_mod.PostgresCorpusStats(
            settings.evals_database_url
        ).pipeline_fingerprint(),
        scope=work.read_jsonl(work.SCOPE),
        corpus=_corpus(),
    )
    manifest = freeze_mod.freeze(
        inp, drift.PostgresGoldStore(settings.evals_database_url), version=version, seed=seed
    )
    typer.echo(f"froze questions.{version}.jsonl: {manifest['counts']} sha256 {manifest['sha256']}")


@dataset_app.command("validate")
@guarded
def dataset_validate(
    path: Annotated[Path, typer.Option("--path")] = work.FROZEN,
    manifest: Annotated[Path, typer.Option("--manifest")] = work.MANIFEST,
) -> None:
    """Schema, gold rules, exact type counts and manifest hash of the frozen dataset."""
    report_ = schema.validate_dataset(path, manifest)
    if not report_.ok:
        typer.secho(f"INVALID: {len(report_.errors)} problem(s)", fg=typer.colors.RED, err=True)
        for e in report_.errors:
            typer.echo(f"  {e}", err=True)
        raise typer.Exit(3)
    typer.echo(
        f"OK: {len(report_.records)} valid records, counts {report_.counts}, "
        f"ci_subset {report_.ci_counts}, sha256 {report_.sha256} matches the manifest"
    )
