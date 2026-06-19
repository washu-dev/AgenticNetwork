#!/bin/bash
# Build and push all per-agent Docker images to DockerHub.
# Run from the repo root:
#   bash deployment/build_and_push.sh
#
# To build a single image:
#   bash deployment/build_and_push.sh planner

set -euo pipefail

REGISTRY=amaankhan25
TAG=latest
PLATFORM=linux/amd64

AGENTS=(ads planner scout email coding intelligence disaster geospatial registration)

# If an agent name is passed as an argument, build only that one.
if [ $# -gt 0 ]; then
    AGENTS=("$@")
fi

for AGENT in "${AGENTS[@]}"; do
    IMAGE="${REGISTRY}/agenticnetwork-${AGENT}:${TAG}"
    DOCKERFILE="deployment/dockerfiles/Dockerfile.${AGENT}"

    echo "──────────────────────────────────────────"
    echo " Building: ${IMAGE}"
    echo "──────────────────────────────────────────"

    docker build \
        --platform "$PLATFORM" \
        -f "$DOCKERFILE" \
        -t "$IMAGE" \
        .

    echo " Pushing: ${IMAGE}"
    docker push "$IMAGE"
    echo " Done: ${IMAGE}"
done

echo ""
echo "All images built and pushed."
