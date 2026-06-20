#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PYTHON_BIN="${ADS_PYTHON:-${SCRIPT_DIR}/.venv/bin/python}"
DIRCTL_BIN="${DIRCTL_BIN:-dirctl}"

SERVER_ADDR="${ADS_SERVER_ADDR:-localhost:8888}"

usage() {
  cat <<EOF
Usage:
  agent_directory_service/run_ads.sh [--server-addr <host:port>] <command>

Global options:
  --server-addr <host:port>  ADS server address. Default: ${SERVER_ADDR}

Commands:
  -list                      Show routed agents with names, endpoints, labels, times.
  -raw-list                  Run raw dirctl routing list JSON.
  -diag                      Diagnose routed, queued, stale, and orphaned records.
  -watch                     Watch ADS publish/unpublish events and repair missing routes.
  -repair                    Route latest active stored records; no deletion.
  -repair-delete             Route latest active records and delete superseded records.
  -publish <card>            Publish one Agent Card to ADS.
  -remove <cid> [cid...]     Remove CID(s) from routing with routing unpublish.
  -delete <cid> [cid...]     Unpublish then delete CID(s) from ADS storage.
  -help                      Show this help.

Examples:
  agent_directory_service/run_ads.sh -list
  agent_directory_service/run_ads.sh -diag
  agent_directory_service/run_ads.sh -publish email_agent/email_agent.card.json
  agent_directory_service/run_ads.sh -remove bae...
  agent_directory_service/run_ads.sh --server-addr 127.0.0.1:8888 -raw-list
EOF
}

require_python() {
  if [[ ! -x "${PYTHON_BIN}" ]]; then
    echo "Missing Python venv at ${PYTHON_BIN}" >&2
    echo "Create it with: python3 -m venv agent_directory_service/.venv && ${PYTHON_BIN} -m pip install -r agent_directory_service/requirements.txt" >&2
    exit 2
  fi
}

require_cids() {
  if [[ "$#" -lt 1 ]]; then
    echo "This command requires at least one CID." >&2
    exit 2
  fi
}

if [[ "$#" -eq 0 ]]; then
  usage
  exit 0
fi

while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --server-addr)
      SERVER_ADDR="${2:?missing value for --server-addr}"
      shift 2
      ;;
    -h|-help|--help)
      usage
      exit 0
      ;;
    *)
      ACTION="${1#-}"
      shift
      break
      ;;
  esac
done

cd "${REPO_ROOT}"

case "${ACTION:-}" in
  list)
    require_python
    exec "${PYTHON_BIN}" -m agent_directory_service.scripts.show_ads_routes \
      --server-addr "${SERVER_ADDR}" "$@"
    ;;
  raw-list)
    exec "${DIRCTL_BIN}" --server-addr "${SERVER_ADDR}" routing list --output json "$@"
    ;;
  diag|diagnose)
    require_python
    exec "${PYTHON_BIN}" -m agent_directory_service.scripts.ads_diagnostics \
      --server-addr "${SERVER_ADDR}" "$@"
    ;;
  watch)
    require_python
    exec "${PYTHON_BIN}" -m agent_directory_service.scripts.watch_ads_routing \
      --server-addr "${SERVER_ADDR}" "$@"
    ;;
  repair)
    require_python
    exec "${PYTHON_BIN}" -m agent_directory_service.scripts.force_route_active_agents \
      --server-addr "${SERVER_ADDR}" \
      --restart-daemon-if-stuck "$@"
    ;;
  repair-delete)
    require_python
    exec "${PYTHON_BIN}" -m agent_directory_service.scripts.force_route_active_agents \
      --server-addr "${SERVER_ADDR}" \
      --restart-daemon-if-stuck \
      --delete-superseded "$@"
    ;;
  publish)
    if [[ "$#" -ne 1 ]]; then
      echo "Usage: agent_directory_service/run_ads.sh -publish <agent.card.json>" >&2
      exit 2
    fi
    require_python
    AGENT_NETWORK_FAKE_ADS=false ADS_BACKEND=cli ADS_SERVER_ADDR="${SERVER_ADDR}" \
      exec "${PYTHON_BIN}" -m agent_directory_service.scripts.publish_agents --card "$1"
    ;;
  remove|unpublish)
    require_cids "$@"
    for cid in "$@"; do
      echo "Unpublishing from routing: ${cid}"
      "${DIRCTL_BIN}" --server-addr "${SERVER_ADDR}" routing unpublish "${cid}" --output json
    done
    ;;
  delete)
    require_cids "$@"
    for cid in "$@"; do
      echo "Unpublishing from routing if present: ${cid}"
      "${DIRCTL_BIN}" --server-addr "${SERVER_ADDR}" routing unpublish "${cid}" --output json || true
      echo "Deleting from ADS storage: ${cid}"
      "${DIRCTL_BIN}" --server-addr "${SERVER_ADDR}" delete "${cid}" --output json
    done
    ;;
  *)
    echo "Unknown command: ${ACTION:-}" >&2
    usage >&2
    exit 2
    ;;
esac
