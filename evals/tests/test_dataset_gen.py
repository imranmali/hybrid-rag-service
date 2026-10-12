from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from corpus_fixture import PAPER_ROWS, FixtureCorpus

from evals.dataset import aggregate, check_filters, generate, precheck, review, sample, work
from evals.ollama import ChatResult, LLMError


@pytest.fixture
def corpus():
    return FixtureCorpus()


# --- sampling --------------------------------------------------------------------------------


def key_of(rows):
    return [(r.type, r.sample_id, r.chunk_keys(), r.phrase, r.topic) for r in rows]


def test_seeded_sampling_is_identical_twice_and_seed_sensitive(corpus):
    a = sample.sample_all(corpus, seed=42, oversample=1)
    b = sample.sample_all(corpus, seed=42, oversample=1)
    assert key_of(a) == key_of(b) and len(a) > 0
    c = sample.sample_all(corpus, seed=7, oversample=1)
    assert key_of(a) != key_of(c)


def test_sample_ids_are_unique_and_typed(corpus):
    rows = sample.sample_all(corpus)
    ids = [r.sample_id for r in rows]
    assert len(ids) == len(set(ids))
    assert all(r.sample_id.startswith(f"s-{r.type}-") for r in rows)
    assert {r.type for r in rows} == {"factoid", "multi_chunk", "filter", "unanswerable"}


def test_factoid_samples_are_long_chunks_one_per_paper_first(corpus):
    rows = sample.draw_samples(corpus, "factoid", 8)
    assert len(rows) == 8
    assert len({r.chunk_keys()[0][0] for r in rows}) == 8  # one chunk per paper on the first pass
    assert len({r.chunk_keys()[0] for r in rows}) == 8
    short = FixtureCorpus(tokens=50)
    assert sample.draw_samples(short, "factoid", 5) == []  # token_count < 150 is never sampled


def test_multi_chunk_samples_come_from_one_paper_in_different_sections(corpus):
    rows = sample.draw_samples(corpus, "multi_chunk", 10)
    assert rows
    for r in rows:
        assert 2 <= len(r.chunks) <= 4
        assert len({c["external_id"] for c in r.chunks}) == 1
        tops = [c["section_path"].split(" > ")[0] for c in r.chunks]
        assert len(set(tops)) == len(tops)


def test_filter_samples_carry_phrase_and_expected_filters(corpus):
    rows = sample.draw_samples(corpus, "filter", 15)
    kinds = {r.phrase["kind"] for r in rows}
    assert kinds == {"year", "author", "category_year"}
    for r in rows:
        ext = r.chunks[0]["external_id"]
        paper = next(p for p in corpus.papers() if p.external_id == ext)
        text, ef = r.phrase["text"], r.phrase["expected_filters"]
        if r.phrase["kind"] == "year":
            assert text == f"in {paper.year}"
            assert ef == {
                "published_from": f"{paper.year}-01-01",
                "published_to": f"{paper.year}-12-31",
            }
        elif r.phrase["kind"] == "author":
            (norm,) = ef["authors"]
            assert text.startswith("by ") and any(norm == n for _, n in paper.authors)
            assert sum(1 for p in corpus.papers() if any(n == norm for _, n in p.authors)) >= 2
        else:
            assert text == f"{paper.primary_category} papers from {paper.year}"
            assert ef["categories"] == [paper.primary_category]


def test_unanswerable_samples_are_topic_seeds_without_chunks(corpus):
    rows = sample.draw_samples(corpus, "unanswerable", 6)
    assert len(rows) == 6 and all(r.chunks == [] for r in rows)
    assert {r.topic["category"] for r in rows} <= {"cs.IR", "cs.CL", "cs.DB"}
    assert len({(r.topic["category"], r.topic["angle"]) for r in rows}) == 6


def test_draw_extends_without_repeating_existing_samples(corpus):
    first = sample.draw_samples(corpus, "factoid", 4)
    more = sample.draw_samples(corpus, "factoid", 4, existing=first)
    assert not {k for r in first for k in r.chunk_keys()} & {
        k for r in more for k in r.chunk_keys()
    }
    assert [r.sample_id for r in more] == [f"s-factoid-{i:04d}" for i in range(5, 9)]


def test_samples_round_trip_through_jsonl(corpus, tmp_path):
    rows = sample.sample_all(corpus)
    sample.write_samples(tmp_path / "s.jsonl", rows)
    assert key_of(sample.read_samples(tmp_path / "s.jsonl")) == key_of(rows)
    assert sample.read_samples(tmp_path / "none.jsonl") == []


