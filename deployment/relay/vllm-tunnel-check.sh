#!/bin/bash
# Restart the vLLM tunnel when vLLM stops answering through it (run by vllm-tunnel-check.timer).
#
# A tunnel to a node whose job has ended stays connected but refuses every request, so
# systemd alone never restarts it. vLLM's /health needs no API key; any HTTP status
# means vLLM answered. Two failed checks in a row (the timer runs every 2 minutes)
# restart the tunnel, which looks the job's node up again.
set -uo pipefail
PORT="${VLLM_PORT:-8100}"
STATE=/run/vllm-tunnel-check.failures

code="$(curl -s -o /dev/null -m 10 -w '%{http_code}' "http://127.0.0.1:${PORT}/health" || true)"
if [ -n "$code" ] && [ "$code" != "000" ]; then
  rm -f "$STATE"
  exit 0
fi
failures=$(( $(cat "$STATE" 2>/dev/null || echo 0) + 1 ))
echo "$failures" > "$STATE"
echo "vLLM did not answer through the tunnel (check $failures)"
if [ "$failures" -ge 2 ]; then
  rm -f "$STATE"
  echo "restarting vllm-tunnel"
  systemctl restart vllm-tunnel.service
fi
