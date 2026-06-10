# ADS (agent directory service) — copies the dirctl binary into debian:slim
# so Pyxis/enroot has a shell to launch the container.
# Build from repo root:
#   docker build --platform linux/amd64 -f deployment/dockerfiles/Dockerfile.ads \
#       -t amaankhan25/agenticnetwork-ads:latest .
FROM ghcr.io/agntcy/dir-ctl:v1.2.0 AS dirctl-src

FROM debian:12-slim

RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=dirctl-src /dirctl /usr/local/bin/dirctl
RUN chmod +x /usr/local/bin/dirctl

# Use /var/lib/ads/ instead of /tmp/ — tmpfs in containers wipes /tmp at runtime.
RUN mkdir -p /var/lib/ads/store /var/lib/ads/routing

COPY deployment/ads_daemon.yaml /app/ads_daemon.yaml

EXPOSE 8888 8999

ENTRYPOINT ["/usr/local/bin/dirctl", "daemon", "start", "--config", "/app/ads_daemon.yaml"]
