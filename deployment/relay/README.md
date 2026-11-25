# vLLM relay (GCP → ENGR)

The `vllm-relay` VM in GCP forwards its port 8100 over SSH to the ENGR GPU node running the vLLM
job (`deployment/start_vllm.sbatch`), so the chat app on Cloud Run can use the cluster's
`gpt-oss-120b` as its LLM (`OPENAI_BASE_URL=http://<relay internal IP>:8100/v1`). Background:
[`docs/deployment/gcp/MIGRATION.md`](../../docs/deployment/gcp/MIGRATION.md) "vLLM endpoint from GCP".

## Why these files

The vLLM job runs at most 12 hours (`#SBATCH --time=12:00:00`), and a resubmitted job can start on
any GPU node. The original relay forwarded to one fixed node (`h100-2405`), so after the job moved
or ended it stayed connected and refused every request, and the chat app ran on its fallback
until someone noticed. Here:

- `vllm-tunnel.sh` asks Slurm on the ENGR login host which node runs the job (`squeue -n
  agenticnetwork-vllm -t R`, as `submit.sbatch` does) and forwards to it. With no job running it
  exits, and systemd tries again a minute later.
- `vllm-tunnel-check.sh`, every 2 minutes from `vllm-tunnel-check.timer`, calls vLLM's `/health`
  through the tunnel. After two failed checks in a row it restarts the tunnel, which looks the node
  up again.

The relay does **not** start the vLLM job. Someone still has to submit `start_vllm.sbatch` every
12 hours (or arrange a longer limit or an automatic resubmission on the cluster), or the chat app
runs on its fallback. The alert policy "Chat app is using the fallback LLM" in `di2-agenticweb`
(log metric `chat_app_llm_failover`) fires when that happens while vLLM is the primary.

## Install or update on the VM

Copy this directory to the VM and run `install.sh` as root. `RELAY_USER` is the VM account that
holds the ENGR SSH key, and `ENGR_LOGIN` is the ENGR account it logs in as:

```bash
gcloud compute scp --recurse deployment/relay vllm-relay:~/relay --zone=us-central1-a
gcloud compute ssh vllm-relay --zone=us-central1-a --command='cd ~/relay && sudo RELAY_USER=<vm user> ENGR_LOGIN=<engr user>@shell.engr.wustl.edu SSH_KEY=/home/<vm user>/.ssh/id_ed25519 bash install.sh'
```

It replaces an older `vllm-tunnel.service` in place. Check it with
`systemctl status vllm-tunnel vllm-tunnel-check.timer` and `curl -s localhost:8100/health`.

The tunnel runs `squeue` (read-only) on the login host as `ENGR_LOGIN`. Today that is one
person's ENGR account and key; a shared or service account would keep the relay working when
that person leaves the project.
