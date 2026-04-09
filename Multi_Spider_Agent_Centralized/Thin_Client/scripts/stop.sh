#!/bin/bash
# Emergency stop: kill process, then sit robot down
# Usage: ./stop.sh

echo "=== EMERGENCY STOP ==="

# 1. Kill thin client (releases GPIO + camera)
echo "[1/2] Killing thin client..."
pkill -9 -f "agent_spider.thin_client" 2>/dev/null && echo "      Killed" || echo "      Was not running"
sleep 2

# 2. Reset robot to default position
echo "[2/2] Resetting robot to default position..."
python3 -c "
from picrawler import Picrawler
p = Picrawler()
print('      Robot reset to default.')
" 2>/dev/null || echo "      Skip (hardware not available)"

# Clean PID files
rm -f /tmp/picrawler_agent.pid

echo ""
echo "All stopped."
