#!/usr/bin/env bash
# Publishes a condensed failure report as a check run on the commit, so build
# failures can be inspected through the GitHub API without downloading logs.
set -u
mkdir -p build-logs
report=build-logs/summary.txt
{
  for f in build-logs/*.log; do
    [ -f "$f" ] || continue
    echo "===== $f ====="
    grep -nE '^e: |^w: |error:|Error:|FAILED|What went wrong|^> |\[ksp\]|AAPT|Lint found|Unresolved|Caused by|Could not' "$f" | head -n 120
    echo "--- tail ---"
    tail -n 40 "$f"
  done
  if [ -f app/build/reports/lint-results.txt ]; then
    echo "===== lint errors ====="
    grep -B1 -A7 -E ': (Error|Fatal):' app/build/reports/lint-results.txt | head -n 250
    tail -n 3 app/build/reports/lint-results.txt
  fi
  echo "===== failing tests ====="
  find . -path '*test-results*' -name '*.xml' 2>/dev/null | while read -r xml; do
    if grep -qE '<(failure|error)' "$xml"; then
      echo "--- $xml ---"
      grep -E -B1 -A12 '<(failure|error)' "$xml" | head -n 150
    fi
  done
} > "$report" 2>&1

head -c 60000 "$report" > build-logs/summary-trimmed.txt
jq -n --arg sha "$GITHUB_SHA" --rawfile text build-logs/summary-trimmed.txt \
  '{name: "CI diagnostics", head_sha: $sha, status: "completed", conclusion: "neutral",
    output: {title: "Build failure details", summary: "Condensed Gradle, lint and test failure output.", text: $text}}' \
  | gh api "repos/$GITHUB_REPOSITORY/check-runs" --input - > /dev/null \
  && echo "Published diagnostics check run." || echo "Could not publish diagnostics check run."
