#!/bin/bash
set -euo pipefail

ADS_ADDR="${ADS_SERVER_ADDR:-localhost:8888}"
ADS_HOST="${ADS_ADDR%%:*}"
ADS_PORT="${ADS_ADDR##*:}"

echo "[coding] Waiting for ADS at ${ADS_ADDR}..."
for i in $(seq 1 60); do
    if /opt/conda/envs/coding-agent-a2a/bin/python -c "import socket; socket.create_connection(('${ADS_HOST}', ${ADS_PORT}), timeout=1).close()" 2>/dev/null; then
        echo "[coding] ADS ready (${i}s)."
        break
    fi
    if [ "$i" -eq 60 ]; then
        echo "[coding] ERROR: ADS not ready after 60s."
        exit 1
    fi
    sleep 1
done

cd /app/coding_agent
exec /opt/conda/envs/coding-agent-a2a/bin/python coding_agent.py serve-a2a \
    --host "${CODING_AGENT_A2A_HOST:-0.0.0.0}" \
    --port "${CODING_AGENT_A2A_PORT:-9106}"
