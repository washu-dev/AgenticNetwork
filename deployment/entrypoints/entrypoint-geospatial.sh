#!/bin/bash
set -euo pipefail

ADS_ADDR="${ADS_SERVER_ADDR:-localhost:8888}"
ADS_HOST="${ADS_ADDR%%:*}"
ADS_PORT="${ADS_ADDR##*:}"

echo "[geospatial] Waiting for ADS at ${ADS_ADDR}..."
for i in $(seq 1 60); do
    if /opt/conda/envs/geospatial-agent/bin/python -c "import socket; socket.create_connection(('${ADS_HOST}', ${ADS_PORT}), timeout=1).close()" 2>/dev/null; then
        echo "[geospatial] ADS ready (${i}s)."
        break
    fi
    if [ "$i" -eq 60 ]; then
        echo "[geospatial] ERROR: ADS not ready after 60s."
        exit 1
    fi
    sleep 1
done

cd /app/geoSpatial_agent
exec /opt/conda/envs/geospatial-agent/bin/python geospatial_agent.py serve-a2a \
    --host "${GEOSPATIAL_AGENT_A2A_HOST:-0.0.0.0}" \
    --port "${GEOSPATIAL_AGENT_A2A_PORT:-19108}"
