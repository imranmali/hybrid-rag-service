#!/usr/bin/env bash
# README structure check (design D4).
# - Fails (exit 1) only when the first sentence/section order is wrong or a forbidden section exists.
# - Emits a ::warning annotation per unfilled {{RESULT: placeholder in README.md and docs/adr/*.md; never fails on them.
set -euo pipefail

README="${README:-README.md}"
ADR_GLOB="${ADR_GLOB:-docs/adr/*.md}"
fail=0

if [ ! -f "$README" ]; then
  echo "::error::$README not found"
  exit 1
fi

# BUILD_SPEC "README — the first 60 seconds": the seven above-the-fold sections, in order.
required=("## Results" "## Architecture" "## Findings" "## Run it" "## Latency budget" "## Decisions")

# 1. First sentence: the first non-blank line after the H1 title must be prose, and the very next
#    non-blank line must be the Results heading (nothing between the sentence and the table).
first_after_title="$(awk 'NR==1 && !/^# /{exit} /^# /{t=1; next} t && NF {print NR": "$0; exit}' "$README")"
if [ -z "$first_after_title" ]; then
  echo "::error file=$README,line=1::README must start with an H1 title followed by one sentence"
  fail=1
elif [[ "${first_after_title#*: }" == \#* ]]; then
  echo "::error file=$README::README must have its one-sentence description directly under the title"
  fail=1
else
  sent_line="${first_after_title%%:*}"
  next_nonblank="$(awk -v s="$sent_line" 'NR>s && NF {print $0; exit}' "$README")"
  if [ "$next_nonblank" != "## Results" ]; then
    echo "::error file=$README,line=$sent_line::the Results section must follow the first sentence immediately (found: ${next_nonblank:-end of file})"
    fail=1
  fi
fi

# 2. Heading order: each required heading exists and appears after the previous one.
prev=0
for h in "${required[@]}"; do
  line="$(grep -n -x -F "$h" "$README" | head -n1 | cut -d: -f1 || true)"
  if [ -z "$line" ]; then
    echo "::error file=$README::missing section '$h'"
    fail=1
  elif [ "$line" -le "$prev" ]; then
    echo "::error file=$README,line=$line::section '$h' is out of order (BUILD_SPEC order: ${required[*]})"
    fail=1
  else
    prev="$line"
  fi
done

# 3. Things BUILD_SPEC says to leave out.
if grep -n -i -E '^#{1,6} +(what is rag|roadmap)' "$README"; then
  echo "::error file=$README::README must not contain a 'What is RAG' or roadmap section"
  fail=1
fi

# 4. Unfilled placeholders: annotate, never fail.
count=0
# shellcheck disable=SC2086
for f in "$README" $ADR_GLOB; do
  [ -f "$f" ] || continue
  while IFS=: read -r ln _; do
    echo "::warning file=$f,line=$ln::unfilled placeholder {{RESULT:"
    count=$((count + 1))
  done < <(grep -n -o -F '{{RESULT:' "$f" || true)
done
echo "check_readme: ${count} unfilled placeholder(s); section order $([ "$fail" -eq 0 ] && echo ok || echo VIOLATED)"
exit "$fail"
