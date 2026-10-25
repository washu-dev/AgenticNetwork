#!/bin/bash
set -euo pipefail

ADS_ADDR="${ADS_SERVER_ADDR:-localhost:8888}"
ADS_HOST="${ADS_ADDR%%:*}"
ADS_PORT="${ADS_ADDR##*:}"

echo "[chat-app] Waiting for ADS at ${ADS_ADDR}..."
for i in $(seq 1 60); do
    if /opt/conda/envs/chat-app/bin/python -c "import socket; socket.create_connection(('${ADS_HOST}', ${ADS_PORT}), timeout=1).close()" 2>/dev/null; then
        echo "[chat-app] ADS ready (${i}s)."
        break
    fi
    if [ "$i" -eq 60 ]; then
        echo "[chat-app] ERROR: ADS not ready after 60s."
        exit 1
    fi
    sleep 1
done

# New setting names first; the old MAIN_AGENT_* names still work as fallbacks.
exec /opt/conda/envs/chat-app/bin/python -m chat_app \
    --host "${CHAT_APP_HOST:-${MAIN_AGENT_HOST:-0.0.0.0}}" \
    --port "${CHAT_APP_PORT:-${MAIN_AGENT_PORT:-8090}}"