# --- aggregate templates -----------------------------------------------------------------------


def expected_count(predicate):
    return sum(1 for row in PAPER_ROWS if predicate(row))


def test_t3_answer_equals_fixture_sql_result(corpus):
    cand = aggregate.t3(corpus, "Jimmy Lin", "jimmy lin", 2024)
    independent = expected_count(
        lambda r: r[4].startswith("2024") and any(n == "jimmy lin" for _, n in r[5])
    )
    assert independent == 2
    assert cand.expected_sql_answer == str(independent)
    assert cand.question == "How many papers did Jimmy Lin publish in 2024?"
    assert cand.template == "T3" and cand.params["author"] == "jimmy lin"
    # the stored SQL really produces the stored answer (never LLM-computed)
    assert str(corpus.fetch(cand.sql, cand.params)[0][0]) == cand.expected_sql_answer


def test_t1_t2_t6_counts(corpus):
    assert aggregate.t1(corpus, "Jimmy Lin", "jimmy lin").expected_sql_answer == "4"
    assert aggregate.t2(corpus, "cs.IR", 2025).expected_sql_answer == str(
        expected_count(lambda r: r[4].startswith("2025") and "cs.IR" in r[3])
    )
    assert aggregate.t6(corpus, "cs.CL", "cs.IR").expected_sql_answer == str(
        expected_count(lambda r: {"cs.CL", "cs.IR"} <= set(r[3]))
    )
    assert aggregate.t1(corpus, "Nobody", "nobody") is None  # zero-count answers are not asked
    assert aggregate.t2(corpus, "cs.IR", 1999) is None


def test_t4_unique_top_author_and_ties_are_skipped(corpus):
    cand = aggregate.t4(corpus, "cs.IR")
    assert cand.expected_sql_answer == "Jimmy Lin"  # 4 cs.IR papers; nobody else close
    # cs.DB: Wei Zhang(1) Sam Roe(2) ... Sam Roe has 2 -> unique; build a tie corpus
    tie_rows = [
        ("1", "A", "cs.IR", ("cs.IR",), "2024-01-01", [("X One", "x one")]),
        ("2", "B", "cs.IR", ("cs.IR",), "2024-01-02", [("Y Two", "y two")]),
    ]
    assert aggregate.t4(FixtureCorpus(tie_rows), "cs.IR") is None


def test_t5_lists_titles_for_authors_with_2_to_4_papers(corpus):
    cand = aggregate.t5(corpus, "Wei Zhang", "wei zhang")
    assert json.loads(cand.expected_sql_answer) == sorted(
        r[1] for r in PAPER_ROWS if any(n == "wei zhang" for _, n in r[5])
    )
    assert aggregate.t5(corpus, "Sam Roe", "sam roe") is not None
    one = [("1", "A", "cs.IR", ("cs.IR",), "2024-01-01", [("X One", "x one")])]
    assert aggregate.t5(FixtureCorpus(one), "X One", "x one") is None  # needs 2-4 papers


def test_generate_aggregates_is_seeded_unique_and_capped(corpus):
    a = aggregate.generate_aggregates(corpus, total=12, seed=42)
    b = aggregate.generate_aggregates(corpus, total=12, seed=42)
    assert [c.as_dict() for c in a] == [c.as_dict() for c in b]
    assert 0 < len(a) <= 12 and len({c.question for c in a}) == len(a)
    assert {c.template for c in a} >= {"T1", "T2", "T3"}
    for c in a:  # every candidate's answer is reproducible from its recorded SQL
        rows = corpus.fetch(c.sql, c.params)
        assert c.expected_sql_answer in {str(rows[0][0]), json.dumps([r[0] for r in rows])}


# --- generation --------------------------------------------------------------------------------


class ScriptedLLM:
    provider, model = "ollama", "llama3.1:8b"

    def __init__(self, reply=None):
        self.calls = []
        self.reply = reply or (
            lambda system, user: {"question": f"What is in {len(self.calls)}?", "answer": "A."}
        )

    def chat(self, system, user, *, num_predict, json_format=True, seed=42):
        self.calls.append(
            {"system": system, "user": user, "num_predict": num_predict, "format": json_format}
        )
        out = self.reply(system, user)
        if isinstance(out, Exception):
            raise out
        return ChatResult(out if isinstance(out, str) else json.dumps(out), 10, 5)


