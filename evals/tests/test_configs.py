from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml
from conftest import CONFIGS_DIR

from evals.config_schema import ConfigError, load_config

# Contract C7 grid table: (grid_row, name, vector, bm25, filters, contextual, rerank, routing)
GRID_TABLE = [
    (1, "01-vector", True, False, False, False, False, False),
    (2, "02-hybrid", True, True, False, False, False, False),
    (3, "03-filters", True, True, True, False, False, False),
    (4, "04-contextual", True, True, True, True, False, False),
    (5, "05-rerank", True, True, True, True, True, False),
    (6, "06-routing", True, True, True, True, True, True),
]
ALL_NAMES = [r[1] for r in GRID_TABLE] + ["filter-pre", "filter-post", "filter-auto"]


@pytest.mark.parametrize("name", ALL_NAMES)
def test_all_nine_configs_load(name):
    config, sha = load_config(CONFIGS_DIR / f"{name}.yaml")
    assert config.name == name
    assert len(sha) == 64


def test_exactly_nine_config_files():
    assert sorted(p.stem for p in CONFIGS_DIR.glob("*.yaml")) == sorted(ALL_NAMES)


@pytest.mark.parametrize(
    ("row", "name", "vector", "bm25", "filters", "ctx", "rerank", "routing"), GRID_TABLE
)
def test_grid_switches_match_contract_table(row, name, vector, bm25, filters, ctx, rerank, routing):
    c, _ = load_config(CONFIGS_DIR / f"{name}.yaml")
    p = c.pipeline
    assert c.grid_row == row
    assert (p.retrieval.vector, p.retrieval.bm25) == (vector, bm25)
    assert p.filters.enabled is filters
    assert p.contextual is ctx
    assert p.rerank.enabled is rerank
    assert p.routing.enabled is routing
    assert p.generation.enabled is True  # generation on in every grid row
    assert c.judge.enabled is True


def test_grid_configs_differ_only_in_the_switches():
    base = None
    for _, name, *_ in GRID_TABLE:
        c, _ = load_config(CONFIGS_DIR / f"{name}.yaml")
        d = c.pipeline_payload()
        for path in (
            ("retrieval", "vector"),
            ("retrieval", "bm25"),
            ("filters", "enabled"),
            ("rerank", "enabled"),
            ("routing", "enabled"),
        ):
            d[path[0]].pop(path[1])
        d.pop("contextual")
        if base is None:
            base = d
        assert d == base


@pytest.mark.parametrize("strategy", ["pre", "post", "auto"])
def test_filter_bench_configs(strategy):
    c, _ = load_config(CONFIGS_DIR / f"filter-{strategy}.yaml")
    row4, _ = load_config(CONFIGS_DIR / "04-contextual.yaml")
    assert c.grid_row is None
    assert c.question_types == ["filter"]
    assert c.pipeline.filters.strategy == strategy
    assert c.pipeline.generation.enabled is False
    assert c.judge.enabled is False
    expected = row4.pipeline_payload()
    expected["filters"]["strategy"] = strategy
    expected["generation"]["enabled"] = False
    assert c.pipeline_payload() == expected


def test_default_pipeline_matches_contract_numbers():
    c, _ = load_config(CONFIGS_DIR / "06-routing.yaml")
    d = c.pipeline_payload()
    assert d["retrieval"] == {
        "vector": True,
        "bm25": True,
        "vector_k": 50,
        "bm25_k": 50,
        "fusion": {"method": "rrf", "k": 60},
        "return_k": 50,
    }
    assert d["rerank"] == {"enabled": True, "candidates": 20}
    assert d["generation"] == {"enabled": True, "context_k": 6}
    assert d["filters"]["explicit"] is None


def _variant(tmp_path: Path, mutate) -> Path:
    data = yaml.safe_load((CONFIGS_DIR / "05-rerank.yaml").read_text())
    mutate(data)
    path = tmp_path / "05-rerank.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


def test_missing_pipeline_key_is_rejected_and_named(tmp_path):
    path = _variant(tmp_path, lambda d: d["pipeline"]["rerank"].pop("candidates"))
    with pytest.raises(ConfigError, match=r"pipeline\.rerank\.candidates: missing key"):
        load_config(path)


def test_missing_pipeline_block_is_rejected(tmp_path):
    path = _variant(tmp_path, lambda d: d.pop("pipeline"))
    with pytest.raises(ConfigError, match="pipeline: missing key"):
        load_config(path)


def test_missing_explicit_key_is_rejected(tmp_path):
    path = _variant(tmp_path, lambda d: d["pipeline"]["filters"].pop("explicit"))
    with pytest.raises(ConfigError, match=r"filters\.explicit: missing key"):
        load_config(path)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["pipeline"].update(rerankk={"enabled": True}),
        lambda d: d["pipeline"]["retrieval"].update(bm_25=True),
        lambda d: d["pipeline"]["retrieval"]["fusion"].update(extra=1),
        lambda d: d.update(surprise=True),
        lambda d: d["judge"].update(temperature=1),
        lambda d: d["run"].update(workers=4),
    ],
)
def test_unknown_key_is_rejected(tmp_path, mutate):
    path = _variant(tmp_path, mutate)
    with pytest.raises(ConfigError, match="unknown key"):
        load_config(path)


def test_name_must_equal_filename_stem(tmp_path):
    path = _variant(tmp_path, lambda d: d.update(name="something-else"))
    with pytest.raises(ConfigError, match="filename stem"):
        load_config(path)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["pipeline"]["retrieval"].update(vector=False, bm25=False),
        lambda d: d["pipeline"]["rerank"].update(candidates=101),
        lambda d: (
            d["pipeline"]["rerank"].update(candidates=60)
            or d["pipeline"]["retrieval"].update(return_k=50)
        ),
        lambda d: d["pipeline"]["filters"].update(strategy="sideways"),
        lambda d: (
            d["pipeline"]["filters"].update(overfetch_factor=20)
            or d["pipeline"]["retrieval"].update(vector_k=100)
        ),
        lambda d: d["pipeline"]["generation"].update(context_k=21),
        lambda d: d.update(question_types=["nonsense"]),
    ],
)
def test_service_validation_rules_are_enforced_client_side(tmp_path, mutate):
    with pytest.raises(ConfigError):
        load_config(_variant(tmp_path, mutate))


def test_pipeline_payload_is_the_complete_pipeline_object():
    c, _ = load_config(CONFIGS_DIR / "05-rerank.yaml")
    raw = yaml.safe_load((CONFIGS_DIR / "05-rerank.yaml").read_text())
    assert c.pipeline_payload() == raw["pipeline"]


def test_config_sha_is_of_the_file_bytes(tmp_path):
    import hashlib

    path = _variant(tmp_path, lambda d: copy.deepcopy(d))
    _, sha = load_config(path)
    assert sha == hashlib.sha256(path.read_bytes()).hexdigest()
