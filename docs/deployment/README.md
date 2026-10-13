# Deployment

AgenticNetwork runs on two targets. The **machinery** (Dockerfiles, entrypoints, build/launch
scripts) lives in the repo-root [`deployment/`](../../deployment/) and is shared; the **guides**
live here.

| Target | Guide | What it is |
|---|---|---|
| **ENGR cluster** | [engr/README.md](engr/README.md) | WashU Engineering HPC cluster — Slurm + Pyxis/enroot, one container per service as parallel `srun --overlap` steps, vLLM on a GPU node. The original deployment and still the **live public** one (`agenticweb.engr.wustl.edu`). |
| **GCP** | [gcp/README.md](gcp/README.md) | Google Cloud — Cloud Run (planner, registration, on-demand agents) + Cloud SQL + a GCE ADS VM, with the `CloudRunBackend` auto-deploying registered agents. Built to a working milestone in 2026-08 and paused since the end of summer 2026; running on cloud is a critical leg of the project going forward, so resuming it is a priority (2026-10-13). |

The two guides share the same outline (*architecture · services · access · dev · operate*) so you
can read them **side-by-side** to see how the same system maps onto each platform. GCP is
**additive** — the cluster stays live; nothing was deleted.

## Target-agnostic (here at the top level)
- [AGENT_INTEGRATION_SPEC.md](AGENT_INTEGRATION_SPEC.md) — how an agent integrates with the system (the contract), independent of where it runs.
- [DOWNSTREAM_CONSIDERATIONS.md](DOWNSTREAM_CONSIDERATIONS.md) — forward-looking architecture/scaling decisions (append-only log).

## GCP deep-dives (under `gcp/`)
- [gcp/MIGRATION.md](gcp/MIGRATION.md) — the migration narrative: goal, tracks, decisions, phasing, status.
- [gcp/DEPLOYED_COMPONENTS.md](gcp/DEPLOYED_COMPONENTS.md) — the GCP inventory as of 2026-08-03 (re-verify before relying on it) + control commands.
- [gcp/DESIGN_CLOUDRUN_BACKEND.md](gcp/DESIGN_CLOUDRUN_BACKEND.md) · [gcp/IMPLEMENTATION_CLOUDRUN_BACKEND.md](gcp/IMPLEMENTATION_CLOUDRUN_BACKEND.md) — the register→deploy automation (design + step-by-step).
