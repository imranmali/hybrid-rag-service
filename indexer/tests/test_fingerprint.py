from indexer.fingerprint import content_hash, pipeline_fingerprint


def test_fingerprint_is_stable_and_changes_with_inputs() -> None:
    first = pipeline_fingerprint(
        embedding_model="bge-m3",
        embedding_model_digest="embed",
        llm_model="llama3.1:8b",
        llm_model_digest="llm",
    )
    assert first == pipeline_fingerprint(
        embedding_model="bge-m3",
        embedding_model_digest="embed",
        llm_model="llama3.1:8b",
        llm_model_digest="llm",
    )
    assert first != pipeline_fingerprint(
        embedding_model="bge-m3",
        embedding_model_digest="different",
        llm_model="llama3.1:8b",
        llm_model_digest="llm",
    )
    assert content_hash(arxiv_version=1, title="a", abstract="b", raw_text="c") != content_hash(
        arxiv_version=2, title="a", abstract="b", raw_text="c"
    )
