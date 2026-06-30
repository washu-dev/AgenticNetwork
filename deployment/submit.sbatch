#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# submit.sbatch — launches the AgenticNetwork multi-container deployment on
# a single ENGR CPU node using one srun step per agent image.
#
# Prerequisites:
#   1. Build and push all images (run once from your local machine):
#        bash deployment/build_and_push.sh
#
#   2. Fill in deployment/.env (copy from deployment/.env.example).
#      Make sure OPENAI_BASE_URL points to your running vLLM job's node.
#
#   3. Submit from the repo root on the cluster:
#        sbatch deployment/submit.sbatch
# ─────────────────────────────────────────────────────────────────────────────

#SBATCH --job-name=agenticnetwork
#SBATCH -p condo-di2
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=16
#SBATCH --mem=64G
#SBATCH -A engr-lab-di2
#SBATCH --output=/engrfs/project/di2/AgenticNetwork/logs/%u/agenticnetwork-%j.log
#SBATCH --error=/engrfs/project/di2/AgenticNetwork/logs/%u/agenticnetwork-%j.err

LOGS=/engrfs/project/di2/AgenticNetwork/logs/$USER
ENV_FILE=/engrfs/project/di2/AgenticNetwork/deployment/.env
MOUNTS=/engrfs/project/di2/AgenticNetwork:/workspace

mkdir -p "$LOGS"

# Load env vars — inherited by all srun steps via Slurm environment propagation.
set -a
source "$ENV_FILE"
set +a

echo "======================================================"
echo " AgenticNetwork starting on $(hostname)"
echo " Job ID   : ${SLURM_JOB_ID}"
echo " vLLM node: ${OPENAI_BASE_URL}"
echo "======================================================"

# ── ADS daemon ────────────────────────────────────────────────────────────────
srun --ntasks=1 --overlap \
     --container-image=amaankhan25/agenticnetwork-ads:v4 \
     --container-mounts="$MOUNTS" \
     --container-writable \
     /usr/local/bin/dirctl daemon start --config /workspace/deployment/ads_daemon.yaml \
     >> "$LOGS/ads.log" 2>&1 &

# ── Wait for ADS (coarse poll — agent entrypoints also retry for up to 60s) ──
echo "[submit] Waiting for ADS on port 8888..."
for i in $(seq 1 30); do
    if python3 -c "import socket; socket.create_connection(('localhost', 8888), timeout=1).close()" 2>/dev/null; then
        echo "[submit] ADS ready (${i}s)."
        break
    fi
    if [ "$i" -eq 30 ]; then
        echo "[submit] ERROR: ADS not ready after 30s. Check $LOGS/ads.log"
        exit 1
    fi
    sleep 1
done

# ── Port assignments (single source of truth — entrypoints read these env vars) ─
# Each variable name matches what the agent's entrypoint.sh reads.
# Exported so Slurm propagates them into every container automatically.
export MAIN_AGENT_PORT=8090
export REGISTRATION_SERVICE_PORT=8091
export EMAIL_AGENT_A2A_PORT=9103
export SCOUT_HTTP_PORT=9104
export GEOSPATIAL_AGENT_A2A_PORT=19105
export CODING_AGENT_A2A_PORT=19106
export INTEL_AGENT_A2A_PORT=19107
export DISASTER_AGENT_A2A_PORT=19108

# ── Custom agent spawning (registration service) ─────────────────────────────
# ClusterBackend + GitHub→DockerHub build pipeline for approved custom agents.
# (Also settable in .env; exported here so they propagate even if .env omits them.)
export SPAWN_BACKEND="${SPAWN_BACKEND:-cluster}"
export HANDLER_FILES_DIR="${HANDLER_FILES_DIR:-/workspace/registration_service/uploaded_handlers}"

# ── Agent containers (all start in parallel after ADS is up) ─────────────────
srun --ntasks=1 --overlap \
     --container-image=amaankhan25/agenticnetwork-registration:latest \
     --container-mounts="$MOUNTS,/engrfs/project/di2/postgres_data:/var/lib/postgresql/data" \
     --container-writable \
     /entrypoint.sh \
     >> "$LOGS/registration.log" 2>> "$LOGS/registration.err" &

srun --ntasks=1 --overlap \
     --container-image=amaankhan25/agenticnetwork-planner:latest \
     --container-mounts="$MOUNTS" \
     --container-writable \
     /entrypoint.sh \
     >> "$LOGS/planner.log" 2>> "$LOGS/planner.err" &

srun --ntasks=1 --overlap \
     --container-image=amaankhan25/agenticnetwork-scout:latest \
     --container-mounts="$MOUNTS" \
     --container-writable \
     /entrypoint.sh \
     >> "$LOGS/scout.log" 2>> "$LOGS/scout.err" &

srun --ntasks=1 --overlap \
     --container-image=amaankhan25/agenticnetwork-email:latest \
     --container-mounts="$MOUNTS" \
     --container-writable \
     /entrypoint.sh \
     >> "$LOGS/email.log" 2>> "$LOGS/email.err" &

srun --ntasks=1 --overlap \
     --container-image=amaankhan25/agenticnetwork-coding:latest \
     --container-mounts="$MOUNTS" \
     --container-writable \
     /entrypoint.sh \
     >> "$LOGS/coding.log" 2>> "$LOGS/coding.err" &

srun --ntasks=1 --overlap \
     --container-image=amaankhan25/agenticnetwork-intelligence:latest \
     --container-mounts="$MOUNTS" \
     --container-writable \
     /entrypoint.sh \
     >> "$LOGS/intelligence.log" 2>> "$LOGS/intelligence.err" &

srun --ntasks=1 --overlap \
     --container-image=amaankhan25/agenticnetwork-disaster:latest \
     --container-mounts="$MOUNTS" \
     --container-writable \
     /entrypoint.sh \
     >> "$LOGS/disaster.log" 2>> "$LOGS/disaster.err" &

srun --ntasks=1 --overlap \
     --container-image=amaankhan25/agenticnetwork-geospatial:latest \
     --container-mounts="$MOUNTS" \
     --container-writable \
     /entrypoint.sh \
     >> "$LOGS/geospatial.log" 2>> "$LOGS/geospatial.err" &

# ── Host-side spawn loop for approved custom agents (plan Note #8) ────────────
# Runs in this native Slurm env (where `srun` works) — the registration container
# can't call srun itself. Polls the registration service's /internal/* endpoints.
if [ "${SPAWN_BACKEND:-}" = "cluster" ]; then
    echo "[submit] Launching host-side custom-agent spawn loop..."
    LOGS="$LOGS" \
    REGISTRATION_INTERNAL_URL="http://localhost:${REGISTRATION_SERVICE_PORT:-8091}" \
    bash /workspace/deployment/spawn_agent_loop.sh >> "$LOGS/spawn_agent_loop.log" 2>&1 &
fi

echo "[submit] All containers launched. Waiting..."
wait
echo "[submit] All containers exited. Job complete."