def test_phrase_and_self_containment_checks():
    assert generate.phrase_in_question(
        "by Jimmy Lin", "Which rerankers were proposed by  jimmy lin?"
    )
    assert generate.phrase_in_question("in 2024", "What did they study in 2024 for retrieval?")
    assert not generate.phrase_in_question("by Jimmy Lin", "Which rerankers did Jimmy Lin propose?")
    assert generate.phrase_in_question(
        "cs.IR papers from 2025", "What do cs.IR papers from 2025 evaluate?"
    )
    assert generate.is_self_contained("What does BM25 weight by default?")
    for bad in (
        "What does this paper propose?",
        "What did the authors find?",
        "Summarize the passage.",
    ):
        assert not generate.is_self_contained(bad)


def test_generate_candidates_shape_and_prompts(corpus):
    samples = sample.draw_samples(corpus, "factoid", 3)
    llm = ScriptedLLM()
    out = generate.generate_from_samples(llm, corpus, samples)
    assert [c["candidate_id"] for c in out] == [
        "cand-factoid-0001",
        "cand-factoid-0002",
        "cand-factoid-0003",
    ]
    c = out[0]
    assert c["type"] == "factoid" and c["prompt_version"] == "qgen-factoid-v1"
    assert c["source"]["generator"] == "ollama:llama3.1:8b" and c["source"]["seed"] == 42
    assert c["source"]["sampled_chunks"] == [
        {"external_id": g["external_id"], "ordinal": g["ordinal"]} for g in c["gold_chunks"]
    ]
    assert (
        len(c["gold_chunks"][0]["content_sha256"]) == 64 and c["sample_id"] == samples[0].sample_id
    )
    assert llm.calls[0]["num_predict"] == 512 and llm.calls[0]["format"] is True
    ext = c["gold_chunks"][0]["external_id"]
    assert f"Content of {ext}" in llm.calls[0]["user"]


def test_multi_chunk_prompt_contains_every_passage(corpus):
    (row,) = sample.draw_samples(corpus, "multi_chunk", 1)
    llm = ScriptedLLM()
    generate.generate_from_samples(llm, corpus, [row])
    prompt = llm.calls[0]["user"]
    assert all(f"[Passage {i}]" in prompt for i in range(1, len(row.chunks) + 1))
    assert generate.PROMPTS["multi_chunk"][0] == "qgen-multi-v1"


def test_filter_generation_requires_phrase_and_sets_expected_filters(corpus):
    rows = sample.draw_samples(corpus, "filter", 4)
    good = ScriptedLLM(
        lambda s, u: {"question": f"What was studied {u.split(chr(10))[0][8:]}?", "answer": "A."}
    )
    out = generate.generate_from_samples(good, corpus, rows)
    assert len(out) == 4
    for cand, row in zip(out, rows, strict=True):
        assert cand["filter_phrase"] == row.phrase["text"]
        assert cand["expected_filters"] == row.phrase["expected_filters"]
        assert generate.phrase_in_question(row.phrase["text"], cand["question"])
    stats = generate.GenerationStats()
    bad = ScriptedLLM(lambda s, u: {"question": "What is a reranker?", "answer": "A."})
    assert generate.generate_from_samples(bad, corpus, rows, stats=stats) == []
    assert stats.dropped == {"filter_phrase_missing": 4}


def test_bad_llm_output_is_dropped_and_counted(corpus):
    samples = sample.draw_samples(corpus, "factoid", 5)
    replies = iter(
        [
            "not json",
            {"question": "", "answer": "A"},
            {"question": "What did this paper do?", "answer": "A"},
            LLMError("down"),
            {"question": "What is BM25?", "answer": "A ranking function."},
        ]
    )
    stats = generate.GenerationStats()
    out = generate.generate_from_samples(
        ScriptedLLM(lambda s, u: next(replies)), corpus, samples, stats=stats
    )
    assert [c["question"] for c in out] == ["What is BM25?"]
    assert out[0]["candidate_id"] == "cand-factoid-0001"
    assert stats.dropped == {"llm_error_or_bad_json": 2, "empty_field": 1, "not_self_contained": 1}


def test_unanswerable_generation_uses_topic_and_no_gold(corpus):
    (row,) = sample.draw_samples(corpus, "unanswerable", 1)
    llm = ScriptedLLM(
        lambda s, u: {"question": "What is the latency of ProductX?", "answer": "n/a"}
    )
    (cand,) = generate.generate_from_samples(llm, corpus, [row])
    assert cand["gold_chunks"] == [] and cand["type"] == "unanswerable"
    assert row.topic["angle"] in llm.calls[0]["user"]


