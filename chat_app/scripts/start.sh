#!/usr/bin/env bash
# Start the chat app (web server and planner), auto-loading chat_app/.env.local.
#
# Usage:
#   chat_app/scripts/start.sh                 # uses ADS mode from .env.local
#   chat_app/scripts/start.sh --fake-ads      # force in-process fake ADS
#   chat_app/scripts/start.sh --card path/to/agent.card.json   # repeatable
#
# Override the env file:
#   ENV_FILE=/path/to/other.env chat_app/scripts/start.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLANNER_DIR="$(dirname "$SCRIPT_DIR")"
REPO_ROOT="$(dirname "$PLANNER_DIR")"

ENV_FILE="${ENV_FILE:-$PLANNER_DIR/.env.local}"
if [[ ! -f "$ENV_FILE" ]]; then
  echo "ENV file not found: $ENV_FILE" >&2
  echo "Copy chat_app/.env.example to chat_app/.env.local and fill in OPENAI_API_KEY." >&2
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
    echo "Python interpreter not found." >&2
    echo "Create chat_app/.venv, repo .venv, install python3, or run with:" >&2
    echo "  PYTHON_BIN=/path/to/python chat_app/scripts/start.sh" >&2
    exit 1
  fi
fi

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1 && [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Python interpreter not found: $PYTHON_BIN" >&2
  echo "Override with: PYTHON_BIN=/path/to/python chat_app/scripts/start.sh" >&2
  exit 1
fi

cd "$REPO_ROOT"
echo "chat_app_start python=$PYTHON_BIN env=$ENV_FILE port=${CHAT_APP_PORT:-${MAIN_AGENT_PORT:-8090}} fake_ads=${AGENT_NETWORK_FAKE_ADS:-false} model=${PLANNER_LLM_MODEL:-${MAIN_AGENT_LLM_MODEL:-gpt-4o-mini}}"
exec "$PYTHON_BIN" -m chat_app "$@"
