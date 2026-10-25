#!/bin/bash
# Build and push Docker images for infrastructure components (chat app, ADS, registration).
# Specialist agents (email, coding, intelligence, disaster, geospatial, scout) have
# moved to the agent-directory repo — build those from there.
#
# Run from the AgenticNetwork repo root:
#   bash deployment/build_and_push.sh
#
# To build a single image:
#   bash deployment/build_and_push.sh chat-app
#
# To build a candidate under a personal tag WITHOUT clobbering :latest (for testing a
# change against a `deploy.sh test` instance — set the matching <SERVICE>_IMAGE_TAG there):
#   IMAGE_TAG=dev-alice bash deployment/build_and_push.sh chat-app
#
# To build specialist agents, run from the agent-directory repo root:
#   bash build_and_push.sh

set -euo pipefail

# Registry namespace images are built/pushed under. Group-owned rather than tied
# to a personal account; override via IMAGE_REGISTRY. See docs/MULTI_CONTRIBUTOR.md.
REGISTRY="${IMAGE_REGISTRY:-di2washu}"
# Tag to build/push under. Defaults to latest; set IMAGE_TAG=dev-<you> to publish a
# candidate for a test instance without overwriting the prod-facing :latest.
TAG="${IMAGE_TAG:-latest}"
PLATFORM=linux/amd64

AGENTS=(postgres ads chat-app registration)

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
