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
# A second deploy of the SAME instance (two prods, or two tests) is refused here at
# submit time, across all users — before sbatch is called, so you get an immediate
# message instead of a job stuck PENDING behind --dependency=singleton. The preflight
# inside submit.sbatch + that singleton dependency remain as backstops for anyone who
# bypasses this wrapper and runs sbatch directly.
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
JOB_NAME="agenticnetwork-${ENV}"

# Submit-time singleton: refuse if a job with this name is already running/queued (ANY
# user), so a second prod (or second test) can't be launched. Catches the same-user case
# that would otherwise sit silently PENDING(Dependency), and the cross-user case the
# Slurm --dependency=singleton can't see.
EXISTING="$(squeue -h -o '%A %u %t' -n "$JOB_NAME" -t RUNNING,PENDING 2>/dev/null || true)"
if [ -n "$EXISTING" ]; then
    echo "error: a '$JOB_NAME' deployment is already live or queued — refusing to submit a second." >&2
    echo "$EXISTING" | sed 's/^/  job: /' >&2
    echo "Cancel it first (scancel -n $JOB_NAME), or launch an isolated test: $0 test" >&2
    exit 1
fi

exec sbatch \
    --job-name="$JOB_NAME" \
    --export=ALL,DEPLOY_ENV="${ENV}",PORT_OFFSET="${OFFSET}" \
    "${HERE}/submit.sbatch"
