#!/bin/bash
set -euo pipefail

ADS_ADDR="${ADS_SERVER_ADDR:-localhost:8888}"
ADS_HOST="${ADS_ADDR%%:*}"
ADS_PORT="${ADS_ADDR##*:}"

echo "[email] Waiting for ADS at ${ADS_ADDR}..."
for i in $(seq 1 60); do
    if /opt/conda/envs/email-agent-a2a/bin/python -c "import socket; socket.create_connection(('${ADS_HOST}', ${ADS_PORT}), timeout=1).close()" 2>/dev/null; then
        echo "[email] ADS ready (${i}s)."
        break
    fi
    if [ "$i" -eq 60 ]; then
        echo "[email] ERROR: ADS not ready after 60s."
        exit 1
    fi
    sleep 1
done

cd /app/email_agent
exec /opt/conda/envs/email-agent-a2a/bin/python email_agent.py serve-a2a \
    --host "${EMAIL_AGENT_A2A_HOST:-0.0.0.0}" \
    --port "${EMAIL_AGENT_A2A_PORT:-9103}" \
    --advertise-url "http://localhost:${EMAIL_AGENT_A2A_PORT:-9103}/"
