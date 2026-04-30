#!/usr/bin/env bash
# Start the v3 main agent server, auto-loading v3_main_agent/.env.local.
#
# Usage:
#   v3_main_agent/scripts/start.sh                 # uses ADS mode from .env.local
#   v3_main_agent/scripts/start.sh --fake-ads      # force in-process fake ADS
#   v3_main_agent/scripts/start.sh --card path/to/agent.card.json   # repeatable
#
# Override the env file:
#   ENV_FILE=/path/to/other.env v3_main_agent/scripts/start.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
V3_DIR="$(dirname "$SCRIPT_DIR")"
REPO_ROOT="$(dirname "$V3_DIR")"

ENV_FILE="${ENV_FILE:-$V3_DIR/.env.local}"
if [[ ! -f "$ENV_FILE" ]]; then
  echo "ENV file not found: $ENV_FILE" >&2
  echo "Copy v3_main_agent/.env.example to v3_main_agent/.env.local and fill in OPENAI_API_KEY." >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

if [[ "${OPENAI_API_KEY:-sk-REPLACE_ME}" == sk-REPLACE_ME* ]]; then
  echo "OPENAI_API_KEY in $ENV_FILE is still the placeholder. Edit the file and put your real key." >&2
  exit 1
fi

PYTHON_BIN="${PYTHON_BIN:-/home/server2/miniconda3/envs/mas_spider/bin/python}"
if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Python interpreter not found at $PYTHON_BIN" >&2
  echo "Override with: PYTHON_BIN=/path/to/python v3_main_agent/scripts/start.sh" >&2
  exit 1
fi

cd "$REPO_ROOT"
echo "main_agent_start env=$ENV_FILE port=${MAIN_AGENT_PORT:-8090} fake_ads=${AGENT_NETWORK_FAKE_ADS:-false} model=${MAIN_AGENT_LLM_MODEL:-gpt-4o-mini}"
exec "$PYTHON_BIN" -m v3_main_agent.scripts.run_server "$@"
