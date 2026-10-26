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
#   3. Submit from the repo root on the cluster via the deploy.sh wrapper:
#        deployment/deploy.sh            # prod (canonical instance)
#        deployment/deploy.sh test       # isolated test instance (may run beside prod)
#
#      deploy.sh sets --job-name + DEPLOY_ENV/PORT_OFFSET. Submitting this file
#      directly still works and defaults to prod. See docs/MULTI_CONTRIBUTOR.md.
# ─────────────────────────────────────────────────────────────────────────────

#SBATCH --job-name=agenticnetwork-prod
# singleton: only one job with THIS name runs per user at a time — a second
# submission QUEUES until the first finishes. Combined with the cross-user preflight
# below (which covers OTHER users' jobs of the same name), this prevents two prod
# deployments sharing the /engrfs postgres_data volume and corrupting it. Prod and
# test carry different names (set by deploy.sh), so an isolated test instance may
# run alongside prod. See docs/MULTI_CONTRIBUTOR.md.
#SBATCH --dependency=singleton
#SBATCH -p condo-di2
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=16
#SBATCH --mem=64G
#SBATCH -A engr-lab-di2
#SBATCH --output=/engrfs/project/di2/AgenticNetwork/logs/%u/agenticnetwork-%j.log
#SBATCH --error=/engrfs/project/di2/AgenticNetwork/logs/%u/agenticnetwork-%j.err

REPO=/engrfs/project/di2/AgenticNetwork
LOGS=$REPO/logs/$USER
ENV_FILE=$REPO/deployment/.env
MOUNTS=$REPO:/workspace

mkdir -p "$LOGS"

# Load env vars — inherited by all srun steps via Slurm environment propagation.
# sbatch passes the submitting shell's environment on, so drop any database address
# from there first: it comes only from .env, the secrets file or the test block below.
unset DATABASE_URL REGISTRATION_DB_DSN
set -a
source "$ENV_FILE"
set +a

# Registry namespace all images are pulled from. Group-owned rather than tied to a
# personal account; override via IMAGE_REGISTRY (e.g. in .env). See docs/MULTI_CONTRIBUTOR.md.
IMAGE_REGISTRY="${IMAGE_REGISTRY:-di2washu}"

# Image tag resolution. Each service's tag is chosen in order:
#   1. <SERVICE>_IMAGE_TAG  (per-service override, e.g. CHAT_APP_IMAGE_TAG=dev-alice)
#   2. IMAGE_TAG            (instance-wide default; unset for prod, a SHA under CI later)
#   3. the service's own default (latest; v4 for ads)
# So a dev rebuilds ONLY the service(s) they changed under a personal tag (see
# build_and_push.sh) and everything else stays on :latest. See docs/MULTI_CONTRIBUTOR.md.
image_ref() {
    local svc="$1" default_tag="$2" var tag
    var="$(printf '%s' "$svc" | tr '[:lower:]-' '[:upper:]_')_IMAGE_TAG"   # chat-app -> CHAT_APP_IMAGE_TAG
    tag="${!var:-${IMAGE_TAG:-$default_tag}}"
    printf '%s/agenticnetwork-%s:%s' "$IMAGE_REGISTRY" "$svc" "$tag"
}

# ── Instance mode: prod (canonical, shared state) vs test (isolated) ───────────
# Set by deploy.sh. prod is the one canonical deployment; test is an isolated copy
# (offset ports + node-local Postgres) that may run beside prod. See docs/MULTI_CONTRIBUTOR.md.
DEPLOY_ENV="${DEPLOY_ENV:-prod}"
PORT_OFFSET="${PORT_OFFSET:-0}"
case "$DEPLOY_ENV" in
    prod|test) : ;;
    *) echo "[submit] ERROR: DEPLOY_ENV must be 'prod' or 'test' (got '$DEPLOY_ENV')"; exit 2 ;;
esac

# ── Secrets — kept OUT of the shared, world-readable .env ──────────────────────
# Real secrets (CREDENTIALS_ENCRYPTION_KEY, GITHUB_TOKEN, GITHUB_ISSUES_TOKEN,
# GOOGLE_CLIENT_SECRET) live in a restricted file, sourced AFTER .env so they override.
# prod reads a shared operator-owned file; test reads a per-user one. Override with SECRETS_FILE.
# See docs/MULTI_CONTRIBUTOR.md and deployment/secrets.env.example.
if [ "$DEPLOY_ENV" = "prod" ]; then
    SECRETS_FILE="${SECRETS_FILE:-$REPO/deployment/secrets.env}"
