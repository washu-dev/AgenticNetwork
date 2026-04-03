#!/bin/bash
# Start all Robot Agent processes from robots.json
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR/.."

CONFIG="$SCRIPT_DIR/robots.json"
if [ ! -f "$CONFIG" ]; then
    echo "ERROR: $CONFIG not found"
    exit 1
fi

PYTHON="$SCRIPT_DIR/venv/bin/python"
if [ ! -e "$PYTHON" ]; then
    PYTHON=python
fi

PIDS=()
COUNT=$("$PYTHON" -c "import json; print(len(json.load(open('$CONFIG'))))")

echo "Starting $COUNT Robot Agent(s)..."
echo ""

for i in $(seq 0 $((COUNT-1))); do
    ROBOT_ID=$("$PYTHON" -c "import json; print(json.load(open('$CONFIG'))[$i]['robot_id'])")
    PI_HOST=$("$PYTHON" -c "import json; print(json.load(open('$CONFIG'))[$i]['pi_host'])")
    ZONE=$("$PYTHON" -c "import json; print(json.load(open('$CONFIG'))[$i].get('zone', ''))")
    ROBOT_TYPE=$("$PYTHON" -c "import json; print(json.load(open('$CONFIG'))[$i].get('robot_type', 'spider'))")

    LOG="/tmp/robot_agent_${ROBOT_ID}.log"
    "$PYTHON" -m Command_Center.robot_agent \
        --robot-id "$ROBOT_ID" \
        --pi-host "$PI_HOST" \
        --zone "$ZONE" \
        --robot-type "$ROBOT_TYPE" \
        > "$LOG" 2>&1 &
    PID=$!
    PIDS+=($PID)
    echo "  [$ROBOT_ID] PID=$PID zone=$ZONE pi=$PI_HOST log=$LOG"
done

echo ""
echo "All Robot Agents started. PIDs: ${PIDS[*]}"
echo "Stop: kill ${PIDS[*]}"

# Save PIDs
printf "%s\n" "${PIDS[@]}" > /tmp/robot_agents.pids

trap "kill ${PIDS[*]} 2>/dev/null" EXIT
wait
