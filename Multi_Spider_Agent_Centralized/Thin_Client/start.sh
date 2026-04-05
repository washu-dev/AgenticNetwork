#!/bin/bash
# Start PiCrawler Thin Client
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$SCRIPT_DIR/.."

# Activate venv
if [ -d "$PROJECT_DIR/venv" ]; then
    source "$PROJECT_DIR/venv/bin/activate"
fi

# Start camera
LOG_DIR="$PROJECT_DIR/logs"
mkdir -p "$LOG_DIR"
python3 "$PROJECT_DIR/stream.py" >> "$LOG_DIR/stream.log" 2>&1 &
STREAM_PID=$!
echo "[1/2] stream.py started (PID=$STREAM_PID, port 9000)"

# Wait for camera to be ready
for i in $(seq 1 15); do
    sleep 1
    if curl -s --max-time 2 http://localhost:9000/status > /dev/null 2>&1; then
        echo "      Camera ready after ${i}s"
        break
    fi
done

# Start thin client
cd "$PROJECT_DIR/.."

echo "[2/2] Starting thin client..."
python -m agent_spider.thin_client >> "$LOG_DIR/thin_client.log" 2>&1 &
AGENT_PID=$!

for i in $(seq 1 15); do
    sleep 1
    if curl -s --max-time 2 http://localhost:9004/health > /dev/null 2>&1; then
        echo "      Thin client ready after ${i}s"
        break
    fi
done

echo ""
echo "============================================"
echo "  All services running"
echo "  Camera:      PID=$STREAM_PID  port 9000"
echo "  Thin client: PID=$AGENT_PID  port 9004"
echo "  Stop:        ./stop.sh"
echo "============================================"

echo "$STREAM_PID" > /tmp/picrawler_stream.pid
echo "$AGENT_PID" > /tmp/picrawler_agent.pid

trap "kill $STREAM_PID $AGENT_PID 2>/dev/null" EXIT
wait
