#!/usr/bin/env bash
# Download + verify the CI index snapshot, then run the replay eval gate (`make eval-ci`).
#
# Inputs (environment):
#   LOCK           path of the snapshot lock file         (default evals/ci/snapshot.lock.json)
#   SKIP_DOWNLOAD  1 = use an existing local snapshot instead of `gh release download`
#   SNAPSHOT       path of the snapshot file              (default data/snapshots/<asset_name>)
#   GH_TOKEN       token for `gh release download`        (set by the workflow)
# Exit: 0 pass; 1 threshold breach or lock/snapshot problem; 2 cassette miss; 3 setup error (from make eval-ci).
set -euo pipefail

LOCK="${LOCK:-evals/ci/snapshot.lock.json}"
DEST_DIR="data/snapshots"

publish_hint() {
  cat >&2 <<HINT
The eval gate needs the index snapshot described by ${LOCK} and published as a GitHub Release asset.
Publish it from a machine that holds the indexed corpus:
  make snapshot                      # writes data/snapshots/rag-index-<12hex>.dump and prints its sha256
  cd evals && uv run evals ci-lock   # writes ${LOCK} (release tag, asset name, sha256)
  gh release create index-snapshot-<12hex> data/snapshots/rag-index-<12hex>.dump \\
      --notes "DB snapshot for CI eval gate"
then commit ${LOCK}. The job fails (instead of skipping) so a missing gate never looks like a pass.
HINT
}

if [ ! -f "$LOCK" ]; then
  echo "error: snapshot lock file ${LOCK} not found" >&2
  publish_hint
  exit 1
fi

TAG="$(jq -er '.release_tag' "$LOCK")"
ASSET="$(jq -er '.asset_name' "$LOCK")"
SHA="$(jq -er '.sha256' "$LOCK")"
SNAPSHOT="${SNAPSHOT:-$DEST_DIR/$ASSET}"

if [ "${SKIP_DOWNLOAD:-0}" != "1" ]; then
  mkdir -p "$DEST_DIR"
  echo "Downloading ${ASSET} from release ${TAG}"
  if ! gh release download "$TAG" -p "$ASSET" -D "$DEST_DIR" --clobber; then
    echo "error: could not download ${ASSET} from release ${TAG}" >&2
    publish_hint
    exit 1
  fi
fi

if [ ! -f "$SNAPSHOT" ]; then
  echo "error: snapshot file ${SNAPSHOT} does not exist" >&2
  exit 1
fi

if command -v sha256sum >/dev/null 2>&1; then
  ACTUAL="$(sha256sum "$SNAPSHOT" | cut -d' ' -f1)"
else
  ACTUAL="$(shasum -a 256 "$SNAPSHOT" | cut -d' ' -f1)"
fi
if [ "$ACTUAL" != "$SHA" ]; then
  echo "error: sha256 mismatch for ${SNAPSHOT}" >&2
  echo "  lock file: ${SHA}" >&2
  echo "  actual:    ${ACTUAL}" >&2
  echo "Refusing to restore a corrupted or stale snapshot." >&2
  exit 1
fi
echo "sha256 verified: ${ACTUAL}"

# `make` exits 2 for any failed recipe and hides the recipe's own exit status, so the gate's
# code (0 pass, 1 threshold breach, 2 degraded/cassette miss, 3 setup error) is read from the
# "gate exit code: N" line that `evals grid --gate` prints. No such line means the gate never
# ran (migration, restore or stack startup failed), which is a setup error.
COMPOSE=(docker compose --env-file .env -f infra/docker-compose.yml)
# Apply the migrations synchronously: `up --wait flyway` can return before the one-shot
# migration finishes on a fresh database, and the restore then fails with "relation does not exist".
"${COMPOSE[@]}" up -d --wait postgres
"${COMPOSE[@]}" run --rm flyway

LOG="$(mktemp)"
trap 'rm -f "$LOG"' EXIT
rc=0
make eval-ci SNAPSHOT="$SNAPSHOT" 2>&1 | tee "$LOG" || rc=${PIPESTATUS[0]}
if [ "$rc" -ne 0 ]; then
  gate_rc="$(sed -n 's/^gate exit code: \([0-9]\).*/\1/p' "$LOG" | tail -n1)"
  rc="${gate_rc:-3}"
fi

case "$rc" in
  0) echo "eval gate passed" ;;
  1) echo "::error::eval gate failed: a metric fell below evals/thresholds/ci.yaml (see the metric table above)" ;;
  2)
    echo "::error::eval gate failed: a degraded response, almost always a cassette miss" >&2
    echo "The service sent a model request that has no recording. If that change is intended, re-record locally:" >&2
    echo "  make eval-record-ci   # needs Ollama and the stack; then commit evals/cassettes/ci/ and evals/thresholds/ci.yaml" >&2
    ;;
  3) echo "::error::eval gate setup error (exit 3): snapshot restore or stack startup failed" ;;
esac
exit "$rc"
