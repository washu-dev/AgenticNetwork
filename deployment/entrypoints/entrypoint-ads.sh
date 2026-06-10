#!/bin/sh
mkdir -p /tmp/ads/store /tmp/ads/routing
exec /usr/local/bin/dirctl daemon start --config /app/ads_daemon.yaml
