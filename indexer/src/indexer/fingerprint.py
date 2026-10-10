"""Stable content and pipeline identity hashes."""

import hashlib
import json


def content_hash(*, arxiv_version: int, title: str, abstract: str, raw_text: str) -> str:
    payload = f"{arxiv_version}\n{title}\n{abstract}\n{raw_text}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def pipeline_fingerprint(
    *,
    embedding_model: str,
    embedding_model_digest: str,
    llm_model: str,
    llm_model_digest: str,
    chunk_size_tokens: int = 384,
    chunk_overlap_tokens: int = 64,
    min_chunk_tokens: int = 64,
    tokenizer: str = "BAAI/bge-m3",
    header_prompt_version: str = "ctx-v1",
    parser_version: str = "html-v1",
) -> str:
    """Hash exactly the D8 indexing inputs, sorted to remain stable across runs."""
    payload = {
        "chunk_size_tokens": chunk_size_tokens,
        "chunk_overlap_tokens": chunk_overlap_tokens,
        "min_chunk_tokens": min_chunk_tokens,
        "tokenizer": tokenizer,
        "header_prompt_version": header_prompt_version,
        "parser_version": parser_version,
        "embedding_model": embedding_model,
        "embedding_model_digest": embedding_model_digest,
        "llm_model": llm_model,
        "llm_model_digest": llm_model_digest,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
