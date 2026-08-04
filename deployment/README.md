# deployment/ — build & run machinery

This directory holds the **artifacts** that build and run AgenticNetwork, shared across both
deployment targets (ENGR cluster and GCP). **The documentation lives in
[`docs/deployment/`](../docs/deployment/)** — start there:

- **[docs/deployment/README.md](../docs/deployment/README.md)** — deployment overview (the two targets).
- **[docs/deployment/engr/](../docs/deployment/engr/README.md)** — ENGR cluster (Slurm + Pyxis/enroot) runbook.
- **[docs/deployment/gcp/](../docs/deployment/gcp/README.md)** — GCP (Cloud Run + Cloud SQL) runbook + migration.

## What's here

| Artifact | Target | What it is |
|---|---|---|
| `dockerfiles/` | both | One image per service (`Dockerfile.<svc>`); built by `build_and_push.sh` → `di2washu/agenticnetwork-<svc>`. **The same images run on the cluster and on Cloud Run.** |
| `entrypoints/` | both | Container entrypoints (`entrypoint-<svc>.sh`). `entrypoint-registration.sh` branches for Cloud Run via `SKIP_STARTUP_WAITS` + `$PORT`. |
| `build_and_push.sh` | both | Build + push the infra images (`linux/amd64`). |
| `submit.sbatch`, `start_vllm.sbatch` | engr | Slurm launch — service containers as parallel `srun --overlap` steps; vLLM on a GPU node. |
| `spawn_agent_loop.py` / `.sh` | engr | Host-side loop that `srun`s image-ready custom agents (the `ClusterBackend` host loop). |
| `deploy.sh` | engr | Cluster deploy wrapper. |
| `ads_daemon.yaml` / `ads_daemon.gcp.yaml` | engr / gcp | ADS daemon configs. |
| `scripts/gcp_iam_step7.sh` | gcp | Idempotent GCP IAM setup for the CloudRunBackend (service accounts + grants). |
| `gcp_target_architecture.svg` | gcp | Target-architecture diagram. |
| `.env.example`, `secrets.env.example` | both | Config templates. |

> Artifacts stay **flat** here (not split into `engr/`/`gcp/` subfolders) because several are
> shared and are referenced by code (`registration_service/`, `agent_skeleton/generator/`) and by
> each other — foldering them would ripple into those paths. Only the *docs* are split by target.
