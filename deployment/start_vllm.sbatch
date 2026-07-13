#!/bin/bash
#SBATCH --job-name=agenticnetwork-vllm
#SBATCH --partition=general-gpu
#SBATCH --cpus-per-task=8
#SBATCH --mem=32G
#SBATCH --gres=gpu:h100:1
#SBATCH -A engr-lab-di2
#SBATCH --time=04:00:00
#SBATCH --container-image=vllm/vllm-openai:latest
#SBATCH --container-mounts=/engrfs/project/di2/AgenticNetwork:/workspace
#SBATCH --container-writable
#SBATCH --output=/engrfs/project/di2/AgenticNetwork/logs/%u/vllm-%j.log
#SBATCH --error=/engrfs/project/di2/AgenticNetwork/logs/%u/vllm-%j.err

unset CC CXX CUDAHOSTCXX
if command -v gcc >/dev/null 2>&1; then export CC="$(command -v gcc)"; fi
if command -v g++ >/dev/null 2>&1; then export CXX="$(command -v g++)"; fi

export MODEL=openai/gpt-oss-120b
export NUM_GPUS=1
export HF_HUB_CACHE=/workspace/hf_cache
export HOME=/workspace
# HuggingFace token — read from /workspace/.hf_token (gitignored). It is a secret on
# shared /engrfs: keep it `chmod 600` so it is not world-readable. (Tracked as a Phase 1
# follow-up in docs/MULTI_CONTRIBUTOR.md.)
export HF_TOKEN=$(cat /workspace/.hf_token 2>/dev/null || echo "${HF_TOKEN}")
# Served port. Keep in sync with submit.sbatch's vLLM auto-discovery (VLLM_PORT).
CHAT_PORT="${VLLM_PORT:-8100}"

mkdir -p /workspace/hf_cache /workspace/logs

echo "======================================================"
echo " vLLM starting on $(hostname)"
echo " Job ID: ${SLURM_JOB_ID}"
echo " Model : ${MODEL}"
echo " OpenAI-compatible endpoint: http://$(hostname):${CHAT_PORT}/v1/"
echo " (submit.sbatch auto-discovers this node via job name 'agenticnetwork-vllm')"
echo "======================================================"

vllm serve "$MODEL" \
  --served-model-name openai/gpt-oss-120b \
  --reasoning-parser openai_gptoss \
  --tool-call-parser openai \
  --enable-auto-tool-choice \
  --host 0.0.0.0 \
  --port "$CHAT_PORT" \
  --tensor-parallel-size "$NUM_GPUS" \
  --gpu-memory-utilization 0.85 \
  --max-model-len 32768 \
  --max-num-batched-tokens 10240 \
  --max-num-seqs 128
