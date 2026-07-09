#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# deploy.sh — launch an AgenticNetwork deployment on the ENGR cluster.
#
#   deployment/deploy.sh            # prod (canonical instance, default)
#   deployment/deploy.sh prod       # prod (explicit)
#   deployment/deploy.sh test       # isolated test instance, may run BESIDE prod
#
# prod is the one canonical deployment (shared /engrfs Postgres holding real, encrypted
# user credentials). test is an isolated copy: all ports are offset by +10000 and its
# Postgres lives on node-local per-user scratch (/scratch/$USER), so a test run cannot
# collide with — or touch the data of — a live prod instance.
#
# A second deploy of the SAME instance (two prods, or two tests) is refused by the
# preflight in submit.sbatch, across all users.
#
# To test a candidate image without clobbering prod's :latest, build it under a tag
# and pass a matching per-service override (propagated via --export=ALL):
#   IMAGE_TAG=dev-me bash deployment/build_and_push.sh planner
#   PLANNER_IMAGE_TAG=dev-me deployment/deploy.sh test
# See docs/MULTI_CONTRIBUTOR.md.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

ENV="${1:-prod}"
case "$ENV" in
    prod) OFFSET=0 ;;
    test) OFFSET=10000 ;;
    *) echo "usage: $0 [prod|test]" >&2; exit 2 ;;
esac

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

exec sbatch \
    --job-name="agenticnetwork-${ENV}" \
    --export=ALL,DEPLOY_ENV="${ENV}",PORT_OFFSET="${OFFSET}" \
    "${HERE}/submit.sbatch"
