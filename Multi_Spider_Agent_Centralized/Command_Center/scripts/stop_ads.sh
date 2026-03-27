#!/bin/bash
# Stop Agent Directory Service (ADS)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
COMPOSE_FILE="$SCRIPT_DIR/../docker-compose-ads.yaml"

echo "Stopping ADS services..."
docker compose -f "$COMPOSE_FILE" down
echo "ADS stopped."
