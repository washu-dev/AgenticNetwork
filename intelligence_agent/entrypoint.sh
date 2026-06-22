#!/bin/bash
set -euo pipefail

ADS_ADDR="${ADS_SERVER_ADDR:-localhost:8888}"
ADS_HOST="${ADS_ADDR%%:*}"
ADS_PORT="${ADS_ADDR##*:}"

echo "[intelligence] Waiting for ADS at ${ADS_ADDR}..."
for i in $(seq 1 60); do
    if /opt/conda/envs/intelligence-agent-a2a/bin/python -c "import socket; socket.create_connection(('${ADS_HOST}', ${ADS_PORT}), timeout=1).close()" 2>/dev/null; then
        echo "[intelligence] ADS ready (${i}s)."
        break
    fi
    if [ "$i" -eq 60 ]; then
        echo "[intelligence] ERROR: ADS not ready after 60s."
        exit 1
    fi
    sleep 1
done

cd /app/intelligence_agent
exec /opt/conda/envs/intelligence-agent-a2a/bin/python intelligence_agent.py serve-a2a \
    --host "${INTEL_AGENT_A2A_HOST:-0.0.0.0}" \
    --port "${INTEL_AGENT_A2A_PORT:-9105}" \
    --advertise-url "http://localhost:${INTEL_AGENT_A2A_PORT:-9105}/" \
    --directory-server "${ADS_ADDR}"
