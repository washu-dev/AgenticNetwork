#!/usr/bin/env bash
# Stop NATS server
set -euo pipefail
cd "$(dirname "$0")/.."

echo "Stopping NATS server..."
docker compose -f docker-compose-nats.yaml down
echo "NATS stopped."