else
    SECRETS_FILE="${SECRETS_FILE:-$HOME/.config/agenticnetwork/secrets.env}"
fi
if [ -f "$SECRETS_FILE" ]; then
    set -a; source "$SECRETS_FILE"; set +a
    echo "[submit] Loaded secrets from $SECRETS_FILE"
else
    echo "[submit] WARN: secrets file not found ($SECRETS_FILE)."
    echo "[submit]       CREDENTIALS_ENCRYPTION_KEY and other secrets will be missing —"
    echo "[submit]       set SECRETS_FILE or create it (see deployment/secrets.env.example)."
fi

# One database address under both names (DATABASE_URL wins, as in agentnet/settings.py):
# images built before the 2026-10-25 rename read only REGISTRATION_DB_DSN.
DATABASE_URL="${DATABASE_URL:-${REGISTRATION_DB_DSN:-}}"
if [ -n "$DATABASE_URL" ]; then
    export DATABASE_URL REGISTRATION_DB_DSN="$DATABASE_URL"
fi

# Cross-user singleton per instance: refuse to start if ANOTHER job with the same
# name (this instance) is already live. Prod and test have different names, so they
# coexist; two prods (or two tests) do not — even across users, which the per-user
# --dependency=singleton above cannot see. This is the guardrail against a second
# deploy colliding on ports or the shared Postgres.
DUP="$(squeue -h -o '%A %j' -t RUNNING,PENDING 2>/dev/null \
        | awk -v n="$SLURM_JOB_NAME" -v self="$SLURM_JOB_ID" '$2==n && $1!=self {print $1}')"
if [ -n "$DUP" ]; then
    echo "[submit] ERROR: another '$SLURM_JOB_NAME' deployment is already live (job(s): ${DUP//$'\n'/ })."
    echo "[submit]        Refusing to start a second — it would collide on ports and Postgres."
    echo "[submit]        Cancel it first (scancel <jobid>), or run an isolated test: deployment/deploy.sh test"
    exit 1
fi

# Ports — offset by PORT_OFFSET so a test instance can run beside prod on one node.
# Single source of truth: each agent entrypoint reads its own *_PORT env var.
export CHAT_APP_PORT=$((8090 + PORT_OFFSET))
export REGISTRATION_SERVICE_PORT=$((8091 + PORT_OFFSET))
export EMAIL_AGENT_A2A_PORT=$((9103 + PORT_OFFSET))
export SCOUT_HTTP_PORT=$((9104 + PORT_OFFSET))
export GEOSPATIAL_AGENT_A2A_PORT=$((19105 + PORT_OFFSET))
export CODING_AGENT_A2A_PORT=$((19106 + PORT_OFFSET))
export INTEL_AGENT_A2A_PORT=$((19107 + PORT_OFFSET))
export DISASTER_AGENT_A2A_PORT=$((19108 + PORT_OFFSET))
ADS_PORT=$((8888 + PORT_OFFSET))
ADS_ROUTING_PORT=$((8999 + PORT_OFFSET))
PG_PORT=$((5432 + PORT_OFFSET))
# Repoint clients (agents, planner, registration) at THIS instance's ADS, Postgres and
# registration service. Only for an offset (test) instance — prod keeps whatever .env /
# the entrypoint defaults provide, so prod behavior is unchanged by this block.
if [ "$PORT_OFFSET" -ne 0 ]; then
    export ADS_SERVER_ADDR="localhost:${ADS_PORT}"
    export PGPORT="$PG_PORT"
    export DATABASE_URL="postgresql://postgres@localhost:${PG_PORT}/registration"
    # Images built before the 2026-10-25 rename read only the old name; set it to the
    # same value so a test instance on such an image uses its own database, never the
    # one prod's .env names.
    export REGISTRATION_DB_DSN="$DATABASE_URL"
    # The chat app's /register/* pass-through goes to this instance's registration service.
    export REGISTRATION_SERVICE_URL="http://localhost:${REGISTRATION_SERVICE_PORT}"
    # Offset the registration service's dynamic custom-agent port pool too, so a test
    # instance's spawned custom agents don't collide with prod's on a shared node.
    export CUSTOM_AGENT_PORT_START=$((19200 + PORT_OFFSET))
    export CUSTOM_AGENT_PORT_END=$((19299 + PORT_OFFSET))
