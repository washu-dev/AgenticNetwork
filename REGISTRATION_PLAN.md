# Agent Registration Service — Planning Doc

Track work toward enabling non-technical researchers to self-register agents.

---

## Architecture Summary

### Existing agents (unchanged)
Hand-built specialists (`email_agent`, `disaster_response_agent`, etc.) keep their current
design: each has its own Dockerfile + entrypoint, runs as a sibling container in `submit.sbatch`
via Pyxis/enroot. Nothing about this changes.

### Researcher-registered agents (new)
Created at runtime through the wizard. The registration service spawns these as subprocesses
**within its own container** — not as sibling containers in the Slurm job.

```
submit.sbatch
├── srun: ADS container               (existing, unchanged)
├── srun: planner container           (existing, unchanged)
├── srun: email_agent container       (existing, unchanged)
├── srun: ...other agents...          (existing, unchanged)
└── srun: registration_service container
              └── subprocess: researcher-agent-1
              └── subprocess: researcher-agent-2
```

On cloud migration, researcher agents become proper Kubernetes Pods. Existing agents get their
own Deployments too. The registration service is the only thing that changes shape.

### ContainerBackend abstraction
The registration service never directly spawns processes — it calls a `ContainerBackend` interface:

```python
class ContainerBackend:
    async def spawn(self, config: AgentConfig) -> str   # returns endpoint URL
    async def stop(self, agent_id: str)
    async def health(self, agent_id: str) -> bool
```

| Environment | Backend | Notes |
|---|---|---|
| ENGR POC | `SubprocessBackend` | asyncio subprocess, no container isolation |
| Cloud (prod) | `KubernetesBackend` | real Pod isolation, resource limits, native restarts |
| Local dev | `SubprocessBackend` | same as ENGR, works on any laptop |

ENGR does not support Podman or Docker-in-Docker (Pyxis/enroot only). Subprocess is the right
POC choice — researcher population is trusted WashU academics behind institutional access controls.
Sandboxing is a cloud migration task, not a POC blocker.

### Handler types
Each agent subprocess runs the generic `registration_service.runner` module, parameterized by a
config JSON written by the registration service before spawning:

| Type | What runs | Isolation needed |
|---|---|---|
| `api_proxy` | in-process HTTP call, no subprocess | none |
| `llm` | system prompt + model + tool loop | subprocess |
| `script` | uploaded Python file, `run(input) -> str` | subprocess (container on cloud) |

### Database
Postgres from day one (not SQLite) — runs in a container on ENGR, on RDS/Cloud SQL on cloud,
same connection string shape. DB is source of truth; ADS is rebuilt from it on every restart.

---

## Checklist

### 1. Repo Cleanup (prerequisite — remove dead code before building on top of it)

- [x] Delete obsolete single-container deployment artifacts
  - `deployment/Dockerfile` (was already gone)
  - `deployment/entrypoint.sh` ✓
  - `deployment/supervisord.conf` ✓
- [ ] Remove or archive local-run scripts no longer used (Tier A/B/C)
  - `planner_agent/scripts/start.sh`
  - `planner_agent/scripts/doctor.sh`
  - Local conda env setup instructions in READMEs
- [x] Strip hardcoded `10.229.117.x` IPs from all agent cards and configs ✓
  - Card JSON files → `http://AGENT_HOST:{port}/` placeholder
  - Python `DEFAULT_AGENT_URL` / `DEFAULT_ADS_SERVER_ADDR` constants → `localhost`
  - All entrypoints → pass `--advertise-url http://localhost:{port}/` at startup
  - Added `--advertise-url` + `--directory-server` support to coding and intelligence agents (were missing both)
- [x] Centralize per-agent ADS publish logic into `ads_utils/` shared package ✓
  - `ads_utils/publisher.py` — `DirectoryPublisher` (push + routing publish + poll)
  - `ads_utils/converter.py` — `agent_card_to_ads_record` with real OASF taxonomy IDs
  - All 5 agents (coding, intelligence, disaster, geospatial, email) migrated
  - OASF skill taxonomy sourced from agntcy/oasf GitHub schema repo

### 2. Registration Service — Core

- [ ] Define Postgres DB schema (agent records, port pool, handler config, status)
- [ ] Define `AgentConfig` JSON schema (what a subprocess reads on startup)
- [ ] Implement `ContainerBackend` interface + `SubprocessBackend` for ENGR POC
- [ ] Build generic runner (`registration_service.runner`) for each handler type
  - [ ] `api_proxy` — in-process, no subprocess
  - [ ] `llm` — system prompt + model + tool loop
  - [ ] `script` — uploaded file, sandboxed on cloud
- [ ] Build process supervisor (spawn, health-check poll, restart with backoff, graceful shutdown)
- [ ] Build ADS publisher (dirctl push + routing publish/unpublish, called after health confirmed)
- [ ] Port pool manager (assign on register, reclaim on delete)
- [ ] On-restart recovery (re-spawn + re-publish all `status=live` agents from DB)
- [ ] FastAPI REST API
  - [ ] `POST /agents` — register
  - [ ] `GET /agents` — list
  - [ ] `GET /agents/{id}` — status + card
  - [ ] `DELETE /agents/{id}` — graceful shutdown + unpublish
  - [ ] `POST /agents/{id}/restart` — manual restart

### 3. Web Wizard UI

- [ ] Entry point in planner chat UI (`:8090`) or standalone page
- [ ] "Wrap existing API" flow — URL + auth + skill descriptions → register
- [ ] "LLM agent" flow — system prompt + model + skill descriptions → register
- [ ] "Upload script" flow — file upload + skill descriptions → register
- [ ] Live status view — list registered agents, health indicator, logs

### 4. Deployment Integration (ENGR)

- [ ] Registration service Dockerfile + entrypoint
- [ ] Postgres container + persistent volume in deployment
- [ ] Add registration service to `submit.sbatch` (starts after ADS, before planner)
- [ ] Persistent volume for script uploads
- [ ] Document new agent registration flow in CLAUDE.md / AGENTS.md

### 5. Cloud Migration (post-funding)

- [ ] Implement `KubernetesBackend` (calls K8s API: create/delete Deployment + Service)
- [ ] Swap Postgres to managed DB (RDS / Cloud SQL)
- [ ] Swap script upload storage to S3 / GCS
- [ ] Add real sandboxing for script-runner agents (Pod resource limits, network policies)
- [ ] Auth — WashU SSO or equivalent

---

## Open Questions

- Auth for POC — open to any WashU researcher, or manually gated by an admin?
- Script-runner resource limits on ENGR — CPU/memory caps per subprocess?
- Where does the wizard UI live — inside planner (`:8090`) or separate port?
