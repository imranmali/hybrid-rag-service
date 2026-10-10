#!/usr/bin/env bash
# Asserts the least-privilege role model and the contextual-variant schema behaviour against the
# running stack (postgres + flyway already up). Usage: bash infra/scripts/verify_roles.sh
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi
: "${POSTGRES_DB:=rag}" "${POSTGRES_USER:=rag_owner}" "${POSTGRES_PASSWORD:=rag_owner_dev}"
: "${RAG_INDEXER_PASSWORD:=rag_indexer_dev}" "${RAG_READER_PASSWORD:=rag_reader_dev}"
: "${RAG_ROUTER_PASSWORD:=rag_router_dev}"

COMPOSE=(docker compose --env-file .env -f infra/docker-compose.yml)
DOC_ID='00000000-0000-0000-0000-00000000f001'
FAILED=0
LAST_OUT=""

# pg <user> <password> <sql>: runs one statement over TCP (-h postgres, so password auth is
# exercised rather than the container's loopback trust rule). Output goes to LAST_OUT.
pg() {
  LAST_OUT="$("${COMPOSE[@]}" exec -T -e PGPASSWORD="$2" postgres \
    psql -h postgres -U "$1" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -tA -c "$3" 2>&1)"
}

pass() { echo "PASS  $1"; }
fail() { echo "FAIL  $1"; [[ -n "${2:-}" ]] && echo "      got: $2"; FAILED=1; }

expect_ok() { # <label> <user> <password> <sql>
  if pg "$2" "$3" "$4"; then pass "$1"; else fail "$1" "$LAST_OUT"; fi
}
expect_error() { # <label> <user> <password> <sql> <error pattern>
  if pg "$2" "$3" "$4"; then
    fail "$1 (statement unexpectedly succeeded)" "$LAST_OUT"
  elif grep -qiE "$5" <<<"$LAST_OUT"; then
    pass "$1"
  else
    fail "$1 (failed with an unexpected error)" "$LAST_OUT"
  fi
}
expect_value() { # <label> <expected> <user> <password> <sql>
  if pg "$3" "$4" "$5" && [[ "$LAST_OUT" == "$2" ]]; then pass "$1"; else fail "$1" "$LAST_OUT"; fi
}

cleanup() {
  pg "$POSTGRES_USER" "$POSTGRES_PASSWORD" "delete from documents where id = '$DOC_ID'" || true
  pg "$POSTGRES_USER" "$POSTGRES_PASSWORD" "delete from index_metadata where key = 'verify_roles_probe'" || true
}
trap cleanup EXIT

if ! pg "$POSTGRES_USER" "$POSTGRES_PASSWORD" "select 1"; then
  echo "FAIL  cannot reach postgres as $POSTGRES_USER (is the stack up? run: make up)"
  echo "      got: $LAST_OUT"
  exit 1
fi
cleanup

# --- every login role can authenticate ---
expect_value "rag_indexer can log in" rag_indexer rag_indexer "$RAG_INDEXER_PASSWORD" "select current_user"
expect_value "rag_reader can log in" rag_reader rag_reader "$RAG_READER_PASSWORD" "select current_user"
expect_value "rag_router can log in" rag_router rag_router "$RAG_ROUTER_PASSWORD" "select current_user"

# --- rag_reader: read-only ---
expect_error "rag_reader cannot insert into index_metadata" rag_reader "$RAG_READER_PASSWORD" \
  "insert into index_metadata(key, value) values ('verify_roles_probe', 'x')" \
  "read-only transaction|permission denied"
expect_ok "rag_reader can read chunks" rag_reader "$RAG_READER_PASSWORD" "select count(*) from chunks"

# --- rag_router: structured tables only ---
expect_error "rag_router cannot read chunks.content" rag_router "$RAG_ROUTER_PASSWORD" \
  "select content from chunks limit 1" "permission denied"
expect_error "rag_router cannot read documents.raw_text" rag_router "$RAG_ROUTER_PASSWORD" \
  "select raw_text from documents limit 1" "permission denied"
expect_ok "rag_router can read documents.title" rag_router "$RAG_ROUTER_PASSWORD" \
  "select title from documents limit 1"
expect_ok "rag_router can read paper_categories" rag_router "$RAG_ROUTER_PASSWORD" \
  "select count(*) from paper_categories"
expect_error "rag_router statement times out after 2 s" rag_router "$RAG_ROUTER_PASSWORD" \
  "select pg_sleep(3)" "statement timeout"
expect_error "rag_router cannot write" rag_router "$RAG_ROUTER_PASSWORD" \
  "delete from authors" "read-only transaction|permission denied"

# --- rag_indexer: read-write ---
expect_ok "rag_indexer can insert into index_metadata" rag_indexer "$RAG_INDEXER_PASSWORD" \
  "insert into index_metadata(key, value) values ('verify_roles_probe', 'x')"
expect_ok "rag_indexer can delete from index_metadata" rag_indexer "$RAG_INDEXER_PASSWORD" \
  "delete from index_metadata where key = 'verify_roles_probe'"

# --- schema behaviour: contextual variant, unique chunk identity, cascade ---
VEC="array_fill(0.01::real, array[1024])::vector"
SETUP_DOC="insert into documents(id, external_id, arxiv_version, title, abstract, primary_category,
  published_on, updated_on, source_url, raw_text, context_summary, content_hash, pipeline_fingerprint)
  values ('$DOC_ID', 'verify.roles', 1, 't', 'a', 'cs.IR', '2024-01-01', '2024-01-01', 'u', 'r', 'c', 'h', 'p')"
SETUP_CHUNK="insert into chunks(document_id, ordinal, section_path, content, context_header, token_count,
  content_sha256, embedding, embedding_ctx)
  values ('$DOC_ID', 0, '1 Intro', 'plain body text about retrieval', 'Paper: t. ctxonlymarker', 5, 'abc', $VEC, $VEC)"
expect_ok "owner inserts a test document and chunk" "$POSTGRES_USER" "$POSTGRES_PASSWORD" \
  "$SETUP_DOC; $SETUP_CHUNK; insert into paper_categories values ('$DOC_ID', 'cs.IR', true)"
expect_value "header-only word matches tsv_ctx" "t" "$POSTGRES_USER" "$POSTGRES_PASSWORD" \
  "select tsv_ctx @@ plainto_tsquery('english', 'ctxonlymarker') from chunks where document_id = '$DOC_ID'"
expect_value "header-only word does not match tsv" "f" "$POSTGRES_USER" "$POSTGRES_PASSWORD" \
  "select tsv @@ plainto_tsquery('english', 'ctxonlymarker') from chunks where document_id = '$DOC_ID'"
expect_error "duplicate (document_id, ordinal) is rejected" "$POSTGRES_USER" "$POSTGRES_PASSWORD" \
  "$SETUP_CHUNK" "duplicate key|unique"
expect_ok "owner deletes the test document" "$POSTGRES_USER" "$POSTGRES_PASSWORD" \
  "delete from documents where id = '$DOC_ID'"
expect_value "delete cascades to chunks and paper_categories" "0|0" "$POSTGRES_USER" "$POSTGRES_PASSWORD" \
  "select (select count(*) from chunks where document_id = '$DOC_ID') || '|' ||
   (select count(*) from paper_categories where document_id = '$DOC_ID')"

if [[ $FAILED -eq 0 ]]; then
  echo "ALL ROLE CHECKS PASSED"
else
  echo "ROLE CHECKS FAILED"
  exit 1
fi