fi

# A test instance sits behind NO Shibboleth SP — it's reached by SSH tunnel to its
# chat app port, not the public agenticweb URL — so relax the chat app's auth gate, or
# every /app and /messages request 401s (no identity headers). Scoped strictly to test;
# prod ALWAYS keeps the SSO gate. Override with CHAT_APP_ALLOW_ANON=0 to keep it on in test
# (the old name PLANNER_ALLOW_ANON still works).
if [ "$DEPLOY_ENV" = "test" ]; then
    export CHAT_APP_ALLOW_ANON="${CHAT_APP_ALLOW_ANON:-${PLANNER_ALLOW_ANON:-1}}"
fi

# Postgres data dir: prod uses the persistent /engrfs volume (real, credential-bearing
# state); test uses node-local per-user scratch so it CANNOT touch prod's data.
if [ "$DEPLOY_ENV" = "prod" ]; then
    PG_DATA_HOSTDIR=/engrfs/project/di2/postgres_data
else
    PG_DATA_HOSTDIR="${TEST_PG_HOSTDIR:-/scratch/$USER/agn-test}/postgres_data"
    mkdir -p "$PG_DATA_HOSTDIR"
fi

# ADS daemon config: prod uses the committed yaml unchanged; test generates a copy
# with the offset server/routing/PG ports so it doesn't collide with prod on one node.
if [ "$PORT_OFFSET" -eq 0 ]; then
    ADS_CONFIG=/workspace/deployment/ads_daemon.yaml
else
    sed -e "s|0.0.0.0:8888|0.0.0.0:${ADS_PORT}|" \
        -e "s|tcp/8999|tcp/${ADS_ROUTING_PORT}|" \
        -e "s|port: 5432|port: ${PG_PORT}|" \
        "$REPO/deployment/ads_daemon.yaml" > "$LOGS/ads_daemon.${DEPLOY_ENV}.yaml"
    ADS_CONFIG="/workspace/logs/$(basename "$LOGS")/ads_daemon.${DEPLOY_ENV}.yaml"
fi

# ── vLLM endpoint auto-discovery ──────────────────────────────────────────────
# The vLLM job lands on a different GPU node each run, so its hostname isn't stable.
# Rather than hand-edit OPENAI_BASE_URL in .env every time, find the running vLLM job
# by name and build the URL from its node. Prefer your own job; else any user's (a
# test instance can share prod's vLLM — inference is stateless). Falls back to the
# .env value if none is found. Set VLLM_AUTODISCOVER=0 to always use .env verbatim.
VLLM_PORT="${VLLM_PORT:-8100}"
VLLM_JOB_NAME="${VLLM_JOB_NAME:-agenticnetwork-vllm}"
if [ "${VLLM_AUTODISCOVER:-1}" != "0" ]; then
    VLLM_NODE="$(squeue -h -u "$USER" -n "$VLLM_JOB_NAME" -t RUNNING -o '%N' 2>/dev/null | head -n1)"
    [ -z "$VLLM_NODE" ] && VLLM_NODE="$(squeue -h -n "$VLLM_JOB_NAME" -t RUNNING -o '%N' 2>/dev/null | head -n1)"
    if [ -n "$VLLM_NODE" ]; then
        export OPENAI_BASE_URL="http://${VLLM_NODE}:${VLLM_PORT}/v1"
        export OPENAI_API_BASE="$OPENAI_BASE_URL"   # litellm agents (coding, intelligence) read this
        echo "[submit] vLLM discovered: ${OPENAI_BASE_URL} (job '${VLLM_JOB_NAME}' on ${VLLM_NODE})"
    else
        echo "[submit] WARN: no running '${VLLM_JOB_NAME}' job found — using OPENAI_BASE_URL from .env: ${OPENAI_BASE_URL:-<unset>}"
    fi
fi

