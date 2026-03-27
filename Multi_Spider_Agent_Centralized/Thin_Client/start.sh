#!/bin/bash
# Start PiCrawler Thin Client
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR/.."

# Activate venv if exists
if [ -d "$SCRIPT_DIR/venv" ]; then
    source "$SCRIPT_DIR/venv/bin/activate"
fi

echo "Starting Thin Client..."
python -m Multi_Spider_Agent_Thin_Client.thin_client
