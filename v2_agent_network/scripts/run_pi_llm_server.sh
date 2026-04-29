#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT"

PI_ENV_FILE="${PI_ENV_FILE:-$REPO_ROOT/v2_agent_network/.env.pi.local}"
if [[ -f "$PI_ENV_FILE" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "$PI_ENV_FILE"
  set +a
fi

export PI_LLM_ENABLED=true
export PI_LLM_PROVIDER="${PI_LLM_PROVIDER:-openai}"
export PI_LLM_MODEL="${PI_LLM_MODEL:-gpt-5.5}"
export PI_LLM_REASONING_EFFORT="${PI_LLM_REASONING_EFFORT:-low}"
export PI_LLM_VERBOSITY="${PI_LLM_VERBOSITY:-low}"
export PI_LLM_MAX_OUTPUT_TOKENS="${PI_LLM_MAX_OUTPUT_TOKENS:-1200}"
export PI_LLM_TIMEOUT_SECONDS="${PI_LLM_TIMEOUT_SECONDS:-30}"
export PI_LLM_ALLOW_DETERMINISTIC_FALLBACK="${PI_LLM_ALLOW_DETERMINISTIC_FALLBACK:-false}"
export PI_LLM_USE_FOR_FINAL_SUMMARY="${PI_LLM_USE_FOR_FINAL_SUMMARY:-true}"
export PI_AGENT_MODE="${PI_AGENT_MODE:-tool_loop}"
export PI_POLICY_MODE="${PI_POLICY_MODE:-permissive}"
export PI_LOG_LEVEL="${PI_LOG_LEVEL:-INFO}"

export AGENT_NETWORK_FAKE_ADS="${AGENT_NETWORK_FAKE_ADS:-false}"
export ADS_BACKEND="${ADS_BACKEND:-cli}"
export ADS_SERVER_ADDR="${ADS_SERVER_ADDR:-localhost:8888}"

PI_AGENT_HOST="${PI_AGENT_HOST:-0.0.0.0}"
PI_AGENT_PORT="${PI_AGENT_PORT:-8080}"
PYTHON_BIN="${PYTHON_BIN:-$REPO_ROOT/v1_classification/.venv/bin/python}"
export PYTHONUNBUFFERED="${PYTHONUNBUFFERED:-1}"

if [[ -z "${OPENAI_API_KEY:-}" ]]; then
  echo "OPENAI_API_KEY is required. Put it in $PI_ENV_FILE or export it before running this script." >&2
  exit 2
fi

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Python interpreter not found or not executable: $PYTHON_BIN" >&2
  exit 2
fi

echo "Starting PI LLM server host=$PI_AGENT_HOST port=$PI_AGENT_PORT ads=$ADS_SERVER_ADDR llm_model=$PI_LLM_MODEL agent_mode=$PI_AGENT_MODE policy_mode=$PI_POLICY_MODE log_level=$PI_LOG_LEVEL" >&2

args=(
  -m v2_agent_network.scripts.run_pi_agent_server
  --host "$PI_AGENT_HOST"
  --port "$PI_AGENT_PORT"
  --llm
  --llm-model "$PI_LLM_MODEL"
)

if [[ "${AGENT_NETWORK_FAKE_ADS,,}" == "true" ]]; then
  args+=(--fake-ads)
fi

exec "$PYTHON_BIN" "${args[@]}" "$@"
