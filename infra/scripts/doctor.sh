#!/usr/bin/env bash
# Environment self-check. Prints one PASS/FAIL (or WARN) line per check and exits non-zero if any
# check FAILs. Run through `make doctor`. WARN lines never affect the exit code.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

# Real environment variables win over .env.
if [[ -f .env ]]; then
  while IFS='=' read -r key value; do
    if [[ -z "${!key+x}" ]]; then export "$key=$value"; fi
  done < <(grep -E '^[A-Z][A-Z0-9_]*=' .env)
fi
OLLAMA_HOST_URL="${OLLAMA_HOST_URL:-http://localhost:11434}"
OLLAMA_EMBED_MODEL="${OLLAMA_EMBED_MODEL:-bge-m3}"
OLLAMA_LLM_MODEL="${OLLAMA_LLM_MODEL:-llama3.1:8b}"
MIN_DOCKER_MEM_BYTES=8000000000 # 8 GB

FAILED=0
pass() { echo "PASS  $1"; }
fail() { echo "FAIL  $1"; [[ -n "${2:-}" ]] && echo "      fix: $2"; FAILED=1; }
warn() { echo "WARN  $1"; [[ -n "${2:-}" ]] && echo "      hint: $2"; }

# 1. Docker daemon
if docker info >/dev/null 2>&1; then
  pass "docker daemon is running"
  mem="$(docker info --format '{{.MemTotal}}' 2>/dev/null || echo 0)"
  if [[ "$mem" =~ ^[0-9]+$ && "$mem" -ge "$MIN_DOCKER_MEM_BYTES" ]]; then
    pass "docker has $((mem / 1000000000)) GB of memory (>= 8 GB)"
  else
    warn "docker memory is below 8 GB (${mem} bytes); the reranker and Postgres may be OOM-killed" \
      "Docker Desktop > Settings > Resources > Memory >= 8 GB"
  fi
else
  fail "docker daemon is not reachable" "start Docker Desktop (or the docker engine)"
fi

# 2. .env
if [[ -f .env ]]; then
  pass ".env exists"
else
  fail ".env is missing" "cp .env.example .env"
fi

# 3. Ollama reachable + models pulled
tags="$(curl -fsS --max-time 5 "${OLLAMA_HOST_URL%/}/api/tags" 2>/dev/null)"
if [[ -n "$tags" ]]; then
  pass "ollama is reachable at $OLLAMA_HOST_URL"
  for model in "$OLLAMA_EMBED_MODEL" "$OLLAMA_LLM_MODEL"; do
    wanted="$model"
    [[ "$wanted" == *:* ]] || wanted="$wanted:latest"
    if grep -q "\"name\":\"$wanted\"" <<<"$tags"; then
      pass "ollama model $model is pulled"
    else
      fail "ollama model $model is not pulled" "ollama pull $model  (or: make models)"
    fi
  done
else
  fail "ollama is not reachable at $OLLAMA_HOST_URL" "start Ollama (ollama serve) or fix OLLAMA_HOST_URL"
  for model in "$OLLAMA_EMBED_MODEL" "$OLLAMA_LLM_MODEL"; do
    fail "ollama model $model cannot be checked (ollama unreachable)"
  done
fi

# 4. uv
if uv_version="$(uv --version 2>/dev/null)"; then
  pass "uv is installed ($uv_version)"
else
  fail "uv is not installed" "https://docs.astral.sh/uv/getting-started/installation/"
fi

# 5. JDK >= 21
if java_line="$(java -version 2>&1 | head -n 1)" && [[ "$java_line" == *version* ]]; then
  java_version="$(sed -E 's/.*version "([0-9]+)(\.([0-9]+))?.*/\1 \3/' <<<"$java_line")"
  major="${java_version%% *}"
  [[ "$major" == "1" ]] && major="${java_version##* }" # 1.8 style
  if [[ "$major" =~ ^[0-9]+$ && "$major" -ge 21 ]]; then
    pass "JDK $major found (>= 21)"
  else
    fail "JDK is older than 21 ($java_line)" "install a JDK 21 or later"
  fi
else
  fail "java is not installed" "install a JDK 21 or later"
fi

if [[ $FAILED -eq 0 ]]; then
  echo "doctor: all checks passed"
else
  echo "doctor: some checks failed"
  exit 1
fi
