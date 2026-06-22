#!/bin/bash
set -euo pipefail

ADS_ADDR="${ADS_SERVER_ADDR:-localhost:8888}"
ADS_HOST="${ADS_ADDR%%:*}"
ADS_PORT="${ADS_ADDR##*:}"

echo "[scout] Waiting for ADS at ${ADS_ADDR}..."
for i in $(seq 1 60); do
    if /opt/conda/envs/scout-agent/bin/python -c "import socket; socket.create_connection(('${ADS_HOST}', ${ADS_PORT}), timeout=1).close()" 2>/dev/null; then
        echo "[scout] ADS ready (${i}s)."
        break
    fi
    if [ "$i" -eq 60 ]; then
        echo "[scout] ERROR: ADS not ready after 60s."
        exit 1
    fi
    sleep 1
done

exec /opt/conda/envs/scout-agent/bin/python -m scout serve \
    --no-repl \
    --http-host "${SCOUT_HTTP_HOST:-0.0.0.0}" \
    --http-port "${SCOUT_HTTP_PORT:-9104}"