def test_generate_type_continues_ids_and_tops_up_when_drops_happen(corpus):
    seen = {"n": 0}

    def reply(system, user):
        seen["n"] += 1
        return (
            "garbage" if seen["n"] % 2 else {"question": f"Q number {seen['n']}?", "answer": "A."}
        )

    existing = [{"candidate_id": "cand-factoid-0007", "type": "factoid", "sample_id": None}]
    out, samples, stats = generate.generate_type(
        ScriptedLLM(reply), corpus, "factoid", 3, existing_candidates=existing, existing_samples=[]
    )
    assert len(out) == 3
    assert [c["candidate_id"] for c in out] == [
        "cand-factoid-0008",
        "cand-factoid-0009",
        "cand-factoid-0010",
    ]
    assert stats.dropped["llm_error_or_bad_json"] >= 3
    used = {c["sample_id"] for c in out}
    assert used <= {s.sample_id for s in samples}
    # a second call never reuses a sample that already produced (or burned) a candidate
    more, _, _ = generate.generate_type(
        ScriptedLLM(),
        corpus,
        "factoid",
        2,
        existing_candidates=existing + out,
        existing_samples=samples,
    )
    assert not used & {c["sample_id"] for c in more}


def test_generate_type_aggregate_needs_no_llm_and_skips_known_questions(corpus):
    out, _, stats = generate.generate_type(
        None, corpus, "aggregate", 5, existing_candidates=[], existing_samples=[]
    )
    assert 0 < len(out) <= 5 and stats.generated == len(out)
    assert all(
        c["expected_route"] == "sql" and c["expected_sql_answer"] and c["gold_chunks"] == []
        for c in out
    )
    assert out[0]["candidate_id"] == "cand-aggregate-0001" and out[0]["sql"]["query"]
    more, _, _ = generate.generate_type(
        None, corpus, "aggregate", 5, existing_candidates=out, existing_samples=[]
    )
    assert not {c["question"] for c in out} & {c["question"] for c in more}
    assert more[0]["candidate_id"] == f"cand-aggregate-{len(out) + 1:04d}"
    with pytest.raises(ValueError):
        generate.generate_type(
            None, corpus, "factoid", 1, existing_candidates=[], existing_samples=[]
        )


# --- precheck ----------------------------------------------------------------------------------


def cand(ctype="factoid", cid="cand-factoid-0001", gold=(("2401.00001", 0),)):
    return {
        "candidate_id": cid, "type": ctype, "question": "What is X?", "reference_answer": "X is Y.",
        "source": {"generator": "ollama:llama3.1:8b", "seed": 42, "sampled_chunks": []},
        "gold_chunks": [{"external_id": e, "ordinal": o, "content_sha256": "0" * 64} for e, o in gold],
    }  # fmt: skip


def test_precheck_answerable_passes_on_yes(corpus):
    llm = ScriptedLLM(lambda s, u: {"verdict": "yes"})
    (entry,) = precheck.run_precheck(llm, corpus, [cand()], None)
    assert (
        entry["passed"] is True
        and entry["verdict"] == "yes"
        and entry["prompt_version"] == "precheck-v1"
    )
    assert (
        "Content of 2401.00001 chunk 0." in llm.calls[0]["user"]
        and "X is Y." in llm.calls[0]["user"]
    )
    assert llm.calls[0]["num_predict"] == 64
    (no,) = precheck.run_precheck(
        ScriptedLLM(lambda s, u: {"verdict": "no"}), corpus, [cand()], None
    )
    assert no["passed"] is False


def test_precheck_unanswerable_passes_only_when_passages_do_not_answer(corpus):
    retr = lambda q: [{"content": "irrelevant passage"}]  # noqa: E731
    c = cand("unanswerable", "cand-unanswerable-0001", gold=())
    (ok,) = precheck.run_precheck(ScriptedLLM(lambda s, u: {"verdict": "no"}), corpus, [c], retr)
    (bad,) = precheck.run_precheck(ScriptedLLM(lambda s, u: {"verdict": "yes"}), corpus, [c], retr)
    assert ok["passed"] is True and bad["passed"] is False
    with pytest.raises(ValueError):
        precheck.run_precheck(ScriptedLLM(), corpus, [c], None)