echo "======================================================"
echo " AgenticNetwork starting on $(hostname)"
echo " Instance : ${DEPLOY_ENV} (port offset ${PORT_OFFSET})"
echo " Job ID   : ${SLURM_JOB_ID}  (${SLURM_JOB_NAME})"
echo " Postgres : ${PG_DATA_HOSTDIR} on :${PG_PORT}"
echo " ADS      : :${ADS_PORT} (routing :${ADS_ROUTING_PORT})"
echo " vLLM node: ${OPENAI_BASE_URL}"
echo " Registry : ${IMAGE_REGISTRY}  (default tag: ${IMAGE_TAG:-latest})"
_TAG_OVERRIDES=""
for _svc in POSTGRES ADS REGISTRATION CHAT_APP SCOUT EMAIL CODING INTELLIGENCE DISASTER GEOSPATIAL; do
    _var="${_svc}_IMAGE_TAG"
    [ -n "${!_var:-}" ] && _TAG_OVERRIDES="${_TAG_OVERRIDES} $(printf '%s' "$_svc" | tr '[:upper:]_' '[:lower:]-')=${!_var}"
done
[ -n "$_TAG_OVERRIDES" ] && echo " Img tags :${_TAG_OVERRIDES}"
echo "======================================================"

# ── PostgreSQL (shared: registration "dir" + ADS "dir") ───────────────────────
# Dedicated Postgres service — starts FIRST so both ADS (database=postgres) and
# the registration service can connect as pure clients over host-localhost:5432.
# The data-dir volume is mounted here (moved off the registration container).
PG_READY_HOST="$LOGS/postgres_ready"                                    # host-visible path
PG_READY_CONTAINER="/workspace/logs/$(basename "$LOGS")/postgres_ready"  # same file, container view
rm -f "$PG_READY_HOST" 2>/dev/null || true

srun --ntasks=1 --overlap \
     --container-image="$(image_ref postgres latest)" \
     --container-mounts="$MOUNTS,${PG_DATA_HOSTDIR}:/var/lib/postgresql/data" \
     --container-writable \
     --export=ALL,POSTGRES_READY_FILE="$PG_READY_CONTAINER",PGPORT="$PG_PORT" \
     /entrypoint.sh >> "$LOGS/postgres.log" 2>&1 &

echo "[submit] Waiting for PostgreSQL (registration + dir) ..."
for i in $(seq 1 60); do
    if [ -f "$PG_READY_HOST" ] && python3 -c "import socket; socket.create_connection(('localhost',${PG_PORT}),timeout=1).close()" 2>/dev/null; then
        echo "[submit] PostgreSQL ready (${i}s)."
        break
    fi
    if [ "$i" -eq 60 ]; then
        echo "[submit] ERROR: PostgreSQL not ready after 60s. Check $LOGS/postgres.log"
        exit 1
    fi
    sleep 1
done

# ── ADS daemon ────────────────────────────────────────────────────────────────
# dirctl (v1.2.0) takes its settings from the config file and DIRECTORY_DAEMON_*
# variables only, so the exported DATABASE_URL does not reach it.
srun --ntasks=1 --overlap \
     --container-image="$(image_ref ads v4)" \
     --container-mounts="$MOUNTS" \
     --container-writable \
     /usr/local/bin/dirctl daemon start --config "$ADS_CONFIG" \
     >> "$LOGS/ads.log" 2>&1 &

# ── Wait for ADS (coarse poll — agent entrypoints also retry for up to 60s) ──
echo "[submit] Waiting for ADS on port ${ADS_PORT}..."
for i in $(seq 1 30); do
    if python3 -c "import socket; socket.create_connection(('localhost', ${ADS_PORT}), timeout=1).close()" 2>/dev/null; then
        echo "[submit] ADS ready (${i}s)."
        break
    fi
    if [ "$i" -eq 30 ]; then
        echo "[submit] ERROR: ADS not ready after 30s. Check $LOGS/ads.log"
        exit 1
    fi
    sleep 1
done

# Port assignments (CHAT_APP_PORT, *_A2A_PORT, etc.) are exported in the instance-mode
# block near the top of this script, offset by PORT_OFFSET. Slurm propagates them into
# every container automatically.

# ── Custom agent spawning (registration service) ─────────────────────────────
# ClusterBackend + GitHub→DockerHub build pipeline for approved custom agents.
# (Also settable in .env; exported here so they propagate even if .env omits them.)
export SPAWN_BACKEND="${SPAWN_BACKEND:-cluster}"
export HANDLER_FILES_DIR="${HANDLER_FILES_DIR:-/workspace/registration_service/uploaded_handlers}"

