#!/bin/bash
# Forward port 8100 on the GCP relay VM to the ENGR node that is running the vLLM job.
#
# The vLLM Slurm job (deployment/start_vllm.sbatch) runs for at most 12 hours and a
# resubmitted job can land on any GPU node, so the target is looked up each time the
# tunnel starts: `squeue` on the ENGR login host, by job name, the same way
# submit.sbatch finds it. With no running job this exits 75 and systemd retries.
# vllm-tunnel-check.sh restarts the tunnel when vLLM stops answering through it.
#
# Settings (/etc/default/vllm-tunnel):
#   ENGR_LOGIN       user@login-host the tunnel logs in as (required)
#   SSH_KEY          private key for that login (required)
#   VLLM_JOB_NAME    Slurm job name (default agenticnetwork-vllm)
#   VLLM_PORT        port vLLM serves on, and the local port (default 8100)
#   VLLM_NODE_SUFFIX appended to the node name (default .engr.wustl.edu)
set -euo pipefail

: "${ENGR_LOGIN:?set ENGR_LOGIN in /etc/default/vllm-tunnel}"
: "${SSH_KEY:?set SSH_KEY in /etc/default/vllm-tunnel}"
JOB_NAME="${VLLM_JOB_NAME:-agenticnetwork-vllm}"
PORT="${VLLM_PORT:-8100}"
SUFFIX="${VLLM_NODE_SUFFIX-.engr.wustl.edu}"
SSH_OPTS=(-i "$SSH_KEY" -o BatchMode=yes -o ConnectTimeout=20 -o ServerAliveInterval=30
          -o ServerAliveCountMax=3 -o StrictHostKeyChecking=accept-new)

node="$(ssh "${SSH_OPTS[@]}" "$ENGR_LOGIN" "squeue -h -n '$JOB_NAME' -t R -o %N" | head -n 1 || true)"
if [ -z "$node" ]; then
  echo "no running Slurm job named $JOB_NAME; retrying later"
  exit 75
fi
case "$node" in *[!A-Za-z0-9.-]*) echo "unexpected node list from squeue: $node"; exit 75 ;; esac
case "$node" in *.*) target="$node" ;; *) target="${node}${SUFFIX}" ;; esac

echo "forwarding 0.0.0.0:$PORT -> $target:$PORT via $ENGR_LOGIN"
exec ssh "${SSH_OPTS[@]}" -N -o ExitOnForwardFailure=yes -L "0.0.0.0:$PORT:$target:$PORT" "$ENGR_LOGIN"
