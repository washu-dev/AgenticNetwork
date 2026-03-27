#!/bin/bash
# Start Agent Directory Service (ADS) via Docker Compose
# Run this ONCE before starting Command Center and Pi agents.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
COMPOSE_FILE="$SCRIPT_DIR/../docker-compose-ads.yaml"

echo "Starting ADS services..."
docker compose -f "$COMPOSE_FILE" up -d

echo ""
echo "Waiting for ADS to be ready..."
for i in $(seq 1 30); do
    if wget --no-verbose --tries=1 --spider http://127.0.0.1:8889/healthz/ready 2>/dev/null; then
        echo "ADS is ready! (gRPC: localhost:8888)"
        exit 0
    fi
    sleep 2
done

echo "WARNING: ADS health check timed out. Check: docker logs multi-spider-ads"