# The cluster build pipeline — and the recover-live-agents-on-redeploy path that
# depends on it — is a no-op without a GitHub PAT and target repo. If they're
# missing, custom agents will NOT be rebuilt or restarted on redeploy; they'll be
# reconciled to 'stopped' and then purged. Surface this loudly rather than let it
# fail silently (see supervisor._oob_build_enabled / recover_live_agents).
if [ "$SPAWN_BACKEND" = "cluster" ]; then
    if [ -z "${GITHUB_TOKEN:-}" ] || [ -z "${GITHUB_REPO:-}" ]; then
        echo "[submit] WARN: SPAWN_BACKEND=cluster but GITHUB_TOKEN/GITHUB_REPO are unset."
        echo "[submit]       Custom-agent build + restart-on-redeploy is DISABLED (approvals fail);"
        echo "[submit]       registered custom agents will not come back after a redeploy."
        echo "[submit]       Set them in $SECRETS_FILE (GITHUB_TOKEN) and .env (GITHUB_REPO)."
    fi
fi

# ── Agent containers ─────────────────────────────────────────────────────────
# Launched with a stagger: all agents publish to the ADS routing index (SQLite)
# shortly after start, and simultaneous writes cause "database is locked
# (SQLITE_BUSY)" crashes. Spacing the launches spreads the publishes out.
STAGGER="${AGENT_START_STAGGER_SECONDS:-4}"

srun --ntasks=1 --overlap \
     --container-image="$(image_ref registration latest)" \
     --container-mounts="$MOUNTS" \
     --container-writable \
     /entrypoint.sh \
     >> "$LOGS/registration.log" 2>> "$LOGS/registration.err" &
sleep "$STAGGER"

srun --ntasks=1 --overlap \
     --container-image="$(image_ref chat-app latest)" \
     --container-mounts="$MOUNTS" \
     --container-writable \
     /entrypoint.sh \
     >> "$LOGS/chat-app.log" 2>> "$LOGS/chat-app.err" &
sleep "$STAGGER"

srun --ntasks=1 --overlap \
     --container-image="$(image_ref scout latest)" \
     --container-mounts="$MOUNTS" \
     --container-writable \
     /entrypoint.sh \
     >> "$LOGS/scout.log" 2>> "$LOGS/scout.err" &
sleep "$STAGGER"

srun --ntasks=1 --overlap \
     --container-image="$(image_ref email latest)" \
     --container-mounts="$MOUNTS" \
     --container-writable \
     /entrypoint.sh \
     >> "$LOGS/email.log" 2>> "$LOGS/email.err" &
sleep "$STAGGER"

srun --ntasks=1 --overlap \
     --container-image="$(image_ref coding latest)" \
     --container-mounts="$MOUNTS" \
     --container-writable \
     /entrypoint.sh \
     >> "$LOGS/coding.log" 2>> "$LOGS/coding.err" &
sleep "$STAGGER"

srun --ntasks=1 --overlap \
     --container-image="$(image_ref intelligence latest)" \
     --container-mounts="$MOUNTS" \
     --container-writable \
     /entrypoint.sh \
     >> "$LOGS/intelligence.log" 2>> "$LOGS/intelligence.err" &
sleep "$STAGGER"

srun --ntasks=1 --overlap \
     --container-image="$(image_ref disaster latest)" \
     --container-mounts="$MOUNTS" \
     --container-writable \
     /entrypoint.sh \
     >> "$LOGS/disaster.log" 2>> "$LOGS/disaster.err" &
sleep "$STAGGER"

srun --ntasks=1 --overlap \
     --container-image="$(image_ref geospatial latest)" \
     --container-mounts="$MOUNTS" \
     --container-writable \
     /entrypoint.sh \
     >> "$LOGS/geospatial.log" 2>> "$LOGS/geospatial.err" &

# ── Host-side spawn loop for approved custom agents (plan Note #8) ────────────
# Runs in this native Slurm env (where `srun` works) — the registration container
# can't call srun itself. Polls the registration service's /internal/* endpoints.
if [ "${SPAWN_BACKEND:-}" = "cluster" ]; then
    echo "[submit] Launching host-side custom-agent spawn loop..."
    # Runs on the HOST (not in a container), so use the host path — /workspace only
    # exists inside containers via --container-mounts.
    LOGS="$LOGS" \
    REGISTRATION_INTERNAL_URL="http://localhost:${REGISTRATION_SERVICE_PORT:-8091}" \
    bash "$REPO/deployment/spawn_agent_loop.sh" >> "$LOGS/spawn_agent_loop.log" 2>&1 &
fi

echo "[submit] All containers launched. Waiting..."
wait
echo "[submit] All containers exited. Job complete."
