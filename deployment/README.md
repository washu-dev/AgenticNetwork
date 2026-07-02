# ENGR Cluster Deployment

This directory contains everything needed to run the full AgenticNetwork multi-agent
system on the WashU Engineering HPC cluster (ENGR) using Slurm and Pyxis/enroot.

## Architecture

Each service runs in its **own Docker container**, all launched in parallel as
`srun --overlap` steps within a single Slurm CPU job. A separate Slurm job runs
the vLLM inference server on a GPU node.

```
[CPU node — submit.sbatch]
  srun step: amaankhan25/agenticnetwork-ads:v4              :8888
  srun step: amaankhan25/agenticnetwork-registration:latest :8091  (+ postgres)
  srun step: amaankhan25/agenticnetwork-planner:latest      :8090
  srun step: amaankhan25/agenticnetwork-scout:latest        :9104
  srun step: amaankhan25/agenticnetwork-email:latest        :9103
  srun step: amaankhan25/agenticnetwork-coding:latest       :19106
  srun step: amaankhan25/agenticnetwork-intelligence:latest :19107
  srun step: amaankhan25/agenticnetwork-disaster:latest     :19108
  srun step: amaankhan25/agenticnetwork-geospatial:latest   :19105

[GPU node — start_vllm.sbatch]
  enroot container (vllm/vllm-openai:latest)
    └── vLLM serving openai/gpt-oss-20b   :8100
```

All containers communicate over `localhost` on the same compute node.
The planner reaches vLLM via `OPENAI_BASE_URL=http://<gpu-node>:8100/v1`.

ADS starts first; all agent containers wait up to 60 seconds for it to be
reachable before publishing their cards and starting their A2A servers.

The registration container also runs a PostgreSQL 15 instance internally.
Its data directory is bind-mounted from `/engrfs/project/di2/postgres_data`
so registered agents survive job restarts.

---

## Files

| File | Purpose |
|---|---|
| `submit.sbatch` | Main Slurm job — launches all 9 containers in parallel |
| `start_vllm.sbatch` | GPU Slurm job — starts the vLLM inference server |
| `build_and_push.sh` | Builds and pushes all Docker images to DockerHub |
| `dockerfiles/` | One `Dockerfile.<agent>` per service |
| `entrypoints/` | One `entrypoint-<agent>.sh` per service (postgres init + ADS wait + exec) |
| `ads_daemon.yaml` | dirctl daemon config (listen address, data paths) |
| `google_credentials/` | OAuth credentials for the Email agent |
| `AGENT_INTEGRATION_SPEC.md` | Reference for adding a new agent: what fields are required and why |
| `DOWNSTREAM_CONSIDERATIONS.md` | Forward-looking architecture & scaling decisions (deferred-by-design) |

---

## One-time Setup

### 1. Create required directories on the cluster

```bash
mkdir -p /engrfs/project/di2/AgenticNetwork/logs
mkdir -p /engrfs/project/di2/postgres_data   # persistent postgres storage for registration service
```

### 2. Create your `.env` file

```bash
cp deployment/.env.example deployment/.env
```

Edit `.env` and fill in:

| Variable | Description |
|---|---|
| `OPENAI_BASE_URL` | Set after starting vLLM — e.g. `http://gpu123:8100/v1` |
| `BRAVE_API_KEY` | Brave Search API key for Scout |
| `EMAIL_AGENT_EMAIL` | Gmail address for the Email agent |
| `GOOGLE_CALENDAR_ID` | Google Calendar ID |
| `GOOGLE_CALENDAR_TOKEN` | Path to `token.json` inside the container |
| `GOOGLE_CALENDAR_CREDENTIALS` | Path to `credentials.json` inside the container |
| `GOOGLE_API_KEY` / `GOOGLE_MAPS_API_KEY` | For the GeoSpatial agent |
| `PGDATABASE` | Postgres database name for the registration service (default: `registration`) |
| `REGISTRATION_SERVICE_PORT` | Port for the registration service (default: `8091`) |

