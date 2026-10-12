from __future__ import annotations

import json

import httpx
import pytest
import respx

from evals.metrics import judge
from evals.ollama import AnthropicLLM, LLMError, OllamaLLM, fetch_digests

BASE = "http://ollama.test"


def _chat_reply(content, prompt=100, completion=5):
    return httpx.Response(
        200,
        json={
            "message": {"role": "assistant", "content": content},
            "prompt_eval_count": prompt,
            "eval_count": completion,
            "done": True,
        },
    )


@pytest.fixture
def llm():
    return OllamaLLM("llama3.1:8b", BASE)


@respx.mock
def test_chat_request_body_follows_contract_c3(llm):
    route = respx.post(f"{BASE}/api/chat").mock(
        return_value=_chat_reply('{"verdict": "supported"}')
    )
    v = judge.judge_faithfulness(llm, "Q?", "ctx", "ans")
    body = json.loads(route.calls.last.request.content)
    assert body["model"] == "llama3.1:8b"
    assert body["stream"] is False
    assert body["format"] == "json"
    assert body["options"]["temperature"] == 0
    assert body["options"]["seed"] == 42
    assert body["options"]["num_predict"] == 128
    assert body["options"]["num_ctx"] == 8192
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    assert "ctx" in body["messages"][1]["content"] and "ans" in body["messages"][1]["content"]
    assert (v.label, v.score, v.error) == ("supported", 1.0, None)
    assert (v.prompt_tokens, v.completion_tokens) == (100, 5)


@respx.mock
def test_free_text_chat_omits_format(llm):
    route = respx.post(f"{BASE}/api/chat").mock(return_value=_chat_reply("hello"))
    llm.chat("s", "u", num_predict=384, json_format=False)
    body = json.loads(route.calls.last.request.content)
    assert "format" not in body and body["options"]["num_predict"] == 384


@respx.mock
@pytest.mark.parametrize(
    ("verdict", "score"),
    [("supported", 1.0), ("partially_supported", 0.5), ("unsupported", 0.0), ("SUPPORTED", 1.0)],
)
def test_faithfulness_mapping(llm, verdict, score):
    respx.post(f"{BASE}/api/chat").mock(return_value=_chat_reply(json.dumps({"verdict": verdict})))
    assert judge.judge_faithfulness(llm, "q", "c", "a").score == score


@respx.mock
@pytest.mark.parametrize(
    ("verdict", "score"), [("relevant", 1.0), ("partially_relevant", 0.5), ("irrelevant", 0.0)]
)
def test_relevance_mapping(llm, verdict, score):
    route = respx.post(f"{BASE}/api/chat").mock(
        return_value=_chat_reply(json.dumps({"verdict": verdict}))
    )
    assert judge.judge_relevance(llm, "q", "a").score == score
    assert "CONTEXT" not in json.loads(route.calls.last.request.content)["messages"][1]["content"]


@respx.mock
@pytest.mark.parametrize("content", ["not json at all", '["a list"]', '{"verdict": "maybe"}', "{}"])
def test_malformed_judge_reply_is_recorded_as_judge_error(llm, content):
    respx.post(f"{BASE}/api/chat").mock(return_value=_chat_reply(content))
    v = judge.judge_faithfulness(llm, "q", "c", "a")
    assert v.score is None and v.label is None
    assert v.error is not None and v.error.startswith("judge_error")


@respx.mock
def test_unreachable_judge_is_a_judge_error_not_a_crash(llm):
    respx.post(f"{BASE}/api/chat").mock(side_effect=httpx.ConnectError("down"))
    v = judge.judge_relevance(llm, "q", "a")
    assert v.error and v.error.startswith("judge_error")


@respx.mock
def test_server_error_raises_llm_error_from_chat(llm):
    respx.post(f"{BASE}/api/chat").mock(return_value=httpx.Response(500))
    with pytest.raises(LLMError):
        llm.chat("s", "u", num_predict=10)


def _resp(answer, route="vector", sql=None, chunks=None):
    return {"route": route, "sql": sql, "chunks": chunks or [], "answer": answer}


@respx.mock
def test_score_answer_judges_ok_answers_with_exact_context_k_passages(llm):
    route = respx.post(f"{BASE}/api/chat").mock(
        side_effect=[
            _chat_reply('{"verdict": "partially_supported"}'),
            _chat_reply('{"verdict": "relevant"}'),
        ]
    )
    chunks = [{"content": f"passage-{i}"} for i in range(1, 5)]
    out = judge.score_answer(
        llm,
        qtype="factoid",
        question="Q?",
        response=_resp({"status": "ok", "abstained": False, "text": "the answer"}, chunks=chunks),
        context_k=2,
    )
    assert out["faithfulness"]["score"] == 0.5
    assert out["relevance"]["score"] == 1.0
    faith_prompt = json.loads(route.calls[0].request.content)["messages"][1]["content"]
    assert "passage-1" in faith_prompt and "passage-2" in faith_prompt
    assert "passage-3" not in faith_prompt  # only the context_k passages the generator saw