def test_precheck_aggregate_is_sql_computed_without_llm_and_done_is_skipped(corpus):
    llm = ScriptedLLM()
    agg = {**cand("aggregate", "cand-aggregate-0001", gold=())}
    (entry,) = precheck.run_precheck(llm, corpus, [agg], None)
    assert entry["passed"] and entry["method"] == "sql" and llm.calls == []
    assert precheck.run_precheck(llm, corpus, [agg], None, done={"cand-aggregate-0001"}) == []


@pytest.mark.parametrize("reply", ["junk", {"verdict": "maybe"}, LLMError("x")])
def test_precheck_errors_fail_closed(corpus, reply):
    (entry,) = precheck.run_precheck(ScriptedLLM(lambda s, u: reply), corpus, [cand()], None)
    assert entry["passed"] is False and entry["error"]


# --- filter checks -------------------------------------------------------------------------------


def test_filter_comparison_normalises_order_and_nulls():
    exp = {"authors": ["b", "a"], "published_from": "2024-01-01", "published_to": "2024-12-31"}
    got = {
        "authors": ["a", "b"],
        "published_from": "2024-01-01",
        "published_to": "2024-12-31",
        "categories": None,
    }
    assert check_filters.compare_filters(exp, got)["match"] is True
    diff = check_filters.compare_filters(exp, {"authors": ["a"], "published_from": "2024-01-01"})
    assert diff["match"] is False and diff["differences"] == ["authors", "published_to"]
    assert (
        check_filters.compare_filters(
            {"authors": ["a"]}, {"authors": ["a"], "categories": ["cs.IR"]}
        )["match"]
        is False
    )


def test_check_filters_runs_only_filter_candidates():
    cands = [
        {
            **cand("filter", "cand-filter-0001"),
            "expected_filters": {"published_from": "2024-01-01"},
        },
        cand(),
    ]
    out = check_filters.run_check_filters(cands, lambda q: {"published_from": "2024-01-01"})
    assert [r["candidate_id"] for r in out] == ["cand-filter-0001"] and out[0]["match"] is True


# --- review / verdict ----------------------------------------------------------------------------


@pytest.fixture
def ws(tmp_path):
    paths = {
        "candidates_path": tmp_path / "candidates.jsonl",
        "precheck_path": tmp_path / "precheck.jsonl",
        "filter_checks_path": tmp_path / "filter_checks.jsonl",
        "verification_path": tmp_path / "verification.jsonl",
    }
    cands = [
        cand(),
        cand(cid="cand-factoid-0002"),
        {
            **cand("filter", "cand-filter-0001"),
            "expected_filters": {"published_from": "2024-01-01"},
        },
    ]
    work.write_jsonl(paths["candidates_path"], cands)
    work.write_jsonl(
        paths["precheck_path"],
        [
            {
                "candidate_id": "cand-factoid-0001",
                "passed": True,
                "verdict": "yes",
                "method": "llm",
            },
            {
                "candidate_id": "cand-factoid-0002",
                "passed": False,
                "verdict": "no",
                "method": "llm",
            },
            {"candidate_id": "cand-filter-0001", "passed": True, "verdict": "yes", "method": "llm"},
        ],
    )
    return paths


NOW = lambda: datetime(2026, 10, 10, tzinfo=UTC)  # noqa: E731


def verdict(ws, **kw):
    args = {
        "candidate_id": "cand-factoid-0001",
        "status": "accept",
        "by": "agent:claude-test",
        "notes": "",
        "now": NOW,
    }
    return review.record_verdict(**{**args, **kw}, **ws)


def test_verdict_appends_with_verifier_identity(ws):
    entry = verdict(ws, notes="supported by the gold chunk")
    (row,) = work.read_jsonl(ws["verification_path"])
    assert row == entry
    assert (
        row["by"] == "agent:claude-test"
        and row["status"] == "accept"
        and row["at"].startswith("2026-10-10")
    )
    verdict(ws, candidate_id="cand-factoid-0002", status="reject", notes="not answerable")
    assert [r["status"] for r in work.read_jsonl(ws["verification_path"])] == [
        "accept",
        "reject",
    ]  # append-only


def test_edit_keeps_original_text_in_notes(ws):
    entry = verdict(ws, status="edit", question="What is X exactly?", notes="made self-contained")
    assert entry["question"] == "What is X exactly?"
    assert (
        "original question: What is X?" in entry["notes"]
        and "original answer: X is Y." in entry["notes"]
    )


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"status": "maybe"}, "status must be"),
        ({"by": " "}, "verifier identity"),
        ({"status": "reject", "notes": ""}, "needs --notes"),
        ({"candidate_id": "nope"}, "unknown candidate"),
        ({"candidate_id": "cand-factoid-0002"}, "failed the automated precheck"),
        ({"status": "edit", "notes": "x"}, "edit needs"),
        ({"question": "new?"}, "only valid with --status edit"),
    ],
)
def test_invalid_verdicts_are_refused_and_write_nothing(ws, kwargs, message):
    with pytest.raises(review.VerdictError, match=message):
        verdict(ws, **kwargs)
    assert work.read_jsonl(ws["verification_path"]) == []


