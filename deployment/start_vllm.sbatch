#!/bin/bash
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
export HF_TOKEN=$(cat /workspace/.hf_token 2>/dev/null || echo "${HF_TOKEN}")
CHAT_PORT=8100

mkdir -p /workspace/hf_cache /workspace/logs

echo "======================================================"
echo " vLLM starting on $(hostname)"
echo " Job ID: ${SLURM_JOB_ID}"
echo " Model : ${MODEL}"
echo " OpenAI-compatible endpoint: http://$(hostname):${CHAT_PORT}/v1/"
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