@respx.mock
def test_score_answer_uses_sql_rows_as_context_on_the_sql_route(llm):
    route = respx.post(f"{BASE}/api/chat").mock(
        return_value=_chat_reply('{"verdict": "supported"}')
    )
    sql = {"query": "select 1", "columns": ["n"], "rows": [[42]], "error": None}
    judge.score_answer(
        llm,
        qtype="aggregate",
        question="How many?",
        response=_resp({"status": "ok", "abstained": False, "text": "42"}, "sql", sql),
        context_k=6,
    )
    prompt = json.loads(route.calls[0].request.content)["messages"][1]["content"]
    assert "select 1" in prompt and "42" in prompt


@respx.mock
def test_abstentions_are_scored_without_llm_calls(llm):
    route = respx.post(f"{BASE}/api/chat").mock(return_value=_chat_reply("{}"))
    abst = {
        "status": "ok",
        "abstained": True,
        "text": "I cannot answer this from the indexed papers.",
    }
    answerable = judge.score_answer(
        llm, qtype="factoid", question="q", response=_resp(abst), context_k=6
    )
    unanswerable = judge.score_answer(
        llm, qtype="unanswerable", question="q", response=_resp(abst), context_k=6
    )
    assert route.call_count == 0
    assert (answerable["faithfulness"]["score"], answerable["relevance"]["score"]) == (1.0, 0.0)
    assert unanswerable["faithfulness"]["score"] == 1.0 and unanswerable["relevance"] is None


@respx.mock
def test_non_ok_answers_are_not_judged(llm):
    route = respx.post(f"{BASE}/api/chat").mock(return_value=_chat_reply("{}"))
    out = judge.score_answer(
        llm, qtype="factoid", question="q", response=_resp({"status": "unavailable"}), context_k=6
    )
    assert route.call_count == 0
    assert out == {"faithfulness": None, "relevance": None, "abstained": None}


def test_aggregate_judgements_excludes_errors_and_counts_them():
    ok = {"label": "supported", "score": 1.0, "error": None}
    half = {"label": "partially_supported", "score": 0.5, "error": None}
    err = {"label": None, "score": None, "error": "judge_error: malformed"}
    rel = {"label": "relevant", "score": 1.0, "error": None}
    agg = judge.aggregate_judgements(
        [
            ("factoid", {"faithfulness": ok, "relevance": rel}),
            ("filter", {"faithfulness": half, "relevance": err}),
            ("multi_chunk", {"faithfulness": err, "relevance": rel}),
            ("unanswerable", {"faithfulness": ok, "relevance": None}),
        ]
    )
    assert agg["faithfulness"] == {"mean": pytest.approx(2.5 / 3), "n": 3, "judge_errors": 1}
    assert agg["answer_relevance"] == {"mean": 1.0, "n": 2, "judge_errors": 1}


def test_abstention_metrics():
    def r(abstained, status="ok"):
        return {"answer": {"status": status, "abstained": abstained}}

    m = judge.abstention_metrics(
        [
            ("unanswerable", r(True)),
            ("unanswerable", r(True)),
            ("unanswerable", r(False)),
            ("unanswerable", r(True)),
            ("factoid", r(False)),
            ("factoid", r(True)),
            ("filter", r(False, "unavailable")),  # not counted: no answer was produced
        ]
    )
    assert m["abstention_accuracy"] == {"value": 0.75, "n": 4}
    assert m["false_abstention_rate"] == {"value": 0.5, "n": 2}
    empty = judge.abstention_metrics([])
    assert empty["abstention_accuracy"]["value"] is None


# --- model inventory + anthropic ---------------------------------------------------------------


@respx.mock
def test_fetch_digests_adds_latest_alias():
    respx.get(f"{BASE}/api/tags").mock(
        return_value=httpx.Response(
            200,
            json={
                "models": [
                    {"name": "bge-m3:latest", "digest": "a" * 64},
                    {"name": "llama3.1:8b", "digest": "b" * 64},
                ]
            },
        )
    )
    d = fetch_digests(BASE)
    assert d["bge-m3"] == d["bge-m3:latest"] == "a" * 64
    assert d["llama3.1:8b"] == "b" * 64


@respx.mock
def test_fetch_digests_failure_raises():
    respx.get(f"{BASE}/api/tags").mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(LLMError):
        fetch_digests(BASE)


@respx.mock
def test_anthropic_provider_request_shape():
    route = respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": '{"verdict": "relevant"}'}],
                "usage": {"input_tokens": 11, "output_tokens": 4},
            },
        )
    )
    v = judge.judge_relevance(AnthropicLLM("claude-test", "sk-key"), "q", "a")
    req = route.calls.last.request
    body = json.loads(req.content)
    assert req.headers["x-api-key"] == "sk-key"
    assert body["temperature"] == 0 and body["max_tokens"] == 128 and body["model"] == "claude-test"
    assert (v.score, v.prompt_tokens, v.completion_tokens) == (1.0, 11, 4)


def test_anthropic_requires_key_and_model():
    with pytest.raises(LLMError):
        AnthropicLLM("claude-test", "")
    with pytest.raises(LLMError):
        AnthropicLLM("", "sk")
