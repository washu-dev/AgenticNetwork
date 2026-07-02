#!/bin/bash
# Host-side custom-agent spawn loop (plan Note #8). Thin wrapper that execs the
# Python implementation (stdlib-only; no jq needed on compute nodes). Launched in
# the background by submit.sbatch so it runs in the native Slurm env where `srun`
# works. Inherits REGISTRATION_INTERNAL_URL, SPAWN_LOOP_POLL_SECONDS, LOGS from env.
exec python3 "$(dirname "$0")/spawn_agent_loop.py"
