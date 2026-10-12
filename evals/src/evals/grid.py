"""Runs the six grid configs in order under one run id; optional CI gate."""

from __future__ import annotations

import json
from pathlib import Path

from evals.config_schema import ConfigError, load_config
from evals.runner import GridOutcome, RunContext, RunOptions, SetupError, make_run_id, run_config


def grid_config_paths(configs_dir: Path) -> list[Path]:
    """Config files that carry a `grid_row`, ordered by it."""
    rows: list[tuple[int, Path]] = []
    for path in sorted(configs_dir.glob("*.yaml")):
        try:
            config, _ = load_config(path)
        except ConfigError as exc:
            raise SetupError(str(exc)) from exc
        if config.grid_row is not None:
            rows.append((config.grid_row, path))
    if not rows:
        raise SetupError(f"no grid configs found in {configs_dir}")
    return [p for _, p in sorted(rows)]


def run_grid(configs_dir: Path, opts: RunOptions, ctx: RunContext) -> GridOutcome:
    run_id = make_run_id(ctx)
    paths = grid_config_paths(configs_dir)  # validates every config before the first request
    grid = GridOutcome(run_id=run_id)
    for path in paths:
        grid.outcomes.append(run_config(path, opts, ctx, run_id=run_id))
    out_dir = grid.outcomes[0].path.parent
    summary = {
        "run_id": run_id,
        "flags": grid.outcomes[0].record["flags"],
        "git": grid.outcomes[0].record["git"],
        "configs": [
            {
                "name": o.record["config"]["name"],
                "file": o.path.name,
                "ndcg_at_10": o.record["metrics"]["retrieval"]["ndcg_at_10"]["mean"],
                "recall_at_20": o.record["metrics"]["retrieval"]["recall_at_20"]["mean"],
                "degraded": o.degraded_count,
                "errors": o.error_count,
            }
            for o in grid.outcomes
        ],
    }
    grid.summary_path = out_dir / "grid.json"
    grid.summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return grid
