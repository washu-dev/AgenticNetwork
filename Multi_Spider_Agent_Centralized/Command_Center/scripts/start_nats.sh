#!/usr/bin/env bash
# Start NATS server for A2A transport
set -euo pipefail
cd "$(dirname "$0")/.."

echo "Starting NATS server..."
docker compose -f docker-compose-nats.yaml up -d

echo "Waiting for NATS to be ready..."
for i in $(seq 1 20); do
    if curl -sf http://localhost:8222/varz > /dev/null 2>&1; then
        echo "NATS is ready on port 4222"
        echo "Monitor: http://localhost:8222"
        exit 0
    fi
    sleep 1
done

echo "ERROR: NATS did not become ready in time"
exit 1
