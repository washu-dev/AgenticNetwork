#!/bin/bash
# Build and push Docker images for infrastructure components (planner, ADS, registration).
# Specialist agents (email, coding, intelligence, disaster, geospatial, scout) have
# moved to the agent-directory repo — build those from there.
#
# Run from the AgenticNetwork repo root:
#   bash deployment/build_and_push.sh
#
# To build a single image:
#   bash deployment/build_and_push.sh planner
#
# To build specialist agents, run from the agent-directory repo root:
#   bash deployment/build_and_push.sh  (once that repo has its own copy of this script)

set -euo pipefail

REGISTRY=amaankhan25
TAG=latest
PLATFORM=linux/amd64

AGENTS=(ads planner registration)

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
