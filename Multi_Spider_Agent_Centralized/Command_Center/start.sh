#!/bin/bash
# Start Multi-Spider Agent Command Center
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PID_FILE="$SCRIPT_DIR/.pid"

# Check if already running
if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "Command Center is already running (PID: $(cat "$PID_FILE"))"
    exit 1
fi

# Resolve python: prefer venv, fallback to mas_spider conda env (needed for A2A + agntcy-app-sdk on Python 3.12)
PYTHON="${PYTHON_BIN:-$SCRIPT_DIR/venv/bin/python}"
if [ ! -e "$PYTHON" ]; then
    if [ -e "/home/server2/miniconda3/envs/mas_spider/bin/python" ]; then
        PYTHON="/home/server2/miniconda3/envs/mas_spider/bin/python"
    else
        PYTHON=python
    fi
fi
echo "Using Python: $PYTHON"

echo "Starting Multi-Spider Agent Command Center..."
echo "Press Ctrl+C to stop"
echo "---"

trap 'rm -f "$PID_FILE"' EXIT
echo $$ > "$PID_FILE"

cd "$SCRIPT_DIR/.."
DISCOVERY_MODE=ads ADS_SERVER_ADDRESS=localhost:8888 \
    exec "$PYTHON" -B -m Command_Center.main 2>&1
