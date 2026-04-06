#!/bin/bash
# Start PiCrawler Thin Client (single process: control + stream)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$SCRIPT_DIR/.."

# Activate venv
if [ -d "$PROJECT_DIR/venv" ]; then
    source "$PROJECT_DIR/venv/bin/activate"
fi

LOG_DIR="$PROJECT_DIR/logs"
mkdir -p "$LOG_DIR"

# Start single process (control on 9004, stream on 9000)
cd "$PROJECT_DIR/.."

echo "Starting thin client (control + stream)..."
python -m agent_spider.thin_client >> "$LOG_DIR/thin_client.log" 2>&1 &
AGENT_PID=$!

# Wait for both ports to be ready
for i in $(seq 1 20); do
    sleep 1
    CONTROL_OK=$(curl -s --max-time 2 http://localhost:9004/health 2>/dev/null)
    STREAM_OK=$(curl -s --max-time 2 http://localhost:9000/status 2>/dev/null)
    if [ -n "$CONTROL_OK" ] && [ -n "$STREAM_OK" ]; then
        echo "Ready after ${i}s"
        break
    fi
done

echo ""
echo "============================================"
echo "  PiCrawler running (PID=$AGENT_PID)"
echo "  Control: port 9004"
echo "  Stream:  port 9000"
echo "  Stop:    ./stop.sh"
echo "============================================"

echo "$AGENT_PID" > /tmp/picrawler_agent.pid

trap "kill $AGENT_PID 2>/dev/null" EXIT
wait
