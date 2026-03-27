#!/bin/bash
# Stop Multi-Spider Agent Command Center
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PID_FILE="$SCRIPT_DIR/.pid"

stopped=false

# Try PID file first
if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    if kill -0 "$PID" 2>/dev/null; then
        kill "$PID" 2>/dev/null
        sleep 1
        # Force kill if still alive
        if kill -0 "$PID" 2>/dev/null; then
            kill -9 "$PID" 2>/dev/null
        fi
        echo "Stopped Command Center (PID: $PID)"
        stopped=true
    fi
    rm -f "$PID_FILE"
fi

# Also kill any remaining processes by name
PIDS=$(pgrep -f "Multi_Spider_Agent_Command_Center.main" 2>/dev/null)
if [ -n "$PIDS" ]; then
    kill $PIDS 2>/dev/null
    sleep 1
    # Force kill survivors
    PIDS=$(pgrep -f "Multi_Spider_Agent_Command_Center.main" 2>/dev/null)
    if [ -n "$PIDS" ]; then
        kill -9 $PIDS 2>/dev/null
    fi
    if [ "$stopped" = false ]; then
        echo "Stopped Command Center (PID: $PIDS)"
    fi
    stopped=true
fi

if [ "$stopped" = false ]; then
    echo "Command Center is not running."
fi