Edit `submit.sbatch` and `start_vllm.sbatch` — replace `engr-lab-<piname>` with
your actual PI's account name (find it with `sacctmgr show user $USER`).

---

## Building Images

Run from the repo root on your local machine:

```bash
# Build and push all 9 images
bash deployment/build_and_push.sh

# Build a single image (e.g. after changing only the planner)
bash deployment/build_and_push.sh planner
```

Images are published to `amaankhan25/agenticnetwork-<agent>:latest` on DockerHub.
Pyxis caches images by tag — use a new tag (`:v2`, `:v3`, etc.) if you need to
force a fresh pull on the cluster after pushing a fix to the same tag.

> **Note:** The ADS image is currently pinned to `:v4` in `submit.sbatch` rather
> than `:latest`. Update the tag in `submit.sbatch` if you rebuild and push the ADS image.

---

## Running on the Cluster

### Step 1 — Start the vLLM inference server

```bash
sbatch deployment/start_vllm.sbatch
```

Find the GPU node hostname and update `.env`:

```bash
squeue -u $USER   # note NODELIST, e.g. gpu123
# Then set: OPENAI_BASE_URL=http://gpu123:8100/v1
```

Verify vLLM is ready (from a compute node, not the login node):

```bash
curl http://gpu123:8100/health
# Expected: {"status":"ok"}
```

### Step 2 — Submit the main job

```bash
sbatch deployment/submit.sbatch
```

Watch startup:

```bash
tail -f /engrfs/project/di2/AgenticNetwork/logs/agenticnetwork-<jobid>.log
```

ADS should be ready within ~10 seconds; all agents should be running within ~60 seconds.
The registration container takes a few extra seconds on first run to initialise the
postgres data directory.

---

## Testing

All curl commands must be run **from the compute node** running the job (not the login node):

```bash
ssh <compute-node>   # e.g. ssh amd128-2204
```

### Health check

```bash
curl http://localhost:8090/health
# Expected: {"status":"ok","fake_ads":"False","model":"openai/gpt-oss-20b"}
```

### Registration service

```bash
curl http://localhost:8091/health
# Expected: {"status":"ok"}
```

The registration web UI is also accessible via the planner at `http://localhost:8090/register/`.

### List available agents

```bash
curl -s http://localhost:8090/messages \
  -H "Content-Type: application/json" \
  -d '{"text": "List all available agents."}' | python3 -m json.tool
```

A successful response has `"state": "completed"` and lists all specialist agents.

### Send a task to an agent

```bash
curl -s http://localhost:8090/messages \
  -H "Content-Type: application/json" \
  -d '{"text": "Give me a brief situational summary about wildfire preparedness."}' \
  | python3 -m json.tool
```

---

## Logs

All logs are written to `/engrfs/project/di2/AgenticNetwork/logs/` and persist
after the job exits. The main job log is `agenticnetwork-<jobid>.log`.

| File | Contents |
|---|---|
| `agenticnetwork-<jobid>.log` | Job-level startup and ADS readiness output |
| `ads.log` / `ads.err` | ADS (dirctl) daemon |
| `registration.log` / `registration.err` | Registration service + embedded postgres |
| `planner.log` / `planner.err` | Planner agent |
| `scout.log` / `scout.err` | Scout agent |
| `email.log` / `email.err` | Email agent |
| `coding.log` / `coding.err` | Coding agent |
| `intelligence.log` / `intelligence.err` | Intelligence agent |
| `disaster.log` / `disaster.err` | Disaster Response agent |
| `geospatial.log` / `geospatial.err` | GeoSpatial agent |
| `vllm-<jobid>.log` / `.err` | vLLM server |

---

## Port Reference

