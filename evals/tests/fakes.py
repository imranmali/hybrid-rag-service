"""Test doubles for the runner: a fake query service (respx), fake DB, fake judge LLM."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import respx
import yaml
from conftest import CONFIGS_DIR, make_record, sha, write_jsonl

from evals.client import ServiceClient
from evals.ollama import ChatResult
from evals.runner import RunContext

SERVICE = "http://svc.test"
LLM_DIGEST = "b" * 64
EMB_DIGEST = "a" * 64
LOCK = {
    "ollama": {"bge-m3": {"digest": EMB_DIGEST}, "llama3.1:8b": {"digest": LLM_DIGEST}},
    "reranker": {"model": "BAAI/bge-reranker-v2-m3", "revision": "r" * 40},
}


def chunk(ext: str, ordinal: int, rank: int, text: str = "passage text") -> dict[str, Any]:
    return {
        "rank": rank,
        "chunk_id": rank * 10,
        "external_id": ext,
        "ordinal": ordinal,
        "title": "T",
        "content": f"{text} {ext}#{ordinal}",
        "context_header": "hdr",
        "scores": {"fused": 0.5, "rerank": 0.9},
    }


def ok_response(
    *, chunks=None, route="vector", sql=None, answer=None, degraded=None, filters=None, timings=None
) -> dict[str, Any]:
    return {
        "request_id": "r1",
        "trace_id": "t1",
        "route": route,
        "route_source": "llm",
        "pipeline": {},
        "filters": filters,
        "chunks": chunks or [],
        "sql": sql,
        "answer": answer
        or {"status": "ok", "http_status": 200, "text": "An answer [1]", "abstained": False,
            "citations": [10], "error": None},
        "degraded": degraded or [],
        "timings_ms": timings or {"embed": 10.0, "retrieve.vector": 5.0, "rerank": 100.0, "total": 150.0},
        "usage": {"embed_prompt_tokens": 14, "rerank_pairs": 20, "llm_prompt_tokens": 100,
                  "llm_completion_tokens": 10, "llm_calls": 1},
    }  # fmt: skip


def default_records() -> list[dict]:
    """q1 factoid (gold A), q2 aggregate, q3 unanswerable, q4 filter, q5 factoid (ci), q6 aggregate (ci)."""
    return [
        make_record("q0001", "factoid", gold=[("2401.00001", 0)]),
        make_record("q0002", "aggregate", expected_sql_answer="3"),
        make_record("q0003", "unanswerable"),
        make_record("q0004", "filter", gold=[("2401.00002", 1)]),
        make_record("q0005", "factoid", gold=[("2401.00003", 2)], ci=True),
        make_record("q0006", "aggregate", expected_sql_answer="5", ci=True),
    ]


def default_handler(body: dict[str, Any]) -> dict[str, Any]:
    """Respond like a decent service: gold at rank 1 for factoid/filter, SQL for aggregates."""
    q = body["question"]
    by_question = {r["question"]: r for r in default_records()}
    rec = by_question.get(q)
    if rec is None:  # warmup question
        return ok_response(chunks=[chunk("warm", 0, 1)])
    if rec["type"] == "aggregate":
        answer = rec["expected_sql_answer"]
        sql = {"query": "select count(*)", "columns": ["n"], "rows": [[int(answer)]],
               "row_count": 1, "truncated": False, "error": None}  # fmt: skip
        return ok_response(route="sql", sql=sql)
    if rec["type"] == "unanswerable":
        return ok_response(
            chunks=[chunk("zz", 0, 1)],
            answer={"status": "ok", "http_status": 200,
                    "text": "I cannot answer this from the indexed papers.",
                    "abstained": True, "citations": [], "error": None},
        )  # fmt: skip
    g = rec["gold_chunks"][0]
    filters = None
    if rec["type"] == "filter":
        filters = {"applied": {}, "extracted": {}, "strategy": "pre", "estimated_selectivity": 0.1,
                   "matching_chunks": 5, "underfilled": False}  # fmt: skip
    return ok_response(
        chunks=[chunk("noise", 0, 1), chunk(g["external_id"], g["ordinal"], 2)], filters=filters
    )


@dataclass
class FakeGoldStore:
    overrides: dict[tuple[str, int], str | None] = field(default_factory=dict)
    calls: int = 0

    def content_hashes(self, keys):
        self.calls += 1
        out = {}
        for k in keys:
            value = self.overrides.get(k, sha(f"{k[0]}#{k[1]}"))
            if value is not None:
                out[k] = value
        return out


class FakeJudge:
    provider = "ollama"
    model = "llama3.1:8b"

    def __init__(self, verdict_by_marker: dict[str, str] | None = None):
        self.calls: list[tuple[str, str]] = []

    def chat(self, system, user, *, num_predict, json_format=True, seed=42):
        self.calls.append((system, user))
        word = "supported" if "CONTEXT" in user else "relevant"
        return ChatResult(json.dumps({"verdict": word}), 50, 3)


class Env:
    """A temp evals dir + fake service wired into a RunContext."""

    def __init__(self, tmp_path: Path, mock: respx.MockRouter):
        self.evals_dir = tmp_path / "evals"
        self.repo_root = tmp_path
        (self.evals_dir / "dataset").mkdir(parents=True)
        (self.evals_dir / "configs").mkdir()
        (tmp_path / "infra").mkdir()
        (tmp_path / "infra" / "models.lock.json").write_text(json.dumps(LOCK))
        shutil.copy(
            Path(__file__).resolve().parents[1] / "pricing.yaml", self.evals_dir / "pricing.yaml"
        )
        for p in CONFIGS_DIR.glob("*.yaml"):
            shutil.copy(p, self.evals_dir / "configs" / p.name)
        write_jsonl(self.evals_dir / "dataset" / "questions.v1.jsonl", default_records())
        self.requests: list[dict[str, Any]] = []
        self.handler = default_handler
        self.query_status = 200
        self.info_calls = 0
        self.info_status = 200
        self.gold = FakeGoldStore()
        self.judge = FakeJudge()
        self.live_digests = {
            "bge-m3": EMB_DIGEST,
            "bge-m3:latest": EMB_DIGEST,
            "llama3.1:8b": LLM_DIGEST,
        }
        self.digest_calls = 0
        self.clock = datetime(2026, 10, 10, 12, 0, 0, tzinfo=UTC)
        mock.get(f"{SERVICE}/v1/info").mock(side_effect=self._info)
        mock.post(f"{SERVICE}/v1/query").mock(side_effect=self._query)

    def _info(self, request):
        self.info_calls += 1
        if self.info_status != 200:
            return httpx.Response(self.info_status)
        return httpx.Response(
            200,
            json={"service": {"version": "0.1.0", "git_sha": "abc1234"},
                  "models": {"reranker": {"revision": "r" * 40, "model": "BAAI/bge-reranker-v2-m3"}},
                  "index": {"pipeline_fingerprint": "f" * 64}},
        )  # fmt: skip

    def _query(self, request):
        body = json.loads(request.content)
        self.requests.append(body)
        if self.query_status != 200:
            return httpx.Response(self.query_status, json={"title": "boom"})
        return httpx.Response(200, json=self.handler(body))

    def _digests(self):
        self.digest_calls += 1
        return dict(self.live_digests)

    def _now(self):
        self.clock += timedelta(seconds=5)
        return self.clock

    def ctx(self, **overrides) -> RunContext:
        base = {
            "service": ServiceClient(SERVICE),
            "gold_store": self.gold,
            "digest_fetcher": self._digests,
            "judge_factory": lambda cfg: self.judge,
            "git_info": lambda: {"sha": "abc1234", "dirty": False},
            "now": self._now,
            "evals_dir": self.evals_dir,
            "repo_root": self.repo_root,
            "dataset_path": self.evals_dir / "dataset" / "questions.v1.jsonl",
            "pricing_path": self.evals_dir / "pricing.yaml",
            "models_lock_path": self.repo_root / "infra" / "models.lock.json",
            "cassette_meta_path": self.evals_dir / "cassettes" / "ci" / "meta.json",
        }
        base.update(overrides)
        return RunContext(**base)

    def config(self, name: str) -> Path:
        return self.evals_dir / "configs" / f"{name}.yaml"

    def edit_config(self, name: str, mutate) -> Path:
        path = self.config(name)
        data = yaml.safe_load(path.read_text())
        mutate(data)
        path.write_text(yaml.safe_dump(data))
        return path

    @property
    def results_md(self) -> Path:
        return self.evals_dir / "results.md"
