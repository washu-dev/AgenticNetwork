#!/usr/bin/env bash
# Planner Agent — 自检脚本
# 一键检查: env 配置 / dirctl 装好 / ADS 通 / server 在跑 / OpenAI 通
# 用法: planner_agent/scripts/doctor.sh

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLANNER_DIR="$(dirname "$SCRIPT_DIR")"
REPO_ROOT="$(dirname "$PLANNER_DIR")"
ENV_FILE="${ENV_FILE:-$PLANNER_DIR/.env.local}"
DIRCTL="${DIRCTL_BIN:-$HOME/.local/bin/dirctl}"
PORT="${MAIN_AGENT_PORT:-8090}"

if [[ -z "${PYTHON_BIN:-}" ]]; then
  if [[ -x "$PLANNER_DIR/.venv/bin/python" ]]; then
    PYTHON_BIN="$PLANNER_DIR/.venv/bin/python"
  elif [[ -x "$REPO_ROOT/.venv/bin/python" ]]; then
    PYTHON_BIN="$REPO_ROOT/.venv/bin/python"
  elif command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python3)"
  elif command -v python >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python)"
  else
    PYTHON_BIN=""
  fi
fi

GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[0;33m'; BOLD='\033[1m'; RESET='\033[0m'
ok()    { echo -e "  ${GREEN}✓${RESET} $1"; }
warn()  { echo -e "  ${YELLOW}⚠${RESET} $1"; }
fail()  { echo -e "  ${RED}✗${RESET} $1"; }
section() { echo -e "\n${BOLD}$1${RESET}"; }

# ---- 1. env file ----
section "1) env file"
if [[ -f "$ENV_FILE" ]]; then
  ok ".env file exists: $ENV_FILE"
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
  if [[ "${OPENAI_API_KEY:-sk-REPLACE_ME}" == sk-REPLACE_ME* ]]; then
    fail "OPENAI_API_KEY is still the placeholder — edit $ENV_FILE"
  else
    ok "OPENAI_API_KEY is set (${OPENAI_API_KEY:0:8}...)"
  fi
  ok "model:           ${MAIN_AGENT_LLM_MODEL:-(unset, default gpt-4o-mini)}"
  ok "reasoning_effort: ${MAIN_AGENT_REASONING_EFFORT:-(unset)}"
  ok "policy_mode:     ${MAIN_AGENT_POLICY_MODE:-(unset, default permissive)}"
  ok "fake_ads:        ${AGENT_NETWORK_FAKE_ADS:-(unset, default false)}"
  ok "ads_server:      ${ADS_SERVER_ADDR:-(unset!)}"
else
  fail "$ENV_FILE does not exist. Copy from .env.example."
fi

# ---- 2. python ----
section "2) python"
if [[ -n "$PYTHON_BIN" ]] && { command -v "$PYTHON_BIN" >/dev/null 2>&1 || [[ -x "$PYTHON_BIN" ]]; }; then
  ok "python: $PYTHON_BIN ($("$PYTHON_BIN" --version 2>&1))"
else
  fail "python not found. Create planner_agent/.venv, repo .venv, install python3, or set PYTHON_BIN=/path/to/python."
fi

# ---- 2. dirctl ----
section "3) dirctl binary"
if [[ -x "$DIRCTL" ]]; then
  ok "dirctl found: $DIRCTL ($($DIRCTL version 2>&1 | head -1))"
else
  if command -v dirctl >/dev/null 2>&1; then
    DIRCTL="$(command -v dirctl)"
    ok "dirctl found on PATH: $DIRCTL"
  else
    fail "dirctl not found. Install:"
    echo "    curl -fL -o ~/.local/bin/dirctl https://github.com/agntcy/dir/releases/download/v1.2.0/dirctl-linux-amd64"
    echo "    chmod +x ~/.local/bin/dirctl"
  fi
fi

# ---- 3. ADS reachability ----
section "4) ADS routing reachability"
ADS_ADDR="${ADS_SERVER_ADDR:-localhost:8888}"
if [[ "${AGENT_NETWORK_FAKE_ADS:-false}" == "true" ]]; then
  warn "fake_ads mode — skipping real ADS check"
elif [[ -x "$DIRCTL" ]] || command -v dirctl >/dev/null 2>&1; then
  if AGENTS=$("$DIRCTL" --server-addr "$ADS_ADDR" routing list --output json 2>&1); then
    COUNT=$(echo "$AGENTS" | "$PYTHON_BIN" -c "import sys, json; d = json.loads(sys.stdin.read()); print(len(d) if isinstance(d, list) else 0)" 2>/dev/null || echo "?")
    ok "ADS reachable at $ADS_ADDR — $COUNT agent(s) routed"
    if [[ "$COUNT" != "0" ]] && [[ "$COUNT" != "?" ]]; then
      echo "$AGENTS" | "$PYTHON_BIN" -c "
import sys, json
d = json.loads(sys.stdin.read())
for r in d:
    cid = r['record_ref']['cid']
    skills = [l for l in r.get('labels', []) if l.startswith('/skills/')][:3]
    print(f'    {cid[:14]}...  {len(r.get(\"labels\", []))} labels')
" 2>/dev/null || true
    fi
  else
    fail "dirctl could not reach ADS at $ADS_ADDR"
    echo "    output: $AGENTS" | head -3
  fi
fi

# ---- 4. planner agent server ----
section "5) planner agent server"
HEALTH=$(curl -s -m 3 "http://localhost:$PORT/health" 2>&1)
if [[ -n "$HEALTH" ]] && [[ "$HEALTH" == *'"status":"ok"'* ]]; then
  ok "server is running on port $PORT"
  ok "  $HEALTH"
  AGENTS_COUNT=$(curl -s -m 5 "http://localhost:$PORT/agents" 2>&1 | "$PYTHON_BIN" -c "import sys,json; print(json.load(sys.stdin).get('count', '?'))" 2>/dev/null || echo "?")
  ok "  /agents returns $AGENTS_COUNT discovered agents"
else
  fail "server NOT running on port $PORT"
  echo "    Start it: planner_agent/scripts/start.sh"
fi

# ---- 5. OpenAI quick ping ----
section "6) OpenAI ping"
if [[ -n "${OPENAI_API_KEY:-}" ]] && [[ "${OPENAI_API_KEY:0:3}" == "sk-" ]]; then
  CODE=$(curl -s -o /dev/null -w "%{http_code}" -m 5 \
    -H "Authorization: Bearer $OPENAI_API_KEY" \
    "https://api.openai.com/v1/models?limit=1")
  case "$CODE" in
    200) ok "OpenAI API key works (HTTP 200)" ;;
    401) fail "OpenAI API returned 401 — key invalid or expired" ;;
    429) warn "OpenAI API returned 429 — rate-limited but key works" ;;
    *)   warn "OpenAI API returned HTTP $CODE" ;;
  esac
else
  fail "OPENAI_API_KEY not set or malformed"
fi

# ---- 6. tests ----
section "7) test suite"
if cd "$REPO_ROOT" 2>/dev/null; then
  TEST_OUT=$(PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 "$PYTHON_BIN" -m pytest planner_agent/tests -q 2>&1 | tail -3)
  if echo "$TEST_OUT" | grep -q "passed"; then
    ok "$(echo "$TEST_OUT" | grep passed | head -1)"
  else
    fail "tests failed: $TEST_OUT"
  fi
else
  fail "could not cd to $REPO_ROOT"
fi

# ---- summary ----
echo ""
echo -e "${BOLD}Summary${RESET}"
echo "  Server URL:  http://localhost:$PORT/"
echo "  Logs:        tail -f /tmp/planner_agent_server.log"
echo "  Handover doc: cat $PLANNER_DIR/HANDOVER.md"