def test_unprechecked_candidate_cannot_be_verified(ws):
    work.append_jsonl(ws["candidates_path"], cand(cid="cand-factoid-0009"))
    with pytest.raises(review.VerdictError, match="no precheck"):
        verdict(ws, candidate_id="cand-factoid-0009")


def test_filter_candidate_needs_a_matching_check_before_accept(ws):
    with pytest.raises(review.VerdictError, match="check-filters"):
        verdict(ws, candidate_id="cand-filter-0001")
    work.append_jsonl(ws["filter_checks_path"], {"candidate_id": "cand-filter-0001", "question": "What is X?",
                                                 "match": False, "differences": ["authors"]})  # fmt: skip
    with pytest.raises(review.VerdictError, match="different filters"):
        verdict(ws, candidate_id="cand-filter-0001")
    work.append_jsonl(ws["filter_checks_path"], {"candidate_id": "cand-filter-0001", "question": "What is X?",
                                                 "match": True, "differences": []})  # fmt: skip
    assert verdict(ws, candidate_id="cand-filter-0001")["status"] == "accept"


def test_editing_a_filter_question_rechecks_it_against_the_service(ws):
    mismatch = lambda q: {"authors": ["someone"]}  # noqa: E731
    with pytest.raises(review.VerdictError, match="different filters"):
        verdict(
            ws,
            candidate_id="cand-filter-0001",
            status="edit",
            question="Q in 2024?",
            notes="n",
            extractor=mismatch,
        )
    with pytest.raises(review.VerdictError, match="live service"):
        verdict(
            ws, candidate_id="cand-filter-0001", status="edit", question="Q in 2024?", notes="n"
        )
    match = lambda q: {"published_from": "2024-01-01"}  # noqa: E731
    entry = verdict(
        ws,
        candidate_id="cand-filter-0001",
        status="edit",
        question="Q in 2024?",
        notes="n",
        extractor=match,
    )
    assert entry["question"] == "Q in 2024?"
    assert (
        work.read_jsonl(ws["filter_checks_path"])[-1]["question"] == "Q in 2024?"
    )  # check recorded


def test_reject_never_needs_filter_checks(ws):
    assert (
        verdict(ws, candidate_id="cand-filter-0001", status="reject", notes="bad phrase")["status"]
        == "reject"
    )


def test_pending_candidates_are_prechecked_and_unverified(ws):
    cands = work.read_jsonl(ws["candidates_path"])
    pre = work.read_jsonl(ws["precheck_path"])
    assert [c["candidate_id"] for c in review.pending_candidates(cands, pre, [])] == [
        "cand-factoid-0001", "cand-factoid-0002", "cand-filter-0001",
    ]  # fmt: skip
    verdict(ws)
    done = work.read_jsonl(ws["verification_path"])
    assert "cand-factoid-0001" not in [
        c["candidate_id"] for c in review.pending_candidates(cands, pre, done)
    ]
    assert review.pending_candidates(cands, [], []) == []  # nothing without a precheck entry


def test_render_review_shows_gold_text_filters_and_service_chunks(corpus):
    text = review.render_review(
        cand(), {"verdict": "yes", "passed": True, "method": "llm"}, None, corpus
    )
    assert (
        "QUESTION: What is X?" in text
        and "Content of 2401.00001 chunk 0." in text
        and "PRECHECK" in text
    )
    f = {
        **cand("filter", "cand-filter-0001"),
        "expected_filters": {"authors": ["a"]},
        "filter_phrase": "by A",
    }
    shown = review.render_review(
        f, None, {"match": False, "differences": ["authors"], "extracted": {}}, corpus
    )
    assert "MISMATCH on ['authors']" in shown and "by A" in shown
    u = cand("unanswerable", "cand-unanswerable-0001", gold=())
    shown = review.render_review(
        u,
        None,
        None,
        corpus,
        lambda q: [{"external_id": "z", "ordinal": 1, "content": "top1 text"}],
    )
    assert "top1 text" in shown
