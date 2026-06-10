#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# submit.sbatch — Slurm job for the AgenticNetwork single-container deployment.
#
# Prerequisites:
#   1. Push the image to DockerHub (run once from your local machine):
#        docker build --platform linux/amd64 \
#            -f deployment/Dockerfile -t <dockerhub-user>/agenticnetwork:latest .
#        docker push <dockerhub-user>/agenticnetwork:latest
#
#   2. Fill in deployment/.env (copy from deployment/.env.example).
#      Make sure OPENAI_BASE_URL points to your vLLM job's compute node.
#
#   3. Update --container-image below with your DockerHub username.
#
#   4. Submit from the repo root on the cluster:
#        sbatch deployment/submit.sbatch
# ─────────────────────────────────────────────────────────────────────────────

#SBATCH --job-name=agenticnetwork
#SBATCH -p general-cpu
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=8
#SBATCH --mem=32G
#SBATCH --time=04:00:00
#SBATCH -A engr-lab-<piname>

# Update these paths to match your project directory.
#SBATCH --output=/engrfs/project/di2/AgenticNetwork/logs/agenticnetwork-%j.log
#SBATCH --error=/engrfs/project/di2/AgenticNetwork/logs/agenticnetwork-%j.err

# ── Container settings ────────────────────────────────────────────────────────
# Replace with your DockerHub username.
#SBATCH --container-image=amaakhan25/agenticnetwork:latest

# ── Job body ──────────────────────────────────────────────────────────────────
mkdir -p /engrfs/project/di2/AgenticNetwork/logs

# Load env vars from .env file into the shell — these get inherited by the container
set -a
source /engrfs/project/di2/AgenticNetwork/deployment/.env
set +a

echo "======================================================"
echo " AgenticNetwork starting on $(hostname)"
echo " Job ID   : ${SLURM_JOB_ID}"
echo " vLLM node: ${OPENAI_BASE_URL}"
echo "======================================================"
echo ""

/entrypoint.sh
