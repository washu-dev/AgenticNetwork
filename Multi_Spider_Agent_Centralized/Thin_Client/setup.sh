#!/bin/bash
# One-click setup for PiCrawler Thin Client
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$SCRIPT_DIR/.."
cd "$PROJECT_DIR"

echo "=== PiCrawler Thin Client Setup ==="

# 1. System dependencies
echo "[1/3] Installing system packages..."
sudo apt-get update -qq
sudo apt-get install -y -qq python3-venv python3-opencv > /dev/null

# 2. Create venv
if [ ! -d "venv" ]; then
    echo "[2/3] Creating virtual environment..."
    python3 -m venv --system-site-packages venv
else
    echo "[2/3] venv already exists, skipping"
fi
source venv/bin/activate

# 3. Install Python dependencies
echo "[3/3] Installing Python dependencies..."
pip install -r requirements.txt -q

# Remove venv numpy if present (conflicts with system simplejpeg)
rm -rf venv/lib/python3.*/site-packages/numpy* venv/lib/python3.*/site-packages/~umpy* 2>/dev/null || true

# 4. Setup .env if not exists
if [ ! -f .env ]; then
    HOSTNAME=$(hostname)
    cat > .env <<EOF
AGENT_ID=${HOSTNAME}-crawler-001
EOF
    echo "Created .env — edit AGENT_ID as needed"
fi

echo ""
echo "=== Setup complete ==="
echo "Next steps:"
echo "  1. Edit .env (set unique AGENT_ID)"
echo "  2. ./scripts/start.sh"
