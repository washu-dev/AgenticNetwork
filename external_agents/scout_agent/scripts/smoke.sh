#!/usr/bin/env bash
set -euo pipefail

[ -n "${OPENAI_API_KEY:-}" ] || { echo "missing OPENAI_API_KEY"; exit 1; }
[ -n "${BRAVE_API_KEY:-}" ]   || { echo "missing BRAVE_API_KEY"; exit 1; }

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

MISSION="Summarize developments in undersea cable infrastructure in the Indo-Pacific over the past 90 days, focusing on new builds, cuts, and policy actions."

OUT_FILE="$(mktemp)"
ERR_FILE="$(mktemp)"
trap 'rm -f "$OUT_FILE" "$ERR_FILE"' EXIT

python -m scout "$MISSION" \
    --max-iterations 6 \
    --max-tool-calls 8 \
    --verbose \
    >"$OUT_FILE" 2>"$ERR_FILE"

echo "--- BRIEF ---"
cat "$OUT_FILE"
echo "--- TRACE ---"
cat "$ERR_FILE"
echo "-------------"

grep -q "## Situation Summary"  "$OUT_FILE" || { echo "FAIL: missing Situation Summary"; exit 1; }
grep -q "## Trend Observations" "$OUT_FILE" || { echo "FAIL: missing Trend Observations"; exit 1; }
grep -q "## Sources"            "$OUT_FILE" || { echo "FAIL: missing Sources"; exit 1; }
grep -q "## Confidence & Gaps"  "$OUT_FILE" || { echo "FAIL: missing Confidence & Gaps"; exit 1; }
grep -qE 'https?://'            "$OUT_FILE" || { echo "FAIL: no URL citations"; exit 1; }

echo "smoke OK"
