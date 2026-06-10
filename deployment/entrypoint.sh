#!/bin/bash
# entrypoint.sh — starts the ADS daemon, waits for it to be ready, then
# hands off to supervisord which manages all agent processes.
set -euo pipefail

# ── Runtime dirs (safe to call multiple times) ────────────────────────────────
mkdir -p /tmp/ads/store /tmp/ads/routing /var/log/supervisor /var/run/supervisor

# ── ADS daemon ────────────────────────────────────────────────────────────────
ADS_LOG=/engrfs/project/di2/AgenticNetwork/logs/ads.log
echo "[entrypoint] Starting ADS daemon (dirctl)..."
dirctl daemon start --config /app/deployment/ads_daemon.yaml \
    >> "$ADS_LOG" 2>&1 &
ADS_PID=$!

# Wait up to 30 s for dirctl to log "Daemon ready"
echo "[entrypoint] Waiting for ADS to be ready..."
for i in $(seq 1 30); do
    if grep -q "Daemon ready" "$ADS_LOG" 2>/dev/null; then
        echo "[entrypoint] ADS is ready (${i}s)."
        break
    fi
    if [ "$i" -eq 30 ]; then
        echo "[entrypoint] ERROR: ADS did not become ready within 30 s. Check $ADS_LOG"
        exit 1
    fi
    sleep 1
done

# ── Hand off to supervisord ───────────────────────────────────────────────────
echo "[entrypoint] Starting agents via supervisord..."
exec /usr/bin/supervisord -n -c /etc/supervisor/conf.d/agenticnetwork.conf
