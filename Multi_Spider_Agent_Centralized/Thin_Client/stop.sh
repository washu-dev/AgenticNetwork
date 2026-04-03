#!/bin/bash
# Emergency stop: kill everything, then sit robot down
# Usage: ./stop.sh

echo "=== EMERGENCY STOP ==="

# 1. Kill thin client (releases GPIO)
echo "[1/3] Killing thin client..."
pkill -9 -f "agent_spider.thin_client" 2>/dev/null && echo "      Thin client killed" || echo "      Thin client was not running"
sleep 2

# 2. Reset robot to default position
echo "[2/3] Resetting robot to default position..."
python3 -c "
from picrawler import Picrawler
p = Picrawler()
print('      Robot reset to default.')
" 2>/dev/null || echo "      Skip (hardware not available)"

# 3. Kill camera stream
echo "[3/3] Stopping camera..."
pkill -9 -f "stream.py" 2>/dev/null && echo "      Camera stopped" || echo "      Camera was not running"

# Clean PID files
rm -f /tmp/picrawler_stream.pid /tmp/picrawler_agent.pid

echo ""
echo "All stopped."
