#!/usr/bin/env bash
# End-to-end smoke test for Scout's networked (serve) mode:
#   1. docker compose up: local AGNTCY directory daemon running
#   2. start `scout serve --no-repl` in background
#   3. wait for it to publish to directory
#   4. invoke scripts/call_scout.py against the running Scout
#   5. tear down
#
# Skip step 1 if you're pointing DIRECTORY_ENDPOINT at a remote / shared dir.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

[ -f .env ] || { echo "missing .env (cp .env.example .env first)"; exit 1; }
set -a; source .env; set +a

[ -n "${OPENAI_API_KEY:-}" ] || { echo "missing OPENAI_API_KEY"; exit 1; }
[ -n "${BRAVE_API_KEY:-}"  ] || { echo "missing BRAVE_API_KEY"; exit 1; }

# 1. Infra
echo "[smoke] bringing infra up..."
docker compose up -d
sleep 4

# 2. Scout serve in background
SERVE_LOG="$(mktemp)"
echo "[smoke] starting scout serve --no-repl (log: $SERVE_LOG)"
.venv/bin/scout serve --no-repl -v >"$SERVE_LOG" 2>&1 &
SERVE_PID=$!
trap 'echo "[smoke] stopping scout (pid $SERVE_PID)"; kill $SERVE_PID 2>/dev/null || true; wait $SERVE_PID 2>/dev/null || true' EXIT

# 3. Wait for publish
for i in {1..30}; do
    if grep -q "published to directory" "$SERVE_LOG"; then
        echo "[smoke] scout published"
        break
    fi
    sleep 1
done

if ! grep -q "published to directory" "$SERVE_LOG"; then
    echo "[smoke] FAIL: scout did not publish in time"
    echo "--- serve log ---"
    cat "$SERVE_LOG"
    exit 1
fi

# 4. External call
echo "[smoke] calling scout from outside..."
OUT="$(mktemp)"
if .venv/bin/python scripts/call_scout.py "Briefly: one notable shipping development in SE Asia this month?" >"$OUT" 2>&1; then
    echo "[smoke] external call returned:"
    head -40 "$OUT"
    if [ -s "$OUT" ]; then
        echo "[smoke] OK"
    else
        echo "[smoke] FAIL: empty response"
        exit 1
    fi
else
    echo "[smoke] FAIL: call_scout.py exited non-zero"
    cat "$OUT"
    exit 1
fi