| Service | Port | Env var |
|---|---|---|
| ADS daemon | 8888 | `ADS_SERVER_ADDR` |
| Registration service | 8091 | `REGISTRATION_SERVICE_PORT` |
| Planner | 8090 | `MAIN_AGENT_PORT` |
| Scout | 9104 | `SCOUT_HTTP_PORT` |
| Email | 9103 | `EMAIL_AGENT_A2A_PORT` |
| GeoSpatial | 19105 | `GEOSPATIAL_AGENT_A2A_PORT` |
| Coding | 19106 | `CODING_AGENT_A2A_PORT` |
| Intelligence | 19107 | `INTEL_AGENT_A2A_PORT` |
| Disaster Response | 19108 | `DISASTER_AGENT_A2A_PORT` |
| vLLM | 8100 | `OPENAI_BASE_URL` |

Ports 19105–19108 use 5-digit numbers to avoid collisions with other users on shared nodes.

---

## Troubleshooting

**Connection refused on the login node**
Curl must be run from the compute node running the job. Agent ports are bound to
`localhost` inside each container and are not accessible from the login node.

**ADS not ready / agents fail to register**
Check `ads.log`. If it is empty or shows repeated errors, the dirctl binary may
have failed to start. Verify the ADS image is up to date (use a new tag to bypass
Pyxis cache; the ADS image is currently pinned to `:v4` in `submit.sbatch`).

**Registration postgres fails to start**
Check `registration.err`. Common causes: the `/engrfs/project/di2/postgres_data`
directory doesn't exist (create it — see One-time Setup), or a previous run left a
stale `postmaster.pid` (the entrypoint removes it automatically on start, but if
the data directory is corrupted, delete it and let `initdb` re-initialise on next run).

**Custom agents don't auto-start on a fresh deployment (unlike the 6 core agents)**
Known limitation. The 6 core agents are hardcoded as `srun` steps in `submit.sbatch`,
so they always launch with the job. Custom agents are **not** in that static set — their
(re)start is driven entirely by the registration service's Postgres DB: on startup
`Supervisor.recover_live_agents` re-queues only rows with `status='live'` (and whose image
still exists on Docker Hub) to `image_ready`, and the host spawn loop (`spawn_agent_loop.py`)
then `srun`s them. Consequences:
- If the Postgres data dir is fresh/reset, or the custom agent's row isn't `status='live'`
  (e.g. it was `stopped`/`failed`), it will **not** come back up on the next deploy — even
  though its build context is committed in the `agent-directory` GitHub registry and its image
  is on Docker Hub. Recovery is **DB-driven, not registry-driven**: nothing enumerates the
  GitHub registry / Docker Hub to decide what should be running.
- To bring such an agent back, re-approve it via the registration UI (rebuilds/re-queues), or
  set its DB row back to `status='live'` before the deploy so `recover_live_agents` picks it up.
- The `agent-directory` GitHub registry is a **build-context store**, not the source of truth
  for what runs; deleting a stale entry there (see the delete flow) doesn't stop a running agent
  and leaving one there doesn't start it.

**`ModuleNotFoundError` or wrong Python version in agent logs**
The cluster auto-mounts its system anaconda (Python 3.7) into containers. All
entrypoints use absolute paths to the conda env Python to avoid this. If you see
this error, check that the relevant entrypoint script still uses the full path.

**vLLM returns 404 or connection errors**
Confirm the GPU job is still running (`squeue -u $USER`) and that `OPENAI_BASE_URL`
in `.env` matches the current GPU node hostname. GPU jobs time out and the node
hostname changes on each new submission.

**Pyxis serving a stale image after a fix**
Pyxis caches images by name and tag. Push to a new tag (e.g. `:v5`) and update
the image reference in `submit.sbatch` to force a fresh pull.

**Port conflict with another user's job**
Change the conflicting port via the corresponding env var in `.env`
(e.g. `DISASTER_AGENT_A2A_PORT=29107`). No rebuild needed.
